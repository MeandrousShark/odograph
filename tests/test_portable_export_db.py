"""DB-backed tests for GET /settings/export/data.

Same conventions as tests/test_vehicles_db.py: skipped unless
TEST_DATABASE_URL is set, a reset database per test via reset_db, and a
bare FastAPI app driven through httpx.ASGITransport rather than the real
create_app().
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.db import make_pool
from app.main import make_templates
from app.portable import FORMAT, FORMAT_VERSION, make_router
from app.vehicles import create_vehicle
from conftest import reset_account_db
from personal_support import configure_personal_app

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

NOW = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(
        dev_no_auth=True, display_tz=timezone.utc,
        geocode_provider=None, app_version="test", app_git_revision="test",
    )
    configure_personal_app(app, pool)
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)
    app.include_router(make_router())
    return app


def _scenario(coro_factory) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            bound = await reset_account_db(pool)
            await coro_factory(bound)
        finally:
            await pool.close()

    asyncio.run(run())


async def _export(pool) -> dict:
    transport = httpx.ASGITransport(app=_bare_app(pool))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/settings/export/data")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"].startswith("attachment;")
    return response.json()


def test_export_on_a_freshly_migrated_instance_reflects_seed_state():
    async def run(pool):
        bundle = await _export(pool)
        assert bundle["format"] == FORMAT
        assert bundle["format_version"] == FORMAT_VERSION
        assert bundle["schema_version"] == 31
        assert len(bundle["vehicles"]) == 1
        assert bundle["vehicles"][0]["name"] == "My Car"
        assert bundle["vehicles"][0]["is_default"] is True
        assert len(bundle["tag_rules"]) == 2
        assert bundle["places"] == []
        assert bundle["trips"] == []
        assert bundle["expenses"] == []
        assert bundle["odometer_readings"] == []
        assert bundle["settings"] == {"auto_assign_default_vehicle": False, "display_tz": "UTC"}

    _scenario(run)


def test_export_reflects_a_populated_instance():
    async def run(pool):
        async with pool.connection() as conn:
            truck_id = await create_vehicle(conn, "Truck", make="Ford", model="F150")
            place_cur = await conn.execute(
                "INSERT INTO places (account_id, name, kind, geom, radius_m) "
                "VALUES (41, 'Office', 'work', ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, 100) "
                "RETURNING id",
            )
            place_id = (await place_cur.fetchone())[0]
            trip_cur = await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category, "
                " purpose, vehicle_id, start_place_id, tag_source) "
                "VALUES (41, 'manual', 'manual', %s, %s, 1609.344, 'business', 'Client visit', "
                " %s, %s, 'human') RETURNING id",
                (NOW, NOW, truck_id, place_id),
            )
            trip_id = (await trip_cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, trip_id) "
                "VALUES (41, %s, '2026-06-01', 'fuel', 45.67, 'business_use_allocated', %s)",
                (truck_id, trip_id),
            )
            await conn.execute(
                "INSERT INTO odometer_readings (account_id, vehicle_id, recorded_at, odometer_m) "
                "VALUES (41, %s, %s, 1000)",
                (truck_id, NOW),
            )

        bundle = await _export(pool)
        vehicle_ids = {v["name"]: v["$id"] for v in bundle["vehicles"]}
        assert set(vehicle_ids) == {"My Car", "Truck"}
        assert bundle["places"][0]["name"] == "Office"
        place_dollar_id = bundle["places"][0]["$id"]

        assert len(bundle["trips"]) == 1
        trip = bundle["trips"][0]
        assert trip["$id"] == trip_id
        assert trip["vehicle"] == vehicle_ids["Truck"]
        assert trip["start_place"] == place_dollar_id
        assert trip["exclusion"] is None
        assert trip["purpose"] == "Client visit"
        assert trip["distance_m"] == 1609.344
        assert trip["start_label"] is None
        assert trip["end_label"] is None

        assert bundle["expenses"] == [{
            "vehicle": vehicle_ids["Truck"], "incurred_on": "2026-06-01", "category": "fuel",
            "amount": "45.67", "treatment": "business_use_allocated", "notes": None,
            "trip": trip_id,
        }]
        assert bundle["odometer_readings"] == [{
            "vehicle": vehicle_ids["Truck"], "recorded_at": NOW.isoformat(),
            "odometer_m": 1000.0, "note": None,
        }]

    _scenario(run)


def test_export_includes_a_trip_endpoint_label_when_set():
    async def run(pool):
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category, "
                " start_label, end_label) "
                "VALUES (41, 'manual', 'manual', %s, %s, 1609.344, 'personal', "
                " 'Grandma''s house', 'Lake cabin')",
                (NOW, NOW),
            )

        bundle = await _export(pool)
        trip = bundle["trips"][0]
        assert trip["start_label"] == "Grandma's house"
        assert trip["end_label"] == "Lake cabin"

    _scenario(run)


def _redirect_app(pool) -> FastAPI:
    from starlette.responses import RedirectResponse

    from app.auth import AuthRedirect

    app = FastAPI()
    app.state.pool = pool
    app.state.config = SimpleNamespace(dev_no_auth=False, display_tz=timezone.utc, app_version="test")
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request, exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(make_router())
    return app


def test_export_requires_authentication():
    async def run(pool):
        transport = httpx.ASGITransport(app=_redirect_app(pool))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False,
        ) as client:
            response = await client.get("/settings/export/data")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    _scenario(run)


def test_export_refuses_to_emit_a_non_finite_value():
    """distance_m has no CHECK constraint against it, so a non-finite value
    can land in the ledger some other way than through this bundle's own
    import path (e.g. a bug elsewhere, or a hand-edited row). json.dumps's
    default allow_nan=True would otherwise write it out as a bare
    NaN/Infinity token -- valid to Python's own json.loads, but not RFC 8259
    JSON, making the file unreadable by anything else. Failing loudly here
    is deliberate: a corrupt export must never be silently produced.
    """
    async def run(pool):
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category) "
                "VALUES (41, 'phone1', 'manual', %s, %s, 'Infinity'::real, 'unclassified')",
                (NOW, NOW),
            )

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/settings/export/data")

        assert response.status_code == 500
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "non_finite_value"

    _scenario(run)
