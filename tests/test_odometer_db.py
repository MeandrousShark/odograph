"""DB-backed tests for the odometer settings CRUD routes. Route
handlers are invoked directly, same convention
tests/test_ui_merge_db.py uses: `dependencies=[Depends(require_csrf)]` is a
router-level concern the ASGI app enforces, not something calling the
Python function directly exercises, so these tests skip straight past it.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} missing")


def _request(pool):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool,
            config=SimpleNamespace(display_tz=TZ),
            templates=make_templates(SimpleNamespace(display_tz=TZ)),
        )),
        session={"csrf": "token"},
    )


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _create_vehicle(conn, name: str) -> int:
    cur = await conn.execute("INSERT INTO vehicles (name) VALUES (%s) RETURNING id", (name,))
    return (await cur.fetchone())[0]


async def _insert_trip(conn, vehicle_id: int, started_at: datetime, distance_m: float) -> None:
    await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, vehicle_id) "
        "VALUES ('manual', 'manual', %s, %s, %s, %s)",
        (started_at, started_at + timedelta(minutes=15), distance_m, vehicle_id),
    )


async def _reading_rows(conn, vehicle_id: int):
    cur = await conn.execute(
        "SELECT recorded_at, odometer_m, note FROM odometer_readings "
        "WHERE vehicle_id = %s ORDER BY recorded_at",
        (vehicle_id,),
    )
    return await cur.fetchall()


async def _add_delete_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            # Detected 60mi in the interval, so a 100mi-delta reading pair
            # produces a 60% coverage / 40mi gap interval.
            await _insert_trip(
                conn, truck_id, datetime(2026, 1, 5, 12, tzinfo=timezone.utc), 60 * 1609.344
            )

        add = _endpoint("/settings/odometer")
        delete = _endpoint("/settings/odometer/{reading_id}/delete")

        request = _request(pool)
        await add(
            request, vehicle_id=truck_id, date="2026-01-01", time="00:00",
            value=1000.0, note="first", user=USER,
        )
        await add(
            request, vehicle_id=truck_id, date="2026-01-10", time="00:00",
            value=1100.0, note="second", user=USER,
        )

        async with pool.connection() as conn:
            rows = await _reading_rows(conn, truck_id)
        assert len(rows) == 2
        # 100 mi entry stores 160934.4 m.
        assert rows[0][1] == pytest.approx(1000 * 1609.344)
        assert rows[1][1] == pytest.approx(1100 * 1609.344)

        from app.ui import _fetch_odometer_context
        async with pool.connection() as conn:
            context = await _fetch_odometer_context(conn)
        entry = next(e for e in context if e["vehicle"]["id"] == truck_id)
        assert len(entry["readings"]) == 2
        # Newest first.
        assert entry["readings"][0]["note"] == "second"
        assert len(entry["intervals"]) == 1
        iv = entry["intervals"][0]
        assert iv.odometer_delta_m == pytest.approx(100 * 1609.344)
        assert iv.detected_m == pytest.approx(60 * 1609.344)
        assert iv.coverage == pytest.approx(0.6)
        assert iv.data_error is False

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM odometer_readings WHERE vehicle_id = %s ORDER BY recorded_at",
                (truck_id,),
            )
            reading_ids = [r[0] for r in await cur.fetchall()]

        await delete(request, reading_id=reading_ids[1], user=USER)
        async with pool.connection() as conn:
            rows = await _reading_rows(conn, truck_id)
            context = await _fetch_odometer_context(conn)
        assert len(rows) == 1
        entry = next(e for e in context if e["vehicle"]["id"] == truck_id)
        # Fewer than two readings again: no intervals, not a crash.
        assert entry["intervals"] == []
    finally:
        await pool.close()


def test_add_and_delete_reading_recomputes_reconciliation():
    asyncio.run(_add_delete_scenario())


async def _fk_violation_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        add = _endpoint("/settings/odometer")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await add(
                request, vehicle_id=999999, date="2026-01-01", time="00:00",
                value=1000.0, note="", user=USER,
            )
        assert exc_info.value.status_code == 400
    finally:
        await pool.close()


def test_add_reading_unknown_vehicle_returns_400():
    asyncio.run(_fk_violation_scenario())


async def _invalid_input_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
        add = _endpoint("/settings/odometer")
        request = _request(pool)

        with pytest.raises(HTTPException) as bad_value:
            await add(
                request, vehicle_id=truck_id, date="2026-01-01", time="00:00",
                value=0, note="", user=USER,
            )
        assert bad_value.value.status_code == 400

        with pytest.raises(HTTPException) as bad_date:
            await add(
                request, vehicle_id=truck_id, date="not-a-date", time="00:00",
                value=1000.0, note="", user=USER,
            )
        assert bad_date.value.status_code == 400
    finally:
        await pool.close()


def test_add_reading_rejects_invalid_value_and_date():
    asyncio.run(_invalid_input_scenario())


async def _duplicate_reading_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
        add = _endpoint("/settings/odometer")
        request = _request(pool)
        await add(
            request, vehicle_id=truck_id, date="2026-01-01", time="00:00",
            value=1000.0, note="", user=USER,
        )
        with pytest.raises(HTTPException) as exc_info:
            await add(
                request, vehicle_id=truck_id, date="2026-01-01", time="00:00",
                value=1000.0, note="", user=USER,
            )
        assert exc_info.value.status_code == 400
    finally:
        await pool.close()


def test_add_duplicate_reading_same_vehicle_and_instant_returns_400():
    asyncio.run(_duplicate_reading_scenario())


async def _report_page_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            await _insert_trip(
                conn, truck_id, datetime(2026, 3, 15, 12, tzinfo=timezone.utc), 60 * 1609.344
            )
            await conn.execute(
                "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m) "
                "VALUES (%s, %s, %s), (%s, %s, %s)",
                (
                    truck_id, datetime(2026, 3, 1, tzinfo=timezone.utc), 1000 * 1609.344,
                    truck_id, datetime(2026, 9, 1, tzinfo=timezone.utc), 1100 * 1609.344,
                ),
            )

        report_page = _endpoint("/report/{year}")
        request = _request(pool)
        response = await report_page(request, year=2026, user=USER)
        body = response.body.decode()
        assert "Odometer coverage" in body
        assert "GPS captured 60.0% of odometer miles (40.0 mi unaccounted)" in body

        report_export = _endpoint("/report/{year}/export")
        export_response = await report_export(request, year=2026, user=USER)

        from io import BytesIO

        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(export_response.body))
        summary_text = " ".join(
            str(c.value) for row in wb["Summary"].iter_rows() for c in row if c.value
        )
        assert "Odometer reconciliation" in summary_text
        assert "60.0%" in summary_text
    finally:
        await pool.close()


def test_report_page_and_export_include_odometer_coverage_when_readings_exist():
    asyncio.run(_report_page_scenario())


async def _report_page_no_readings_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            await _insert_trip(
                conn, truck_id, datetime(2026, 3, 15, 12, tzinfo=timezone.utc), 60 * 1609.344
            )

        report_page = _endpoint("/report/{year}")
        request = _request(pool)
        response = await report_page(request, year=2026, user=USER)
        body = response.body.decode()
        assert "Odometer coverage" not in body
    finally:
        await pool.close()


def test_report_page_omits_odometer_section_with_no_readings():
    asyncio.run(_report_page_no_readings_scenario())


async def _all_categories_counted_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
            # One of each category, all inside the [r1, r2) interval.
            # detected_m must sum all three (the expense report's total-miles figure needs
            # the odometer to reconcile against *all* driving, not just the
            # business slice the report's own business/personal split uses).
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, vehicle_id, category) "
                "VALUES "
                "('manual', 'manual', %(t)s, %(t)s + interval '15 minutes', %(business)s, %(v)s, 'business'), "
                "('manual', 'manual', %(t)s, %(t)s + interval '15 minutes', %(personal)s, %(v)s, 'personal'), "
                "('manual', 'manual', %(t)s, %(t)s + interval '15 minutes', %(unclassified)s, %(v)s, 'unclassified')",
                {
                    "t": datetime(2026, 1, 5, 12, tzinfo=timezone.utc),
                    "business": 20 * 1609.344,
                    "personal": 15 * 1609.344,
                    "unclassified": 10 * 1609.344,
                    "v": truck_id,
                },
            )
            await conn.execute(
                "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m) "
                "VALUES (%s, %s, %s), (%s, %s, %s)",
                (
                    truck_id, datetime(2026, 1, 1, tzinfo=timezone.utc), 1000 * 1609.344,
                    truck_id, datetime(2026, 1, 10, tzinfo=timezone.utc), 1100 * 1609.344,
                ),
            )

        from app.ui import _fetch_odometer_context
        async with pool.connection() as conn:
            context = await _fetch_odometer_context(conn)
        entry = next(e for e in context if e["vehicle"]["id"] == truck_id)
        assert len(entry["intervals"]) == 1
        iv = entry["intervals"][0]
        # Sum of all three categories' distances, not just the 20mi business trip.
        assert iv.detected_m == pytest.approx((20 + 15 + 10) * 1609.344)
    finally:
        await pool.close()


def test_reconciliation_detected_distance_counts_all_trip_categories():
    asyncio.run(_all_categories_counted_scenario())
