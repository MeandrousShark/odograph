"""The report projection omits continuity badges without changing report data."""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook
from psycopg.rows import dict_row

from app.account_context import account_id
from app.db import make_pool
from app.export import to_range_report_xlsx, to_report_xlsx
from app.report import build_annual_report, build_range_report
from app.ui import make_router
from app.ui._common import TRIP_COLUMNS
import app.ui.reports as reports
from conftest import reset_account_db, seed_tracking_device
from personal_support import personal_request

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
BADGE_FIELDS = {
    "prev_end_gap_m", "prev_trip_ended_at", "prev_trip_end_lat",
    "prev_trip_end_lon", "prev_trip_end_place_name", "missing_trip_covered",
}


async def _seed(conn):
    owner = account_id(conn)
    stream = await seed_tracking_device(conn)
    vehicle = (await (await conn.execute(
        "INSERT INTO vehicles(account_id,name,active) VALUES (%s,'Retired truck',false) RETURNING id",
        (owner,),
    )).fetchone())[0]
    place = (await (await conn.execute(
        "INSERT INTO places(account_id,name,geom) "
        "VALUES (%s,'Office',ST_GeogFromText('POINT(-122.3 47.6)')) RETURNING id",
        (owner,),
    )).fetchone())[0]
    await conn.execute(
        "INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES (%s,47.7,-122.4,'Client address')",
        (owner,),
    )

    async def trip(month, day, hour, *, source="detected", category="business", exclusion=None,
                   snapped=None, has_gap=False, labels=False):
        start = datetime(2026, month, day, hour, tzinfo=TZ)
        cur = await conn.execute(
            "INSERT INTO trips(account_id,device,tracking_device_id,source,started_at,ended_at,"
            "distance_m,distance_snapped_m,snap_status,category,exclusion,vehicle_id,"
            "start_place_id,start_geom,end_geom,start_label,end_label,has_gap,purpose,notes) "
            "VALUES (%s,'phone',%s,%s,%s,%s,1609.344,%s,%s,%s,%s,%s,%s,"
            "CASE WHEN %s THEN NULL ELSE ST_GeogFromText('POINT(-122.3 47.6)') END,"
            "CASE WHEN %s THEN NULL ELSE ST_GeogFromText('POINT(-122.4 47.7)') END,"
            "%s,%s,%s,'Client visit','Audit detail') RETURNING id",
            (owner, stream if source == "detected" else None, source, start,
             start + timedelta(minutes=30), snapped, "ok" if source == "detected" else None,
             category, exclusion, vehicle, None if labels else place, labels, labels,
             "Manual start" if labels else None, "Manual end" if labels else None, has_gap),
        )
        return (await cur.fetchone())[0]

    predecessor = await trip(4, 15, 8)
    await conn.execute("UPDATE trips SET end_place_id = %s WHERE id = %s AND account_id = %s",
                       (place, predecessor, owner))
    manual = await trip(4, 15, 9, source="manual", labels=True)
    covered = await trip(4, 15, 10, snapped=3218.688)
    await trip(4, 16, 9, source="manual", exclusion="not_my_vehicle", labels=True)
    uncovered = await trip(4, 16, 10, has_gap=True)
    await trip(5, 1, 10, category="personal")
    await trip(6, 1, 10, category="unclassified")
    excluded = await trip(6, 15, 10, exclusion="not_deductible")
    await trip(6, 16, 10, exclusion="not_my_vehicle")
    await trip(7, 1, 10)
    for linked in (covered, excluded):
        await conn.execute(
            "INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment,trip_id,notes) "
            "VALUES (%s,%s,'2026-06-15','parking',12.50,'fully_business',%s,'Receipt')",
            (owner, vehicle, linked),
        )
    await conn.execute(
        "INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment) "
        "VALUES (%s,%s,'2026-06-15','fuel',100,'business_use_allocated')",
        (owner, vehicle),
    )
    for year, meters in ((2026, 100000), (2027, 200000)):
        await conn.execute(
            "INSERT INTO odometer_readings(account_id,vehicle_id,recorded_at,odometer_m) VALUES (%s,%s,%s,%s)",
            (owner, vehicle, datetime(year, 1, 1, tzinfo=TZ), meters),
        )
    return predecessor, manual, covered, uncovered


async def _full_rows(conn, start, end):
    cur = conn.cursor(row_factory=dict_row)
    next_day = end + timedelta(days=1)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips WHERE started_at >= %s AND started_at < %s "
        "AND account_id = %s ORDER BY started_at",
        (datetime(start.year, start.month, start.day, tzinfo=TZ),
         datetime(next_day.year, next_day.month, next_day.day, tzinfo=TZ), account_id(conn)),
    )
    return await cur.fetchall()


