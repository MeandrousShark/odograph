from __future__ import annotations

import asyncio
import os
from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from psycopg import errors

from app.auth import require_csrf
from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _route(path: str, method: str | None = None):
    for route in make_router().routes:
        if (
            getattr(route, "path", None) == path
            and (method is None or method in getattr(route, "methods", set()))
        ):
            return route
    raise AssertionError(f"route {path} missing")


def _request(pool):
    config = SimpleNamespace(display_tz=TZ)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=config, templates=make_templates(config),
        )),
        session={"csrf": "token"},
    )


async def _reset(pool):
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _crud_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO vehicles (name, active) VALUES ('Retired truck', false) RETURNING id"
            )
            vehicle_id = (await cur.fetchone())[0]

        request = _request(pool)
        add = _route("/expenses", "POST").endpoint
        await add(
            request, vehicle_id=vehicle_id, incurred_on="2026-03-04", category="fuel",
            amount="12.34", treatment="business_use_allocated", notes="station",
            user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, amount, category::text, treatment::text, notes FROM expenses"
            )
            expense_id, amount, category, treatment, notes = await cur.fetchone()
        assert amount == Decimal("12.34")
        assert (category, treatment, notes) == (
            "fuel", "business_use_allocated", "station",
        )

        page = await _route("/expenses", "GET").endpoint(
            request, year=2026, vehicle=str(vehicle_id), user=USER
        )
        body = page.body.decode()
        assert "Retired truck (inactive)" in body
        assert "$12.34" in body

        await _route("/expenses/{expense_id}/update").endpoint(
            request, expense_id=expense_id, vehicle_id=vehicle_id,
            incurred_on="2026-05-01", category="tolls", amount="20.00",
            treatment="fully_business", notes="bridge", user=USER,
        )
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT incurred_on, amount, category::text, treatment::text FROM expenses WHERE id = %s",
                (expense_id,),
            )).fetchone()
        assert row == (date(2026, 5, 1), Decimal("20.00"), "tolls", "fully_business")

        await _route("/expenses/{expense_id}/delete").endpoint(
            request, expense_id=expense_id, user=USER
        )
        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM expenses")).fetchone())[0] == 0
    finally:
        await pool.close()


def test_expense_crud_supports_inactive_vehicle_and_exact_money():
    asyncio.run(_crud_scenario())


async def _constraints_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Truck') RETURNING id"
            )).fetchone())[0]
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment) "
                    "VALUES (%s, current_date, 'fuel', 0, 'business_use_allocated')",
                    (vehicle_id,),
                )
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment) "
                "VALUES (%s, current_date, 'fuel', 1, 'business_use_allocated')",
                (vehicle_id,),
            )
            with pytest.raises(errors.ForeignKeyViolation):
                await conn.execute("DELETE FROM vehicles WHERE id = %s", (vehicle_id,))
    finally:
        await pool.close()


def test_expense_migration_enforces_positive_amount_and_preserves_vehicle_identity():
    asyncio.run(_constraints_scenario())


async def _expense_only_report_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Truck') RETURNING id"
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment) "
                "VALUES (%s, '2026-06-01', 'parking', 30.00, 'fully_business')",
                (vehicle_id,),
            )
        request = _request(pool)
        response = await _route("/report/{year}").endpoint(request, year=2026, user=USER)
        body = response.body.decode()
        assert "No trips recorded in 2026" in body
        assert "Standard vs. actual expense estimate" in body
        assert "Unavailable" in body
        assert "$30.00" in body

        export = await _route("/report/{year}/export").endpoint(request, year=2026, user=USER)
        wb = load_workbook(BytesIO(export.body))
        assert wb.sheetnames == ["Summary", "Trips", "Expenses"]
        assert wb["Expenses"]["D2"].value == 30
    finally:
        await pool.close()


def test_report_and_export_render_expenses_when_there_are_no_trips():
    asyncio.run(_expense_only_report_scenario())


async def _inconsistent_odometer_report_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Truck') RETURNING id"
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, vehicle_id, category) "
                "VALUES "
                "('manual', 'manual', '2026-06-01T12:00:00Z', '2026-06-01T13:00:00Z', %s, %s, 'business'), "
                "('manual', 'manual', '2026-06-02T12:00:00Z', '2026-06-02T13:00:00Z', %s, %s, 'personal')",
                (80 * 1609.344, vehicle_id, 20 * 1609.344, vehicle_id),
            )
            await conn.execute(
                "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment) "
                "VALUES (%s, '2026-06-01', 'fuel', 1000.00, 'business_use_allocated')",
                (vehicle_id,),
            )
            await conn.execute(
                "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m) VALUES "
                "(%s, '2026-01-01T08:00:00Z', %s), (%s, '2027-01-01T08:00:00Z', %s)",
                (vehicle_id, 1000 * 1609.344, vehicle_id, 1050 * 1609.344),
            )

        response = await _route("/report/{year}").endpoint(
            _request(pool), year=2026, user=USER
        )
        body = response.body.decode()
        assert "Provisional (odometer ignored)" in body
        assert "smaller than recorded business miles" in body
        assert "80.0%" in body
        assert "$800.00" in body
        assert "160.0%" not in body
    finally:
        await pool.close()


def test_html_report_ignores_positive_odometer_span_smaller_than_business_miles():
    asyncio.run(_inconsistent_odometer_report_scenario())


def test_all_mutating_expense_routes_require_csrf():
    for path in ("/expenses", "/expenses/{expense_id}/update", "/expenses/{expense_id}/delete"):
        route = _route(path, "POST")
        assert any(dependency.dependency is require_csrf for dependency in route.dependencies)


def test_expense_input_rejects_invalid_fields_before_database_write():
    from app.ui import _parse_expense_input

    for args in (
        ("bad", "fuel", "1.00", "business_use_allocated"),
        ("2026-01-01", "bogus", "1.00", "business_use_allocated"),
        ("2026-01-01", "fuel", "0", "business_use_allocated"),
        ("2026-01-01", "fuel", "1.005", "business_use_allocated"),
        ("2026-01-01", "other", "1.00", ""),
    ):
        with pytest.raises(HTTPException) as exc:
            _parse_expense_input(*args)
        assert exc.value.status_code == 400
