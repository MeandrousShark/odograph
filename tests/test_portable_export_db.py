"""DB-backed tests for GET /settings/export/data.

Same conventions as tests/test_vehicles_db.py: skipped unless
TEST_DATABASE_URL is set, a reset database per test via reset_db, and a
bare FastAPI app driven through httpx.ASGITransport rather than the real
create_app().
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from psycopg.errors import InsufficientPrivilege
from starlette.middleware.sessions import SessionMiddleware

import app.account_context as account_context
from app.db import make_pool
from app.main import make_templates
from app.portable import FORMAT, FORMAT_VERSION, make_router
from app.portable import routes as portable_routes
from app.portable.importer import _apply_import
from app.portable.normalize import normalize_bundle
from app.vehicles import create_vehicle
from conftest import LATEST_SCHEMA_VERSION, add_test_account, reset_account_db
from personal_support import configure_personal_app

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

NOW = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)
RUNTIME_ROLE = account_context.RUNTIME_ROLE

_EXPORT_ENDPOINT = next(
    route.endpoint for route in make_router().routes
    if route.path == "/settings/export/data" and "GET" in route.methods
)


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


async def _direct_export(pool) -> dict:
    response = await _EXPORT_ENDPOINT(
        SimpleNamespace(state=SimpleNamespace(account_pool=pool)), user={}
    )
    assert response.status_code == 200
    return json.loads(response.body)


async def _backend_pid(conn) -> int:
    pid, role = await (await conn.execute(
        "SELECT pg_backend_pid(), current_user"
    )).fetchone()
    assert role == RUNTIME_ROLE
    return pid


async def _insert_vehicle_trip(conn, name: str) -> tuple[int, int]:
    owner = account_context.account_id(conn)
    cur = await conn.execute(
        "INSERT INTO vehicles (account_id, name, is_default, active) "
        "VALUES (%s, %s, false, true) RETURNING id",
        (owner, name),
    )
    vehicle_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, "
        "distance_m, category, vehicle_id) "
        "VALUES (%s, 'manual', 'manual', %s, %s, 1000, 'personal', %s) "
        "RETURNING id",
        (owner, NOW, NOW + timedelta(minutes=30), vehicle_id),
    )
    return vehicle_id, (await cur.fetchone())[0]


def _pause_vehicle_fetch(monkeypatch):
    fetched, resume = asyncio.Event(), asyncio.Event()
    state = {}
    original = portable_routes._fetch_export_vehicles

    async def paused(conn):
        rows = await original(conn)
        state["pid"] = await _backend_pid(conn)
        fetched.set()
        await resume.wait()
        return rows

    monkeypatch.setattr(portable_routes, "_fetch_export_vehicles", paused)
    return fetched, resume, state


def _reference_summary(bundle):
    vehicle_names = {vehicle["$id"]: vehicle["name"] for vehicle in bundle["vehicles"]}
    return (
        tuple(sorted(vehicle_names.values())),
        tuple(sorted(
            (
                trip["device"], trip["source"], trip["started_at"], trip["ended_at"],
                trip["distance_m"],
                vehicle_names.get(trip["vehicle"]),
            )
            for trip in bundle["trips"]
        )),
    )


async def _assert_export_round_trip(bundle, target) -> None:
    normalized, issues = normalize_bundle(bundle)
    assert issues == []
    async with target.connection() as conn:
        await _apply_import(conn, normalized)
    round_trip = await _direct_export(target)
    _, issues = normalize_bundle(round_trip)
    assert issues == []
    assert _reference_summary(round_trip) == _reference_summary(bundle)


def test_export_on_a_freshly_migrated_instance_reflects_seed_state():
    async def run(pool):
        bundle = await _export(pool)
        assert bundle["format"] == FORMAT
        assert bundle["format_version"] == FORMAT_VERSION
        assert bundle["schema_version"] == LATEST_SCHEMA_VERSION
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


def test_export_uses_one_snapshot_when_a_referenced_vehicle_and_trip_are_created(monkeypatch):
    async def run(pool):
        target = await add_test_account(pool.admin_pool, 42)
        fetched, resume, _state = _pause_vehicle_fetch(monkeypatch)
        export_task = None
        try:
            export_task = asyncio.create_task(_direct_export(pool))
            await asyncio.wait_for(fetched.wait(), 5)
            async with pool.connection() as conn:
                await _insert_vehicle_trip(conn, "Concurrent car")
            resume.set()
            bundle = await asyncio.wait_for(export_task, 5)

            assert [vehicle["name"] for vehicle in bundle["vehicles"]] == ["My Car"]
            assert bundle["trips"] == []
            await _assert_export_round_trip(bundle, target)
        finally:
            resume.set()
            if export_task is not None and not export_task.done():
                export_task.cancel()
                await asyncio.gather(export_task, return_exceptions=True)

    _scenario(run)


def test_export_keeps_a_deleted_referenced_vehicle_in_its_snapshot(monkeypatch):
    async def run(pool):
        target = await add_test_account(pool.admin_pool, 42)
        async with pool.connection() as conn:
            vehicle_id, trip_id = await _insert_vehicle_trip(conn, "Truck")

        fetched, resume, _state = _pause_vehicle_fetch(monkeypatch)
        export_task = None
        try:
            export_task = asyncio.create_task(_direct_export(pool))
            await asyncio.wait_for(fetched.wait(), 5)
            async with pool.connection() as conn:
                deleted = await conn.execute(
                    "DELETE FROM vehicles WHERE account_id = %s AND id = %s",
                    (account_context.account_id(conn), vehicle_id),
                )
                assert deleted.rowcount == 1
            resume.set()
            bundle = await asyncio.wait_for(export_task, 5)

            vehicles = {vehicle["name"]: vehicle["$id"] for vehicle in bundle["vehicles"]}
            assert set(vehicles) == {"My Car", "Truck"}
            assert bundle["trips"][0]["$id"] == trip_id
            assert bundle["trips"][0]["vehicle"] == vehicles["Truck"]
            await _assert_export_round_trip(bundle, target)
        finally:
            resume.set()
            if export_task is not None and not export_task.done():
                export_task.cancel()
                await asyncio.gather(export_task, return_exceptions=True)

    _scenario(run)


async def _wait_blocked(owner, waiting_pid, holding_pid, task):
    async with asyncio.timeout(5):
        while True:
            async with owner.connection() as conn:
                blockers = (await (await conn.execute(
                    "SELECT pg_blocking_pids(%s)", (waiting_pid,),
                )).fetchone())[0]
            if holding_pid in blockers:
                return
            if task.done():
                await task
                pytest.fail("account disable completed before export released admission lock")
            await asyncio.sleep(0.01)


def test_export_holds_account_admission_lock_until_its_snapshot_finishes(monkeypatch):
    async def run(pool):
        target = await add_test_account(pool.admin_pool, 42)
        fetched, resume, state = _pause_vehicle_fetch(monkeypatch)
        export_pid = None
        disable_pid = None
        disable_started = asyncio.Event()
        export_task = disable_task = None

        async def disable_account():
            nonlocal disable_pid
            async with pool.admin_pool.connection() as conn:
                disable_pid = (await (await conn.execute(
                    "SELECT pg_backend_pid()"
                )).fetchone())[0]
                disable_started.set()
                await conn.execute(
                    "UPDATE accounts SET is_enabled = false, auth_version = auth_version + 1 "
                    "WHERE id = %s",
                    (pool.principal.account_id,),
                )

        try:
            export_task = asyncio.create_task(_direct_export(pool))
            await asyncio.wait_for(fetched.wait(), 5)
            # The export has passed assert_account_active before reaching its
            # first row fetch, so the account's FOR SHARE lock is held.
            export_pid = state["pid"]
            disable_task = asyncio.create_task(disable_account())
            await asyncio.wait_for(disable_started.wait(), 5)
            await _wait_blocked(pool.admin_pool, disable_pid, export_pid, disable_task)

            resume.set()
            bundle = await asyncio.wait_for(export_task, 5)
            await asyncio.wait_for(disable_task, 5)
            await _assert_export_round_trip(bundle, target)

            with pytest.raises(InsufficientPrivilege):
                await _direct_export(pool)
        finally:
            resume.set()
            for task in (export_task, disable_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (export_task, disable_task) if task is not None),
                return_exceptions=True,
            )

    _scenario(run)


def test_export_retries_serialization_failure_and_rechecks_account_admission(monkeypatch):
    async def run(pool):
        snapshot_ready, allow_admission = asyncio.Event(), asyncio.Event()
        original = account_context.apply_account_context
        apply_calls = 0
        export_task = None

        async def pause_after_snapshot(conn, principal):
            nonlocal apply_calls
            await original(conn, principal)
            apply_calls += 1
            if apply_calls == 1:
                # apply_account_context's SELECT is the first query and has
                # established the repeatable-read snapshot, before admission.
                snapshot_ready.set()
                await allow_admission.wait()

        monkeypatch.setattr(account_context, "apply_account_context", pause_after_snapshot)
        try:
            export_task = asyncio.create_task(_direct_export(pool))
            await asyncio.wait_for(snapshot_ready.wait(), 5)
            async with pool.admin_pool.connection() as conn:
                await conn.execute(
                    "UPDATE accounts SET is_enabled = false, auth_version = auth_version + 1 "
                    "WHERE id = %s",
                    (pool.principal.account_id,),
                )
            allow_admission.set()

            # The first FOR SHARE admission conflicts with the newer row and
            # raises 40001. The retry starts a fresh snapshot and denies the
            # now-disabled account rather than emitting any bundle.
            with pytest.raises(InsufficientPrivilege):
                await asyncio.wait_for(export_task, 5)
            assert apply_calls == 2
        finally:
            allow_admission.set()
            if export_task is not None and not export_task.done():
                export_task.cancel()
                await asyncio.gather(export_task, return_exceptions=True)

    _scenario(run)


def test_export_snapshot_is_opt_in_and_does_not_change_normal_account_transactions():
    async def run(pool):
        async with pool.connection() as conn:
            normal = await (await conn.execute("SHOW transaction_isolation")).fetchone()
        async with pool.connection(consistent_snapshot=True) as conn:
            snapshot = await (await conn.execute("SHOW transaction_isolation")).fetchone()
        async with pool.connection() as conn:
            normal_after = await (await conn.execute("SHOW transaction_isolation")).fetchone()

        assert normal == ("read committed",)
        assert snapshot == ("repeatable read",)
        assert normal_after == ("read committed",)

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
