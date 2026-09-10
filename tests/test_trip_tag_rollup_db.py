"""DB-backed tests for the archive's inline-classify write contract.

Reclassifying a trip from `/trips` can move two figures elsewhere on the
page: the affected month's summary line and the year-to-date deduction in
the archive header. `POST /trips/{trip_id}/tag` marks the committed write so
the archive coordinator can fetch the canonical filtered view and summaries.
Route handler called directly, bypassing FastAPI's dependency injection, same
pattern as tests/test_vehicle_filter_db.py.

tests/test_dashboard_db.py's test_dashboard_tag_rebuilds_past_week_card_and_hero_consistently
already covers this route's `dashboard_week` branch in full; this file
only adds one smoke assertion confirming that branch is untouched.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.dashboard import week_bounds
from app.db import make_pool
from app.formatting import format_miles, format_usd
from app.main import make_templates
from app.rates import deduction as calc_deduction, load_rates
from app.report import sum_month_deductions
from app.ui import make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("UTC")


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} missing")


TAG = _endpoint("/trips/{trip_id}/tag", "POST")


def _request(pool, current_url: str = ""):
    config = SimpleNamespace(
        display_tz=TZ, trips_page_size=25, app_version="test",
        missing_trip_gap_m=1000.0,
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        headers={"HX-Current-URL": current_url} if current_url else {},
    )


async def _insert_trip(
    conn, started_at: datetime, category: str = "unclassified", distance_m: float = 1000.0,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category) "
        "VALUES ('TAGROLL', 'manual', %s, %s, %s, %s) RETURNING id",
        (started_at, started_at + timedelta(hours=1), distance_m, category),
    )
    return (await cur.fetchone())[0]


def _scenario(coro) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await reset_db(pool)
            await coro(pool)
        finally:
            await pool.close()

    asyncio.run(run())


async def _seed_month(pool):
    """One month with a to-be-reclassified trip and an already-business
    sibling, plus a business trip in a different month of the same year so
    the year-to-date figure differs from the single month's own total.
    Returns (year, month, target_id, sibling_id, other_month, other_month_id).
    """
    now = datetime.now(TZ)
    year = now.year
    month = 6
    other_month = 3
    async with pool.connection() as conn:
        # Deterministic regardless of what migrations/002 happens to seed
        # for this year, and regardless of what year the test actually runs in.
        await conn.execute(
            "INSERT INTO mileage_rates (year, rate_per_mi) VALUES (%s, %s) "
            "ON CONFLICT (year) DO UPDATE SET rate_per_mi = EXCLUDED.rate_per_mi",
            (year, "0.6700"),
        )
        target_id = await _insert_trip(
            conn, datetime(year, month, 10, 9, tzinfo=TZ),
            category="unclassified", distance_m=1000.0,
        )
        sibling_id = await _insert_trip(
            conn, datetime(year, month, 12, 9, tzinfo=TZ),
            category="business", distance_m=2000.0,
        )
        other_month_id = await _insert_trip(
            conn, datetime(year, other_month, 10, 9, tzinfo=TZ),
            category="business", distance_m=5000.0,
        )
    return year, month, target_id, sibling_id, other_month, other_month_id


def test_inline_classify_returns_row_plus_month_and_ytd_oob_fragments():
    async def run(pool):
        year, month, target_id, sibling_id, other_month, _ = await _seed_month(pool)

        request = _request(pool, current_url="http://testserver/trips")
        response = await TAG(request, target_id, "business", {"sub": "test"})
        body = response.body.decode()

        assert response.status_code == 200
        assert response.headers["X-Archive-Write"] == "success"
        assert f'<article id="trip-{target_id}"' in body
        assert body.count('hx-swap-oob="outerHTML"') == 2
        assert f'id="month-summary-{year}-{month}"' in body
        assert 'id="trip-archive-ytd"' in body

        async with pool.connection() as conn:
            rates = await load_rates(conn)
        month_business_m = 3000.0  # sibling 2000 + reclassified target 1000
        month_deduction = calc_deduction(month_business_m, year, rates, month)
        ytd_deduction = sum_month_deductions(
            [(month, month_business_m), (other_month, 5000.0)], year, rates,
        )

        month_fragment = body[body.index(f'id="month-summary-{year}-{month}"'):]
        assert "2 trips" in month_fragment
        assert f"{format_miles(month_business_m)} mi total" in month_fragment
        assert f"{format_miles(month_business_m)} mi business" in month_fragment
        assert f"{format_usd(month_deduction)} deduction" in month_fragment

        ytd_fragment = body[body.index('id="trip-archive-ytd"'):]
        assert f"Year-to-date ({year}) business deduction" in ytd_fragment
        assert format_usd(ytd_deduction) in ytd_fragment

    _scenario(run)


def test_inline_classify_ignores_vehicle_date_and_search_filters_for_the_refresh_decision():
    # Only category and exclusion can change which filtered set a trip
    # belongs to; every other filter (vehicle, date range, search) is
    # unaffected by a category change, so an active one of those must still
    # take the normal out-of-band path, not a full refresh.
    async def run(pool):
        year, month, target_id, _, _, _ = await _seed_month(pool)

        request = _request(
            pool,
            current_url="http://testserver/trips?from=2020-01-01&to=2030-01-01&vehicle=none",
        )
        response = await TAG(request, target_id, "business", {"sub": "test"})

        assert response.status_code == 200
        assert response.headers["X-Archive-Write"] == "success"
        assert "HX-Refresh" not in response.headers
        body = response.body.decode()
        assert body.count('hx-swap-oob="outerHTML"') == 2

    _scenario(run)


def test_inline_classify_with_active_category_filter_forces_a_full_refresh():
    async def run(pool):
        _, _, target_id, _, _, _ = await _seed_month(pool)

        request = _request(pool, current_url="http://testserver/trips?category=unclassified")
        response = await TAG(request, target_id, "business", {"sub": "test"})

        assert response.status_code == 204
        assert response.headers["X-Archive-Write"] == "success"
        assert response.body == b""

    _scenario(run)


def test_inline_classify_with_active_exclusion_filter_forces_a_full_refresh():
    async def run(pool):
        _, _, target_id, _, _, _ = await _seed_month(pool)

        request = _request(pool, current_url="http://testserver/trips?exclusion=not_my_vehicle")
        response = await TAG(request, target_id, "business", {"sub": "test"})

        assert response.status_code == 204
        assert response.headers["X-Archive-Write"] == "success"
        assert response.body == b""

    _scenario(run)


def test_inline_classify_degrades_to_a_plain_row_without_usable_page_context():
    # No HX-Current-URL at all (a non-htmx caller, or one predating this
    # feature): the handler cannot know whether a filter is active or
    # recompute figures against unknown context, so it must not guess.
    async def run(pool):
        _, _, target_id, _, _, _ = await _seed_month(pool)

        request = _request(pool)  # no current_url
        response = await TAG(request, target_id, "business", {"sub": "test"})
        body = response.body.decode()

        assert response.status_code == 200
        assert response.headers["X-Archive-Write"] == "success"
        assert "HX-Refresh" not in response.headers
        assert f'<article id="trip-{target_id}"' in body
        assert "hx-swap-oob" not in body

    _scenario(run)


def test_inline_classify_degrades_to_a_plain_row_for_a_current_url_off_the_archive():
    async def run(pool):
        _, _, target_id, _, _, _ = await _seed_month(pool)

        request = _request(pool, current_url="http://testserver/review")
        response = await TAG(request, target_id, "business", {"sub": "test"})
        body = response.body.decode()

        assert response.status_code == 200
        assert response.headers["X-Archive-Write"] == "success"
        assert "HX-Refresh" not in response.headers
        assert f'<article id="trip-{target_id}"' in body
        assert "hx-swap-oob" not in body

    _scenario(run)


def test_dashboard_tag_branch_is_unchanged_by_the_archive_rollup_logic():
    # Not a full re-test of the dashboard branch (see
    # tests/test_dashboard_db.py for that); just confirms the shared helpers
    # this file's changes introduced did not leak into or alter it.
    async def run(pool):
        bounds = week_bounds(datetime.now(TZ).date(), TZ)
        async with pool.connection() as conn:
            target_id = await _insert_trip(
                conn, bounds.start + timedelta(hours=1), category="unclassified",
            )

        request = _request(pool)
        response = await TAG(
            request, target_id, "business", {"sub": "test"}, dashboard_week=bounds.start.date().isoformat(),
        )
        body = response.body.decode()

        assert response.status_code == 200
        assert "HX-Refresh" not in response.headers
        assert body.count('hx-swap-oob="outerHTML"') == 1
        assert body.count('id="dashboard-hero"') == 1
        assert 'id="trip-archive-ytd"' not in body
        assert "month-summary-" not in body

    _scenario(run)
