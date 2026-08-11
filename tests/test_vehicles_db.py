"""DB-backed regression tests for vehicle CRUD.

Like tests/test_retention_db.py, needs a real Postgres and is skipped unless
TEST_DATABASE_URL is set. Covers what app/vehicles.py's pure-Python signature
can't exercise on its own: the partial unique index
(vehicles_one_default_idx) actually enforcing "at most one default" across
set_default_vehicle's two UPDATEs, and trips.vehicle_id's ON DELETE SET NULL
actually detaching (not destroying) a trip when its vehicle is deleted.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import make_router
from app.vehicles import (
    create_vehicle,
    deactivate_vehicle,
    get_auto_assign_default_vehicle,
    list_vehicles,
    set_auto_assign_default_vehicle,
    set_default_vehicle,
    update_vehicle,
)

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

NOW = datetime.now(timezone.utc)


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_trip(conn, vehicle_id: int | None) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, vehicle_id) "
        "VALUES ('manual', 'manual', %s, %s, 1000, %s) RETURNING id",
        (NOW - timedelta(hours=1), NOW, vehicle_id),
    )
    return (await cur.fetchone())[0]


async def _crud_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        async with pool.connection() as conn:
            # 008_vehicles.sql seeds one default vehicle ("My Car").
            seeded = await list_vehicles(conn)
            assert len(seeded) == 1
            assert seeded[0]["name"] == "My Car"
            assert seeded[0]["is_default"] is True

            truck_id = await create_vehicle(conn, "Truck", make="Ford", model="F150")
            active = await list_vehicles(conn)
            assert {v["id"] for v in active} == {seeded[0]["id"], truck_id}

            await update_vehicle(conn, truck_id, "Work Truck", make="Ford", model="F250", plate="ABC123")
            updated = {v["id"]: v for v in await list_vehicles(conn, include_inactive=True)}
            assert updated[truck_id]["name"] == "Work Truck"
            assert updated[truck_id]["model"] == "F250"
            assert updated[truck_id]["plate"] == "ABC123"

            await deactivate_vehicle(conn, truck_id)
            active_only = await list_vehicles(conn)
            assert truck_id not in {v["id"] for v in active_only}
            all_vehicles = await list_vehicles(conn, include_inactive=True)
            assert truck_id in {v["id"] for v in all_vehicles}
            assert next(v for v in all_vehicles if v["id"] == truck_id)["active"] is False
    finally:
        await pool.close()


def test_vehicle_crud_add_edit_deactivate():
    asyncio.run(_crud_scenario())


async def _single_default_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        async with pool.connection() as conn:
            seeded = (await list_vehicles(conn))[0]
            sedan_id = await create_vehicle(conn, "Sedan")

            await set_default_vehicle(conn, sedan_id)
            rows = {v["id"]: v for v in await list_vehicles(conn)}
            assert rows[sedan_id]["is_default"] is True
            assert rows[seeded["id"]]["is_default"] is False

            # Setting a different default again must not leave two defaults
            # (vehicles_one_default_idx would reject that at the DB level).
            await set_default_vehicle(conn, seeded["id"])
            rows = {v["id"]: v for v in await list_vehicles(conn)}
            assert rows[seeded["id"]]["is_default"] is True
            assert rows[sedan_id]["is_default"] is False

            cur = await conn.execute("SELECT count(*) FROM vehicles WHERE is_default")
            assert (await cur.fetchone())[0] == 1
    finally:
        await pool.close()


def test_set_default_vehicle_clears_previous_default():
    asyncio.run(_single_default_scenario())


async def _delete_detaches_trip_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        async with pool.connection() as conn:
            truck_id = await create_vehicle(conn, "Truck")
            trip_id = await _insert_trip(conn, truck_id)

            await conn.execute("DELETE FROM vehicles WHERE id = %s", (truck_id,))

            cur = await conn.execute(
                "SELECT vehicle_id FROM trips WHERE id = %s", (trip_id,)
            )
            row = await cur.fetchone()
            assert row is not None, "deleting a vehicle must not delete its trips"
            assert row[0] is None, "trips.vehicle_id must be nulled, not left dangling"
    finally:
        await pool.close()


def test_deleting_vehicle_nulls_trip_vehicle_id_not_the_trip():
    asyncio.run(_delete_detaches_trip_scenario())


async def _deactivate_clears_default_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        async with pool.connection() as conn:
            # 008_vehicles.sql seeds "My Car" as the default.
            seeded = (await list_vehicles(conn))[0]
            assert seeded["is_default"] is True

            await deactivate_vehicle(conn, seeded["id"])
            rows = {v["id"]: v for v in await list_vehicles(conn, include_inactive=True)}
            assert rows[seeded["id"]]["active"] is False
            assert rows[seeded["id"]]["is_default"] is False
    finally:
        await pool.close()


def test_deactivate_vehicle_clears_is_default():
    """A retired default must stop reading as the default -- otherwise the
    settings table shows a default the active pickers omit, and it would
    keep being auto-assigned to newly detected trips once that setting is
    on."""
    asyncio.run(_deactivate_clears_default_scenario())


async def _app_settings_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        async with pool.connection() as conn:
            versions = await conn.execute(
                "SELECT COALESCE(max(version), 0) FROM schema_migrations"
            )
            assert (await versions.fetchone())[0] == 21

            row = await conn.execute("SELECT count(*) FROM app_settings")
            assert (await row.fetchone())[0] == 1

            assert await get_auto_assign_default_vehicle(conn) is False

            await set_auto_assign_default_vehicle(conn, True)
            assert await get_auto_assign_default_vehicle(conn) is True

            await set_auto_assign_default_vehicle(conn, False)
            assert await get_auto_assign_default_vehicle(conn) is False
    finally:
        await pool.close()


def test_app_settings_migration_seeds_row_defaulting_auto_assign_off():
    asyncio.run(_app_settings_scenario())


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.config = SimpleNamespace(
        dev_no_auth=True, display_tz=timezone.utc,
        geocode_provider=None, app_version="test", app_git_revision="test",
    )
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)
    app.include_router(make_router())
    return app


CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')


def _checkbox_checked(page_html: str) -> bool:
    field = page_html.split('name="auto_assign_default_vehicle"')[1].split(">", 1)[0]
    return "checked" in field


async def _auto_assign_route_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            settings_page = await client.get("/settings")
            csrf = CSRF_RE.search(settings_page.text).group(1)
            headers = {"X-CSRF-Token": csrf}

            checked_on = await client.post(
                "/settings/vehicles/auto_assign",
                data={"auto_assign_default_vehicle": "1"}, headers=headers,
            )
            assert checked_on.status_code == 204
            assert await _read_auto_assign(pool) is True
            assert _checkbox_checked((await client.get("/settings")).text)

            # An unchecked HTML checkbox submits nothing at all -- htmx
            # posts a genuinely empty urlencoded body, the case a
            # Form(default="") argument can't distinguish from at the
            # Python call-site alone.
            unchecked_off = await client.post(
                "/settings/vehicles/auto_assign", content=b"",
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            )
            assert unchecked_off.status_code == 204
            assert await _read_auto_assign(pool) is False
            assert not _checkbox_checked((await client.get("/settings")).text)
    finally:
        await pool.close()


async def _read_auto_assign(pool) -> bool:
    async with pool.connection() as conn:
        return await get_auto_assign_default_vehicle(conn)


def test_auto_assign_route_treats_empty_body_as_unchecked():
    asyncio.run(_auto_assign_route_scenario())
