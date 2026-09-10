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
from app.db import make_pool
from app.detector.core import Params
from app.main import make_templates
from app.ui import make_router
from conftest import reset_db

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
    config = SimpleNamespace(display_tz=TZ, app_version="test", detector_params=Params())
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=config, templates=make_templates(config),
        )),
        session={"csrf": "token"},
    )


async def _crud_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
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


async def _trip_detail_expense_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Trip Car') RETURNING id"
            )).fetchone())[0]
            trip_id = (await (await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, vehicle_id) "
                "VALUES ('manual', 'manual', '2026-06-01T01:30:00Z', "
                "'2026-06-01T02:30:00Z', 1000, %s) RETURNING id",
                (vehicle_id,),
            )).fetchone())[0]

        request = _request(pool)
        add = _route("/trips/{trip_id}/expenses", "POST").endpoint
        response = await add(
            request, trip_id=trip_id, category="fuel", amount="12.34",
            treatment="business_use_allocated", notes="trip receipt",
            vehicle_id="999", incurred_on="2000-01-01", user=USER,
        )
        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == f"/trips/{trip_id}"

        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT vehicle_id, incurred_on, amount, notes, trip_id FROM expenses"
            )).fetchone()
        assert row == (vehicle_id, date(2026, 5, 31), Decimal("12.34"), "trip receipt", trip_id)

        detail = await _route("/trips/{trip_id}").endpoint(
            request, trip_id=trip_id, user=USER
        )
        body = detail.body.decode()
        assert "Expenses for this trip" in body
        assert "$12.34" in body
        assert "trip receipt" in body
    finally:
        await pool.close()


def test_trip_detail_add_expense_uses_trip_vehicle_and_local_date():
    asyncio.run(_trip_detail_expense_scenario())


async def _constraints_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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


async def _not_deductible_trip_report_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Truck') RETURNING id"
            )).fetchone())[0]
            # The second trip's own category is 'business': the exclusion
            # must still keep it out of business_m, or this end-to-end path
            # would silently undercount the actual-expense denominator's
            # correction while overcounting the deductible numerator.
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " vehicle_id, category, exclusion) VALUES "
                "('manual', 'manual', '2026-06-01T12:00:00Z', '2026-06-01T13:00:00Z', "
                " %s, %s, 'business', NULL), "
                "('manual', 'manual', '2026-06-02T12:00:00Z', '2026-06-02T13:00:00Z', "
                " %s, %s, 'business', 'not_deductible')",
                (80 * 1609.344, vehicle_id, 20 * 1609.344, vehicle_id),
            )
            await conn.execute(
                "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment) "
                "VALUES (%s, '2026-06-01', 'fuel', 1000.00, 'business_use_allocated')",
                (vehicle_id,),
            )

        response = await _route("/report/{year}").endpoint(
            _request(pool), year=2026, user=USER
        )
        body = response.body.decode()
        # 80 deductible miles out of 100 GPS-total miles in the
        # actual-expense comparison specifically: the excluded trip's 20
        # miles still count toward the denominator, just not the business
        # numerator, so this comparison's percentage is 80%, not 100%. (The
        # annual report's own business percentage is a separate figure this
        # test doesn't assert on.)
        assert "80.0%" in body
        assert "$800.00" in body
    finally:
        await pool.close()


def test_html_report_excludes_not_deductible_trip_from_business_pct_even_when_categorized_business():
    asyncio.run(_not_deductible_trip_report_scenario())


async def _link_unlink_preserves_report_outputs_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Trip Car') RETURNING id"
            )).fetchone())[0]
            trip_id = (await (await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                "vehicle_id, category) VALUES ('manual', 'manual', "
                "'2026-06-02T12:00:00Z', '2026-06-02T13:00:00Z', %s, %s, 'business') "
                "RETURNING id",
                (10 * 1609.344, vehicle_id),
            )).fetchone())[0]
            expense_id = (await (await conn.execute(
                "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment, notes) "
                "VALUES (%s, '2026-06-02', 'fuel', 40.00, 'business_use_allocated', 'receipt') RETURNING id",
                (vehicle_id,),
            )).fetchone())[0]

        request = _request(pool)
        annual = _route("/report/{year}").endpoint
        date_range = _route("/report/range").endpoint
        dashboard = _route("/").endpoint

        async def outputs():
            annual_response = await annual(request, year=2026, user=USER)
            range_response = await date_range(
                request, from_="2026-06-01", to="2026-06-07", user=USER
            )
            dashboard_response = await dashboard(
                request, user=USER, week="2026-06-01"
            )
            return (
                annual_response.context["report"].total_deduction,
                annual_response.context["expense_report"].allocated_total,
                range_response.context["report"].total_deduction,
                dashboard_response.context["dashboard"].deduction.amount,
                annual_response.body,
                range_response.body,
                dashboard_response.body,
            )

        async def expense_row():
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT vehicle_id, incurred_on, category::text, amount, treatment::text, "
                    "notes, trip_id FROM expenses WHERE id = %s",
                    (expense_id,),
                )
                return await cur.fetchone()

        before = await outputs()
        expense_before = await expense_row()
        await _route("/expenses/{expense_id}/update").endpoint(
            request, expense_id=expense_id, vehicle_id=vehicle_id,
            incurred_on="2026-06-02", category="fuel", amount="40.00",
            treatment="business_use_allocated", notes="receipt", trip_id=str(trip_id),
            user=USER,
        )
        expense_linked = await expense_row()
        assert expense_linked[:-1] == expense_before[:-1]
        assert expense_linked[-1] == trip_id
        linked = await outputs()

        await _route("/expenses/{expense_id}/update").endpoint(
            request, expense_id=expense_id, vehicle_id=vehicle_id,
            incurred_on="2026-06-02", category="fuel", amount="40.00",
            treatment="business_use_allocated", notes="receipt", trip_id="",
            user=USER,
        )
        expense_unlinked = await expense_row()
        unlinked = await outputs()

        assert expense_unlinked == expense_before
        assert linked[:4] == before[:4]
        assert linked[4] == before[4]
        assert linked[5] == before[5]
        assert b"1 expense" in linked[6]
        assert unlinked == before
    finally:
        await pool.close()


def test_link_and_unlink_leave_annual_range_and_dashboard_deductions_unchanged():
    asyncio.run(_link_unlink_preserves_report_outputs_scenario())


def test_all_mutating_expense_routes_require_csrf():
    for path in (
        "/expenses", "/expenses/{expense_id}/update", "/expenses/{expense_id}/delete",
        "/trips/{trip_id}/expenses",
    ):
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
