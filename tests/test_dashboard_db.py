"""DB-backed tests for `GET /`: the weekly
dashboard's query wiring around the pure `app.dashboard` model
(`tests/test_dashboard.py` covers that model itself; `tests/test_dashboard_template.py`
covers rendering). Route handler called directly, bypassing FastAPI's
dependency injection, same pattern as `tests/test_missing_trip_index_db.py`
-- the one exception is the unauthenticated-redirect case, which needs a
real ASGI request for `require_user` to actually fire.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.auth import AuthRedirect
from app.dashboard import week_bounds
from app.db import make_pool
from app.account_context import account_id
from personal_support import configure_personal_app, fixture_device, personal_request
from app.main import make_templates
from app.ui import make_router
from app.vehicles import create_vehicle
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

UTC = ZoneInfo("UTC")
LA = ZoneInfo("America/Los_Angeles")


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/" and "GET" in (route.methods or set()):
            return route.endpoint
    raise AssertionError("dashboard route missing")


DASHBOARD = _endpoint()


def _tag_endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/{trip_id}/tag" and "POST" in (route.methods or set()):
            return route.endpoint
    raise AssertionError("dashboard tag route missing")


TAG = _tag_endpoint()


def _request(pool, tz: ZoneInfo = UTC, missing_trip_gap_m: float = 1000.0):
    config = SimpleNamespace(
        display_tz=tz, missing_trip_gap_m=missing_trip_gap_m, app_version="test",
    )
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        headers={},
    ))


async def _call_dashboard(pool, week: str = "", tz: ZoneInfo = UTC):
    response = await DASHBOARD(_request(pool, tz=tz), {"sub": "test"}, week)
    return response.context["dashboard"]


async def _insert_detected_trip(
    conn, started_at: datetime, ended_at: datetime, category: str = "unclassified",
    device: str = "DASH", distance_m: float = 1000.0, vehicle_id: int | None = None,
    exclusion: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, "
        "distance_m,  point_count, detector_version, category, vehicle_id, exclusion) VALUES (%s, "
        "%s, %s, 'detected', %s, %s, %s, 2, 2, %s, %s, %s) RETURNING id",
        (
            account_id(conn),
            await fixture_device(conn, device),
            device,
            started_at,
            ended_at,
            distance_m,
            category,
            vehicle_id,
            exclusion,
        ),
    )
    return (await cur.fetchone())[0]


async def _insert_expense(conn, vehicle_id: int, incurred_on: date, amount: str) -> int:
    cur = await conn.execute(
        "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment) "
        "VALUES (%s, %s, %s, 'fuel', %s, 'fully_business') RETURNING id",
        (account_id(conn), vehicle_id, incurred_on, Decimal(amount),),
    )
    return (await cur.fetchone())[0]


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


def test_default_week_returns_only_current_week_trips():
    async def run(pool):
        now = datetime.now(UTC)
        bounds = week_bounds(now.date(), UTC)
        async with pool.connection() as conn:
            in_week = await _insert_detected_trip(
                conn, bounds.start + timedelta(hours=1), bounds.start + timedelta(hours=2),
            )
            await _insert_detected_trip(
                conn, bounds.start - timedelta(hours=2), bounds.start - timedelta(hours=1),
            )

        dashboard = await _call_dashboard(pool)
        assert dashboard.trip_count == 1
        assert [t["id"] for g in dashboard.day_groups for t in g.trips] == [in_week]

    _scenario(run)


def test_weekly_dashboard_keeps_not_my_vehicle_visible_while_excluding_it_from_metrics():
    async def run(pool):
        bounds = week_bounds(date(2026, 7, 13), UTC)
        async with pool.connection() as conn:
            business_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(hours=1), bounds.start + timedelta(hours=2),
                category="business", distance_m=1000.0,
            )
            not_my_vehicle_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(days=1),
                bounds.start + timedelta(days=1, hours=1),
                category="unclassified", distance_m=2000.0,
                exclusion="not_my_vehicle",
            )
            nondeductible_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(days=2),
                bounds.start + timedelta(days=2, hours=1),
                category="business", distance_m=3000.0,
                exclusion="not_deductible",
            )

        dashboard = await _call_dashboard(pool, week="2026-07-13")
        visible_ids = [trip["id"] for group in dashboard.day_groups for trip in group.trips]
        assert visible_ids == [nondeductible_id, not_my_vehicle_id, business_id]
        assert dashboard.trip_count == 2
        assert dashboard.distance.total_m == 4000.0
        assert dashboard.distance.business_m == 1000.0
        assert dashboard.distance.nondeductible_m == 3000.0
        assert dashboard.attention is not None
        assert dashboard.attention.unclassified_count == 1

    _scenario(run)


def test_dashboard_tag_rebuilds_past_week_card_and_hero_consistently():
    async def run(pool):
        bounds = week_bounds(date(2026, 7, 13), UTC)
        async with pool.connection() as conn:
            target_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(hours=1), bounds.start + timedelta(hours=2),
                category="unclassified", distance_m=1000.0,
            )
            not_my_vehicle_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(days=1),
                bounds.start + timedelta(days=1, hours=1),
                category="unclassified", distance_m=2000.0,
                exclusion="not_my_vehicle",
            )
            nondeductible_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(days=2),
                bounds.start + timedelta(days=2, hours=1),
                category="business", distance_m=3000.0,
                exclusion="not_deductible",
            )

        request = _request(pool)
        response = await TAG(
            request, target_id, "business", {"sub": "test"},
            dashboard_week="2026-07-13",
        )
        body = response.body.decode()
        refreshed = response.context["dashboard"]
        visible_ids = [trip["id"] for group in refreshed.day_groups for trip in group.trips]

        assert response.status_code == 200
        assert f'id="trip-{target_id}"' in body
        row = body[body.index(f'id="trip-{target_id}"'):]
        quick = row.split('class="trip-quick-actions"', 1)[1].split('</div>', 1)[0]
        business_button = quick.split('value="business"', 1)[1].split('</form>', 1)[0]
        personal_button = quick.split('value="personal"', 1)[1].split('</form>', 1)[0]
        assert 'trip-quick-button-selected' in business_button
        assert 'aria-pressed="true"' in business_button
        assert 'trip-quick-button-selected' not in personal_button
        assert 'aria-pressed' not in personal_button
        assert body.count('hx-swap-oob="outerHTML"') == 1
        assert body.count('id="dashboard-hero"') == 1
        assert visible_ids == [nondeductible_id, not_my_vehicle_id, target_id]
        assert refreshed.nav.week_start == date(2026, 7, 13)
        assert refreshed.trip_count == 2
        assert refreshed.distance.total_m == 4000.0
        assert refreshed.distance.business_m == 1000.0
        assert refreshed.distance.nondeductible_m == 3000.0
        assert refreshed.daily_series[0].business_m == 1000.0
        assert refreshed.daily_series[1].business_m == 0.0
        assert refreshed.daily_series[2].nondeductible_m == 3000.0
        assert refreshed.deduction.available
        assert refreshed.deduction.amount is not None
        assert refreshed.deduction.amount > 0.0
        assert refreshed.attention is not None
        assert refreshed.attention.unclassified_count == 1

        response = await TAG(
            request, target_id, "unclassified", {"sub": "test"},
            dashboard_week="2026-07-13",
        )
        reset_dashboard = response.context["dashboard"]
        assert reset_dashboard.trip_count == 2
        assert reset_dashboard.distance.business_m == 0.0
        assert reset_dashboard.deduction.amount == 0.0
        assert reset_dashboard.attention is not None
        assert reset_dashboard.attention.unclassified_count == 2
        assert not_my_vehicle_id in [
            trip["id"] for group in reset_dashboard.day_groups for trip in group.trips
        ]

        response = await TAG(
            request, not_my_vehicle_id, "business", {"sub": "test"},
            dashboard_week="2026-07-13",
        )
        nmv_body = response.body.decode()
        nmv_dashboard = response.context["dashboard"]
        assert response.status_code == 200
        assert f'id="trip-{not_my_vehicle_id}"' in nmv_body
        nmv_row = nmv_body[nmv_body.index(f'id="trip-{not_my_vehicle_id}"'):]
        nmv_quick = nmv_row.split('class="trip-quick-actions"', 1)[1].split('</div>', 1)[0]
        nmv_business_button = nmv_quick.split('value="business"', 1)[1].split('</form>', 1)[0]
        nmv_personal_button = nmv_quick.split('value="personal"', 1)[1].split('</form>', 1)[0]
        assert 'trip-quick-button-selected' in nmv_business_button
        assert 'aria-pressed="true"' in nmv_business_button
        assert 'trip-quick-button-selected' not in nmv_personal_button
        assert 'aria-pressed' not in nmv_personal_button
        assert nmv_body.count('hx-swap-oob="outerHTML"') == 1
        assert nmv_body.count('id="dashboard-hero"') == 1
        assert nmv_dashboard.nav.week_start == date(2026, 7, 13)
        assert nmv_dashboard.trip_count == 2
        assert nmv_dashboard.distance.total_m == 4000.0
        assert nmv_dashboard.distance.business_m == 0.0
        assert nmv_dashboard.distance.unclassified_m == 1000.0
        assert nmv_dashboard.distance.nondeductible_m == 3000.0
        assert nmv_dashboard.daily_series[0].business_m == 0.0
        assert nmv_dashboard.daily_series[0].unclassified_m == 1000.0
        assert nmv_dashboard.daily_series[1].business_m == 0.0
        assert nmv_dashboard.daily_series[1].unclassified_m == 0.0
        assert nmv_dashboard.daily_series[2].nondeductible_m == 3000.0
        assert nmv_dashboard.deduction.amount == 0.0
        assert nmv_dashboard.attention is not None
        assert nmv_dashboard.attention.unclassified_count == 1
        assert nmv_dashboard.attention.missing_trip_count == 0
        assert nmv_dashboard.attention.missing_trip_url is None
        assert not_my_vehicle_id in [
            trip["id"] for group in nmv_dashboard.day_groups for trip in group.trips
        ]

    _scenario(run)


def test_half_open_week_boundaries():
    async def run(pool):
        bounds = week_bounds(date(2026, 7, 13), UTC)
        async with pool.connection() as conn:
            sunday_2359 = await _insert_detected_trip(
                conn, bounds.end - timedelta(minutes=1), bounds.end - timedelta(seconds=30),
            )
            monday_0000 = await _insert_detected_trip(
                conn, bounds.end, bounds.end + timedelta(minutes=30),
            )

        this_week = await _call_dashboard(pool, week="2026-07-13")
        assert [t["id"] for g in this_week.day_groups for t in g.trips] == [sunday_2359]

        next_week = await _call_dashboard(pool, week="2026-07-20")
        assert [t["id"] for g in next_week.day_groups for t in g.trips] == [monday_0000]

    _scenario(run)


def test_midweek_anchor_selects_containing_week():
    async def run(pool):
        bounds = week_bounds(date(2026, 7, 13), UTC)
        async with pool.connection() as conn:
            trip_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(hours=3), bounds.start + timedelta(hours=4),
            )

        dashboard = await _call_dashboard(pool, week="2026-07-16")  # Thursday
        assert dashboard.nav.week_start == date(2026, 7, 13)
        assert [t["id"] for g in dashboard.day_groups for t in g.trips] == [trip_id]

    _scenario(run)


def test_malformed_anchor_falls_back_to_current_week():
    async def run(pool):
        now = datetime.now(UTC)
        current_monday = week_bounds(now.date(), UTC).monday

        dashboard = await _call_dashboard(pool, week="not-a-date")
        assert dashboard.nav.week_start == current_monday

    _scenario(run)


def test_dst_transition_week_returns_its_trips():
    async def run(pool):
        bounds = week_bounds(date(2026, 3, 8), LA)  # spring-forward week
        assert bounds.monday == date(2026, 3, 2)
        async with pool.connection() as conn:
            trip_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(days=3), bounds.start + timedelta(days=3, hours=1),
            )

        dashboard = await _call_dashboard(pool, week="2026-03-08", tz=LA)
        assert [t["id"] for g in dashboard.day_groups for t in g.trips] == [trip_id]

    _scenario(run)


def test_weekly_expense_total_honors_local_incurred_on_range():
    async def run(pool):
        bounds = week_bounds(date(2026, 7, 13), UTC)
        async with pool.connection() as conn:
            vehicle_id = await create_vehicle(conn, "Test Car")
            await _insert_expense(conn, vehicle_id, bounds.monday - timedelta(days=1), "10.00")
            await _insert_expense(conn, vehicle_id, bounds.monday, "20.00")
            await _insert_expense(conn, vehicle_id, bounds.monday + timedelta(days=6), "5.50")
            await _insert_expense(conn, vehicle_id, bounds.monday + timedelta(days=7), "99.00")

        dashboard = await _call_dashboard(pool, week="2026-07-13")
        assert dashboard.expense_total == Decimal("25.50")

    _scenario(run)


def test_deduction_unavailable_when_business_trip_month_has_no_rate():
    async def run(pool):
        # mileage_rates seeds 2025/2026 only (migrations/002); 2019 has no
        # rate and no earlier year to fall back to (app.rates.rate_for).
        bounds = week_bounds(date(2019, 6, 3), UTC)
        async with pool.connection() as conn:
            await _insert_detected_trip(
                conn, bounds.start + timedelta(hours=1), bounds.start + timedelta(hours=2),
                category="business",
            )

        dashboard = await _call_dashboard(pool, week="2019-06-03")
        assert dashboard.deduction.available is False
        assert dashboard.deduction.amount is None

    _scenario(run)


def test_dashboard_passes_vehicles_and_recent_purposes_for_the_trip_card():
    # dashboard.html includes _trip_card.html directly (unlike trips_archive
    # and trip_month_page, which go through the shared trip-list context
    # builders), so this is the one place that context can silently go
    # missing without any template failing to render.
    async def run(pool):
        now = datetime.now(UTC)
        bounds = week_bounds(now.date(), UTC)
        async with pool.connection() as conn:
            await create_vehicle(conn, "Test Car")
            trip_id = await _insert_detected_trip(
                conn, bounds.start + timedelta(hours=1), bounds.start + timedelta(hours=2),
            )
            await conn.execute(
                "UPDATE trips SET purpose = 'Client visit' WHERE id = %s", (trip_id,)
            )

        response = await DASHBOARD(_request(pool), {"sub": "test"}, "")
        # 008_vehicles.sql seeds a default "My Car" alongside the one created here.
        assert {v["name"] for v in response.context["vehicles"]} == {"My Car", "Test Car"}
        assert response.context["recent_purposes"] == ["Client visit"]

    _scenario(run)


def test_global_review_count_is_independent_from_weekly_attention():
    async def run(pool):
        bounds = week_bounds(date(2026, 7, 13), UTC)
        async with pool.connection() as conn:
            await _insert_detected_trip(
                conn,
                bounds.end + timedelta(days=1),
                bounds.end + timedelta(days=1, hours=1),
                category="unclassified",
            )

        response = await DASHBOARD(_request(pool), {"sub": "test"}, "2026-07-13")
        attention = response.context["dashboard"].attention
        assert attention is None or attention.unclassified_count == 0
        assert response.context["review_count"] == 1

    _scenario(run)


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    configure_personal_app(app, pool)
    app.state.config = SimpleNamespace(
        dev_no_auth=False, display_tz=UTC, missing_trip_gap_m=1000.0, app_version="test",
    )
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request, exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(make_router())
    return app


def test_unauthenticated_request_redirects_to_login():
    async def run(pool):
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False,
        ) as client:
            response = await client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    _scenario(run)
