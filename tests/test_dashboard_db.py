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
from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import make_router
from app.vehicles import create_vehicle

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


def _request(pool, tz: ZoneInfo = UTC, missing_trip_gap_m: float = 1000.0):
    config = SimpleNamespace(display_tz=tz, missing_trip_gap_m=missing_trip_gap_m)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    )


async def _call_dashboard(pool, week: str = "", tz: ZoneInfo = UTC):
    response = await DASHBOARD(_request(pool, tz=tz), {"sub": "test"}, week)
    return response.context["dashboard"]


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_detected_trip(
    conn, started_at: datetime, ended_at: datetime, category: str = "unclassified",
    device: str = "DASH", distance_m: float = 1000.0, vehicle_id: int | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, category, vehicle_id) "
        "VALUES (%s, 'detected', %s, %s, %s, 2, 2, %s, %s) RETURNING id",
        (device, started_at, ended_at, distance_m, category, vehicle_id),
    )
    return (await cur.fetchone())[0]


async def _insert_expense(conn, vehicle_id: int, incurred_on: date, amount: str) -> int:
    cur = await conn.execute(
        "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment) "
        "VALUES (%s, %s, 'fuel', %s, 'fully_business') RETURNING id",
        (vehicle_id, incurred_on, Decimal(amount)),
    )
    return (await cur.fetchone())[0]


def _scenario(coro) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await _reset_schema(pool)
            await coro(pool)
        finally:
            await pool.close()

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


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.config = SimpleNamespace(dev_no_auth=False, display_tz=UTC, missing_trip_gap_m=1000.0)
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