def _workbook_cells(content):
    workbook = load_workbook(BytesIO(content))
    return {sheet.title: list(sheet.values) for sheet in workbook.worksheets}


async def _equivalence_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            predecessor, manual, covered, uncovered = await _seed(conn)
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        async with pool.connection() as conn:
            full = await _full_rows(conn, start, end)
            projected, rates = await reports._fetch_range_trips_in(conn, TZ, start, end)
        assert projected == [{key: value for key, value in trip.items() if key not in BADGE_FIELDS}
                             for trip in full]
        assert set(full[0]) - set(projected[0]) == BADGE_FIELDS
        by_id = {trip["id"]: trip for trip in full}
        assert by_id[covered]["missing_trip_covered"] is True
        assert by_id[uncovered]["missing_trip_covered"] is False
        assert by_id[covered]["prev_trip_ended_at"] == by_id[predecessor]["ended_at"]
        assert by_id[covered]["prev_trip_end_place_name"] == "Office"
        assert by_id[covered]["prev_end_gap_m"] > 0
        assert by_id[manual]["start_place_name"] == "Manual start"
        assert by_id[covered]["end_address"] == "Client address"
        assert by_id[covered]["vehicle_name"] == "Retired truck"
        assert by_id[covered]["expense_count"] == 1
        assert by_id[covered]["display_distance_m"] == by_id[covered]["distance_snapped_m"]
        assert by_id[covered]["display_distance_m"] > by_id[covered]["distance_m"]

        annual_full = build_annual_report(full, rates, TZ, 2026)
        annual_projected = build_annual_report(projected, rates, TZ, 2026)
        assert annual_full == annual_projected
        coverage_full = await reports._fetch_year_odometer_coverage(pool, TZ, 2026, full)
        coverage_projected = await reports._fetch_year_odometer_coverage(pool, TZ, 2026, projected)
        assert coverage_full == coverage_projected
        assert coverage_full
        expenses, expense_full = await reports._fetch_year_expense_report(pool, TZ, 2026, full, rates)
        projected_expenses, expense_projected = await reports._fetch_year_expense_report(
            pool, TZ, 2026, projected, rates,
        )
        assert expenses == projected_expenses
        assert expense_full == expense_projected
        assert _workbook_cells(to_report_xlsx(
            annual_full, full, rates, TZ, coverage_full, expense_full, expenses,
        )) == _workbook_cells(to_report_xlsx(
            annual_projected, projected, rates, TZ, coverage_projected, expense_projected, projected_expenses,
        ))

        quarter_start, quarter_end = date(2026, 4, 1), date(2026, 6, 30)
        async with pool.connection() as conn:
            range_full = await _full_rows(conn, quarter_start, quarter_end)
            range_projected, range_rates = await reports._fetch_range_trips_in(
                conn, TZ, quarter_start, quarter_end,
            )
        ranged_full = build_range_report(range_full, range_rates, TZ, quarter_start, quarter_end)
        ranged_projected = build_range_report(range_projected, range_rates, TZ, quarter_start, quarter_end)
        assert ranged_full == ranged_projected
        assert _workbook_cells(to_range_report_xlsx(ranged_full, range_full, range_rates, TZ)) == (
            _workbook_cells(to_range_report_xlsx(ranged_projected, range_projected, range_rates, TZ))
        )
    finally:
        await raw_pool.close()


def test_report_projection_preserves_rows_reports_expenses_odometer_and_workbook_cells():
    asyncio.run(_equivalence_scenario())


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_ordinary_export_retains_full_trip_projection(monkeypatch, format):
    captured = []
    serializer = getattr(reports, f"to_{format}")

    def capture(trips, rates, tz):
        captured.extend(trips)
        return serializer(trips, rates, tz)

    monkeypatch.setattr(reports, f"to_{format}", capture)

    async def scenario():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            async with pool.connection() as conn:
                await _seed(conn)
                full = await _full_rows(conn, date(2026, 1, 1), date(2026, 12, 31))
                _, rates = await reports._fetch_range_trips_in(conn, TZ, date(2026, 1, 1), date(2026, 12, 31))
            request = personal_request(SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(pool=pool, config=SimpleNamespace(display_tz=TZ))),
            ))
            export = next(route.endpoint for route in make_router().routes if route.path == "/export")
            response = await export(
                request, user={"sub": "test"}, format=format, category="", from_="2026-01-01",
                to="2026-12-31", vehicle="", q="", exclusion="",
            )
            assert response.status_code == 200
            assert captured == list(reversed(full))
            assert BADGE_FIELDS <= set(captured[0])
            expected = serializer(list(reversed(full)), rates, TZ)
            if format == "csv":
                assert response.body == expected
            else:
                assert _workbook_cells(response.body) == _workbook_cells(expected)
        finally:
            await raw_pool.close()

    asyncio.run(scenario())
