"""Explicit personal-query isolation on the restricted runtime role with RLS enforced."""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

import pytest
from fastapi import HTTPException

from app.db import make_pool
from app.auth import require_user
from app.account_settings import load_account_settings
from app.detector.core import Params
from app.main import make_templates
from app.ui import make_router
from app.rates import load_rates
from app.ui._common import _fetch_recent_purposes, _fetch_trip, _trip_filter_sql
from app.ui.manual import _resolve_manual_route_endpoints
from app.ui.places import _fetch_places_rows
from app.ui.reports import _fetch_range_trips_in, _fetch_year_expense_report
from app.ui.settings import _fetch_device_fixes, _fetch_odometer_context
from app.ui.trips import _apply_human_tag, _delete_trip_in
from app.vehicles import list_vehicles, set_default_vehicle
from conftest import account_pool, reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL")
START = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


async def _seed(raw):
    # Test-only second account; reset_db() recreates the singleton guard.
    await raw.execute("DROP INDEX accounts_singleton_idx")
    for owner, name in ((41, "Alpha"), (73, "Beta")):
        await raw.execute(
            "INSERT INTO accounts (id, email, password_hash) VALUES (%s, %s, 'test-only')",
            (owner, name.lower() + "@example.test"),
        )
        await raw.execute("INSERT INTO account_settings (account_id) VALUES (%s)", (owner,))
        await raw.execute(
            "INSERT INTO vehicles (id, account_id, name, is_default) OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, true)",
            (owner, owner, name),
        )
        await raw.execute(
            "INSERT INTO places (id, account_id, name, kind, geom, radius_m) OVERRIDING SYSTEM VALUE "
            "VALUES (%s, %s, %s, 'other', ST_SetSRID(ST_MakePoint(1, 1), 4326), 100)",
            (owner, owner, name),
        )
        await raw.execute(
            "INSERT INTO tracking_devices (id, account_id, label) VALUES (%s, %s, 'phone')",
            (owner, owner),
        )
        await raw.execute(
            "INSERT INTO mileage_rates (account_id, year, rate_per_mi) VALUES (%s, 2026, %s)",
            (owner, owner / 100),
        )
        await raw.execute(
            "INSERT INTO trips (account_id, tracking_device_id, device, started_at, ended_at, "
            "distance_m, vehicle_id, start_place_id, start_geom, end_geom, purpose) "
            "VALUES (%s, %s, 'phone', %s, %s, 1000, %s, %s, "
            "ST_SetSRID(ST_MakePoint(1, 1), 4326), ST_SetSRID(ST_MakePoint(2, 2), 4326), %s)",
            (owner, owner, START, START + timedelta(minutes=10), owner, owner, name),
        )
        await raw.execute(
            "INSERT INTO geocode_cache (account_id, lat, lon, address) VALUES (%s, 1, 1, %s)",
            (owner, name + " address"),
        )
        await raw.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom) "
            "VALUES (%s, %s, 'phone', %s, ST_SetSRID(ST_MakePoint(1, 1), 4326))",
            (owner, owner, START),
        )
        await raw.execute(
            "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment) "
            "VALUES (%s, %s, '2026-09-01', 'parking', 10, 'fully_business')",
            (owner, owner),
        )
    cur = await raw.execute("SELECT account_id, id FROM trips ORDER BY account_id")
    return dict(await cur.fetchall())


