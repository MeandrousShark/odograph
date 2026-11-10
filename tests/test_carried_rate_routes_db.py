"""Routes pass the carried-rate state through to the fragments they render:
the archive's reclassification response, the Settings rates save and the
dashboard. Route handlers are called directly, same pattern as
tests/test_trip_tag_rollup_db.py."""
from __future__ import annotations

import asyncio
import functools
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.account_context import account_id
from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from conftest import reset_account_db
from personal_support import personal_request

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("UTC")
USER = {"sub": "test"}


@functools.cache
def _routes():
    return tuple(make_router().routes)


def _endpoint(path: str, method: str):
    for route in _routes():
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} missing")


def _request(pool, current_url: str = ""):
    config = SimpleNamespace(
        display_tz=TZ, trips_page_size=25, app_version="test", missing_trip_gap_m=1000.0,
    )
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        headers={"HX-Current-URL": current_url} if current_url else {},
    ))


def _scenario(coro) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            await coro(pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


def _sentence(year: int, source_year: int, rate: str) -> str:
    return (
        f"No {year} mileage rate is saved. {year} figures use the {source_year} "
        f"rate of ${rate}/mi until you add one."
    )


async def _only_rate(conn, year: int, rate: str) -> None:
    await conn.execute("DELETE FROM mileage_rates WHERE account_id = %s", (account_id(conn),))
    await conn.execute(
        "INSERT INTO mileage_rates (account_id, year, rate_per_mi) VALUES (%s, %s, %s)",
        (account_id(conn), year, rate),
    )


async def _insert_trip(conn, started_at: datetime, category: str, distance_m: float = 1609.344) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category)"
        " VALUES (%s, 'CARRIED', 'manual', %s, %s, %s, %s) RETURNING id",
        (account_id(conn), started_at, started_at + timedelta(hours=1), distance_m, category),
    )
    return (await cur.fetchone())[0]


def test_reclassification_response_carries_the_note_in_month_summary_and_year_to_date():
    async def run(pool):
        year = datetime.now(TZ).year
        month = 1
        async with pool.connection() as conn:
            await _only_rate(conn, year - 1, "0.7000")
            target = await _insert_trip(conn, datetime(year, month, 10, 9, tzinfo=TZ), "unclassified")
            await _insert_trip(conn, datetime(year, month, 12, 9, tzinfo=TZ), "business")

        tag = _endpoint("/trips/{trip_id}/tag", "POST")
        response = await tag(_request(pool, "http://testserver/trips"), target, "business", USER)
        body = response.body.decode()
        assert response.status_code == 200

        month_fragment = body[body.index(f'id="month-summary-{year}-{month}"'):]
        month_fragment = month_fragment[:month_fragment.index("</div>")]
        assert f"$1.40 deduction ({year - 1} rate)" in month_fragment

        ytd_fragment = body[body.index('id="trip-archive-ytd"'):]
        assert _sentence(year, year - 1, "0.7000") in ytd_fragment
        assert f'<a href="/settings">Add the {year} rate</a>.' in ytd_fragment

    _scenario(run)


def test_reclassification_response_has_no_note_when_the_year_has_its_rate():
    async def run(pool):
        year = datetime.now(TZ).year
        async with pool.connection() as conn:
            await _only_rate(conn, year, "0.7000")
            target = await _insert_trip(conn, datetime(year, 1, 10, 9, tzinfo=TZ), "unclassified")

        tag = _endpoint("/trips/{trip_id}/tag", "POST")
        body = (await tag(_request(pool, "http://testserver/trips"), target, "business", USER)).body.decode()
        assert "mileage rate is saved" not in body
        assert " rate)" not in body

    _scenario(run)


def test_saving_the_current_year_rate_removes_the_settings_note():
    async def run(pool):
        year = datetime.now(TZ).year
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM mileage_rates WHERE account_id = %s", (account_id(conn),))
        upsert = _endpoint("/settings/rates", "POST")

        before = await upsert(
            _request(pool), year=year - 1, rate_per_mi=0.7, mid_year="", rate_h2_per_mi="",
            h2_start_month=7, user=USER,
        )
        body = before.body.decode()
        assert _sentence(year, year - 1, "0.7000") in body
        assert "Add the" not in body

        after = await upsert(
            _request(pool), year=year, rate_per_mi=0.725, mid_year="", rate_h2_per_mi="",
            h2_start_month=7, user=USER,
        )
        assert "mileage rate is saved" not in after.body.decode()

    _scenario(run)


def test_dashboard_response_carries_the_note_for_a_carried_week():
    async def run(pool):
        async with pool.connection() as conn:
            await _only_rate(conn, 2025, "0.7000")
            await _insert_trip(conn, datetime(2026, 6, 2, 9, tzinfo=TZ), "business")

        dashboard = _endpoint("/", "GET")
        response = await dashboard(_request(pool), USER, "2026-06-01")
        assert response.context["dashboard"].deduction.carried.source_year == 2025
        body = response.body.decode()
        assert _sentence(2026, 2025, "0.7000") in body
        assert '<a href="/settings">Add the 2026 rate</a>.' in body

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO mileage_rates (account_id, year, rate_per_mi) VALUES (%s, 2026, 0.7250)",
                (account_id(conn),),
            )
        response = await dashboard(_request(pool), USER, "2026-06-01")
        assert response.context["dashboard"].deduction.carried is None
        assert "mileage rate is saved" not in response.body.decode()

    _scenario(run)
