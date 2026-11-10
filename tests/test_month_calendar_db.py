"""GET /calendar against a real database: row security, the exclusion and
month-bound rules, and agreement with the dashboard's week chart. Route
handlers are called directly, same pattern as tests/test_place_pages_db.py."""
from __future__ import annotations

import asyncio
import functools
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.account_context import account_id
from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from conftest import add_test_account, reset_account_db
from personal_support import personal_request

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}
MILE = 1609.344
OTHER = 99


@functools.cache
def _routes():
    return tuple(make_router().routes)


def _endpoint(path: str):
    for route in _routes():
        if getattr(route, "path", None) == path and "GET" in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route GET {path} missing")


def _request(pool):
    config = SimpleNamespace(
        display_tz=TZ, trips_page_size=25, app_version="test", missing_trip_gap_m=1000.0,
    )
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        headers={},
        url=SimpleNamespace(path="/calendar"),
    ))


def _scenario(coro) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            await coro(raw_pool, pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


async def _trip(conn, started_at, meters, category="business", exclusion=None) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
        " category, exclusion) VALUES (%s, 'CAL', 'manual', %s, %s, %s, %s, %s) RETURNING id",
        (account_id(conn), started_at, started_at + timedelta(minutes=20), meters,
         category, exclusion),
    )
    return (await cur.fetchone())[0]


async def _calendar(pool, month: str):
    response = await _endpoint("/calendar")(_request(pool), USER, month)
    assert response.status_code == 200
    model = response.context["calendar"]
    return {cell.day: cell for week in model.weeks for cell in week}, response.body.decode()


def test_another_accounts_trips_never_appear():
    async def run(raw_pool, pool):
        other = await add_test_account(raw_pool, OTHER)
        day = datetime(2025, 5, 14, 9, tzinfo=TZ)
        async with other.connection() as conn:
            await _trip(conn, day, 40 * MILE)
        async with pool.connection() as conn:
            await _trip(conn, day, 3 * MILE)

        ours, body = await _calendar(pool, "2025-05")
        assert round(ours[date(2025, 5, 14)].distance.total_m / MILE, 6) == 3.0
        assert "43.0" not in body and "40.0" not in body
        theirs, _ = await _calendar(other, "2025-05")
        assert round(theirs[date(2025, 5, 14)].distance.total_m / MILE, 6) == 40.0

    _scenario(run)


def test_not_my_vehicle_is_left_out_and_not_deductible_counts_as_nondeductible():
    async def run(raw_pool, pool):
        day = datetime(2025, 5, 14, 9, tzinfo=TZ)
        async with pool.connection() as conn:
            await _trip(conn, day, 2 * MILE, "business")
            await _trip(conn, day, 5 * MILE, "business", "not_my_vehicle")
            await _trip(conn, day, 7 * MILE, "business", "not_deductible")
        days, body = await _calendar(pool, "2025-05")
        entry = days[date(2025, 5, 14)].distance
        assert round(entry.business_m / MILE, 6) == 2.0
        assert round(entry.nondeductible_m / MILE, 6) == 7.0
        assert round(entry.total_m / MILE, 6) == 9.0
        assert 'aria-label="Wednesday, May 14: 9.0 miles total. Business 2.0 miles; Non-deductible 7.0 miles."' in body

    _scenario(run)


def test_month_bounds_are_local_midnights():
    async def run(raw_pool, pool):
        async with pool.connection() as conn:
            await _trip(conn, datetime(2025, 1, 31, 23, 30, tzinfo=TZ), 1 * MILE)  # Feb 1 in UTC
            await _trip(conn, datetime(2025, 2, 1, 0, 10, tzinfo=TZ), 10 * MILE)
            await _trip(conn, datetime(2025, 2, 28, 23, 30, tzinfo=TZ), 100 * MILE)
            await _trip(conn, datetime(2025, 3, 1, 0, 10, tzinfo=TZ), 1000 * MILE)
        january, _ = await _calendar(pool, "2025-01")
        assert round(january[date(2025, 1, 31)].distance.total_m / MILE, 6) == 1.0
        february, body = await _calendar(pool, "2025-02")
        assert round(february[date(2025, 2, 1)].distance.total_m / MILE, 6) == 10.0
        assert round(february[date(2025, 2, 28)].distance.total_m / MILE, 6) == 100.0
        assert february[date(2025, 1, 31)].distance is None
        assert february[date(2025, 3, 1)].distance is None
        assert "1000.0" not in body and "1,000.0" not in body
        march, _ = await _calendar(pool, "2025-03")
        assert round(march[date(2025, 3, 1)].distance.total_m / MILE, 6) == 1000.0

    _scenario(run)


def test_each_day_equals_the_dashboards_day():
    async def run(raw_pool, pool):
        async with pool.connection() as conn:
            # The week of Sep 29 2025 spans September and October.
            for hour, meters, category, exclusion in (
                (1, 1234.567, "business", None), (9, 89.01, "personal", None),
                (13, 4567.891, "unclassified", None), (17, 3210.98, "personal", "not_deductible"),
                (23, 777.7, "business", "not_my_vehicle"),
            ):
                for offset in range(7):
                    start = datetime(2025, 9, 29, hour, 5 * offset, tzinfo=TZ) + timedelta(days=offset)
                    await _trip(conn, start, meters * (offset + 1), category, exclusion)
        week = await _endpoint("/")(_request(pool), USER, "2025-10-01")
        series = week.context["dashboard"].daily_series
        september, _ = await _calendar(pool, "2025-09")
        october, _ = await _calendar(pool, "2025-10")
        for entry in series:
            days = september if entry.day.month == 9 else october
            assert days[entry.day].in_month
            assert days[entry.day].distance == entry
            assert entry.total_m > 0

    _scenario(run)


def _added(values) -> float:
    # Plain left-to-right addition, as the day buckets do; sum() compensates
    # float error and would hide the order.
    total = 0.0
    for value in values:
        total += value
    return total


def test_a_day_sums_its_trips_in_the_dashboards_order():
    meters = (0.1, 0.2, 0.3)
    # Only equal when added in the same order, newest first as the
    # dashboard reads them.
    assert _added(reversed(meters)) != _added(meters)

    async def run(raw_pool, pool):
        async with pool.connection() as conn:
            for hour, distance in zip((1, 2, 3), meters):
                await _trip(conn, datetime(2025, 10, 1, hour, tzinfo=TZ), distance)
        week = await _endpoint("/")(_request(pool), USER, "2025-10-01")
        day = next(e for e in week.context["dashboard"].daily_series if e.day == date(2025, 10, 1))
        october, _ = await _calendar(pool, "2025-10")
        assert day.business_m == _added(reversed(meters))
        assert october[date(2025, 10, 1)].distance == day

    _scenario(run)


def test_default_month_is_the_current_local_month():
    async def run(raw_pool, pool):
        today = datetime.now(TZ).date()
        days, body = await _calendar(pool, "")
        assert days[today].is_today and days[today].in_month
        assert 'aria-label="Next month unavailable"' in body

    _scenario(run)