async def _query_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as raw:
            ids = await _seed(raw)
        apool, bpool = await account_pool(pool, 41), await account_pool(pool, 73)
        async with apool.connection() as a, bpool.connection() as b:
            assert [v["name"] for v in await list_vehicles(a)] == ["Alpha"]
            assert [v["name"] for v in await list_vehicles(b)] == ["Beta"]
            assert [p["name"] for p in await _fetch_places_rows(a)] == ["Alpha"]
            assert await _fetch_recent_purposes(a) == ["Alpha"]
            assert (await load_rates(a))[2026].rate_per_mi == .41
            assert (await load_rates(b))[2026].rate_per_mi == .73
            assert [r["tracking_device_id"] for r in await _fetch_device_fixes(a)] == [41]
            assert [r["vehicle"]["name"] for r in await _fetch_odometer_context(a)] == ["Alpha"]
            trips, rates = await _fetch_range_trips_in(a, ZoneInfo("UTC"), date(2026, 1, 1), date(2026, 12, 31))
            assert [t["id"] for t in trips] == [ids[41]]
            assert trips[0]["start_address"] == "Alpha address"
            assert trips[0]["vehicle_name"] == "Alpha"
            expenses, _ = await _fetch_year_expense_report(apool, ZoneInfo("UTC"), 2026, trips, rates)
            assert len(expenses) == 1
            with pytest.raises(HTTPException) as missing:
                await _fetch_trip(apool, ids[73])
            assert missing.value.status_code == 404
            for foreign in (
                lambda: _apply_human_tag(a, ids[73], "business"),
                lambda: _delete_trip_in(a, ids[73]),
                lambda: set_default_vehicle(a, 73),
                lambda: _resolve_manual_route_endpoints(a, "places", "41", "73", "", "", "", ""),
            ):
                with pytest.raises(HTTPException):
                    async with a.transaction():
                        await foreign()
            assert (await list_vehicles(a))[0]["is_default"]
            await _apply_human_tag(a, ids[41], "business")
        async with pool.connection() as raw:
            where, params = _trip_filter_sql("", None, None, owner_id=41)
            cur = await raw.execute(f"SELECT id FROM trips {where}", params)
            assert await cur.fetchall() == [(ids[41],)]
            cur = await raw.execute("SELECT id, category::text FROM trips ORDER BY id")
            assert await cur.fetchall() == [(ids[41], "business"), (ids[73], "unclassified")]
            # Neither another account's same-label stream nor a second
            # same-label device in A may become A's predecessor.
            await raw.execute("UPDATE trips SET started_at = %s, ended_at = %s WHERE id = %s",
                              (START - timedelta(hours=2), START - timedelta(hours=1), ids[73]))
            await raw.execute("INSERT INTO tracking_devices (id, account_id, label) VALUES (42, 41, 'phone')")
            await raw.execute(
                "INSERT INTO trips (account_id, tracking_device_id, device, started_at, ended_at, distance_m) "
                "VALUES (41, 42, 'phone', %s, %s, 1000)",
                (START - timedelta(hours=2), START - timedelta(hours=1)),
            )
        assert (await _fetch_trip(apool, ids[41]))["prev_trip_ended_at"] is None
        async with pool.connection() as raw:
            await raw.execute(
                "INSERT INTO trips (account_id, tracking_device_id, device, started_at, ended_at, distance_m) "
                "VALUES (41, 41, 'phone', %s, %s, 1000)",
                (START - timedelta(hours=2), START - timedelta(hours=1)),
            )
        assert (await _fetch_trip(apool, ids[41]))["prev_trip_ended_at"] == START - timedelta(hours=1)
    finally:
        await pool.close()


async def _http_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as raw:
            ids = await _seed(raw)
        apool, bpool = await account_pool(pool, 41), await account_pool(pool, 73)
        cfg = SimpleNamespace(
            display_tz=ZoneInfo("UTC"), app_version="test", app_git_revision="test",
            trips_page_size=20, detector_params=Params(),
            missing_trip_gap_m=500, geocode_provider=None, osrm_url=None,
        )
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test-only-session-secret")
        app.state.templates = make_templates(cfg)
        app.state.config = cfg

        async def account_request(request: Request):
            request.state.principal = apool.principal
            request.state.account_pool = apool
            request.state.config = cfg
            async with apool.connection() as conn:
                request.state.account_settings = await load_account_settings(conn)
            request.session["csrf"] = "test-csrf"
            return {"id": 41, "name": "Alpha", "is_admin": False, "has_avatar": False}

        app.dependency_overrides[require_user] = account_request
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for url in ("/", "/trips", "/review", "/stats?year=2026", "/report/2026", "/expenses?year=2026", "/settings", f"/trips/{ids[41]}", "/export?format=csv"):
                response = await client.get(url)
                assert response.status_code == 200, (url, response.text[:300])
                assert "Beta" not in response.text, url
                if url == "/settings":
                    assert 'name="email_digest_hour" min="0" max="23" value="9"' in response.text
                    assert 'name="email_filing_reminder_mmdd" value="01-15"' in response.text
            assert (await client.get(f"/trips/{ids[73]}" )).status_code == 404
            csrf_headers = {"X-CSRF-Token": "test-csrf"}
            for url, data in (
                (f"/trips/{ids[73]}/tag", {"category": "business"}),
                (f"/trips/{ids[73]}/notes", {"notes": "not yours"}),
                ("/trips/batch_update", {"trip_ids": [ids[41], ids[73]], "category": "business"}),
                ("/trips/batch_delete", {"trip_ids": [ids[41], ids[73]]}),
                ("/settings/vehicles/73/default", {}),
            ):
                response = await client.post(url, data=data, headers=csrf_headers)
                assert response.status_code in (400, 404), (url, response.status_code, response.text[:300])
            async with pool.connection() as raw:
                cur = await raw.execute("SELECT count(*) FROM trips")
                assert (await cur.fetchone())[0] == 2
            response = await client.post(
                "/settings/preferences", headers={"X-CSRF-Token": "test-csrf"},
                data={"display_timezone": "Pacific/Auckland", "email_to": "alpha@example.test", "email_monthly_summary": "1"},
            )
            assert response.status_code == 204
            async with apool.connection() as conn:
                settings = await load_account_settings(conn)
            assert str(settings.display_tz) == "Pacific/Auckland"
            assert settings.email_to == "alpha@example.test"
            async with bpool.connection() as b:
                assert (await load_account_settings(b)).email_to == ""
                assert str((await load_account_settings(b)).display_tz) == "UTC"
    finally:
        await pool.close()


def test_personal_http_pages_and_preferences_scope_account_with_rls():
    asyncio.run(_http_scenario())


def test_personal_queries_and_mutations_scope_account_with_rls():
    asyncio.run(_query_scenario())
