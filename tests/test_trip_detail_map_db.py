"""DB-backed tests for GET /trips/{trip_id}'s map rendering: a routed
manual trip (source='manual' with a stored path/start_geom/end_geom) must
get the same map container and path data a detected trip gets, while a
plain manual trip with no geometry renders no map at all, exactly as
before this behavior existed. Same fixture conventions as
tests/test_trip_card_edit_db.py.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


DETAIL = _endpoint("/trips/{trip_id}", "GET")


def _request(pool):
    config = SimpleNamespace(
        display_tz=TZ, app_version="test",
        detector_params=SimpleNamespace(min_trip_distance_m=300.0),
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test"},
    )


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_routed_manual(conn) -> int:
    row = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
        "path, start_geom, end_geom, snap_status) VALUES ("
        "'manual', 'manual', '2026-07-14T16:00:00Z', '2026-07-14T17:00:00Z', 3200, "
        "ST_SetSRID(ST_GeomFromText('LINESTRING(-122.33 47.60, -122.20 47.70)'), 4326), "
        "ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography, "
        "ST_SetSRID(ST_MakePoint(-122.20, 47.70), 4326)::geography, NULL) RETURNING id"
    )
    return (await row.fetchone())[0]


async def _insert_plain_manual(conn) -> int:
    row = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m) "
        "VALUES ('manual', 'manual', '2026-07-14T16:00:00Z', '2026-07-14T17:00:00Z', 3200) "
        "RETURNING id"
    )
    return (await row.fetchone())[0]


async def _insert_detected(conn) -> int:
    row = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, start_geom, end_geom, "
        "distance_m, point_count, path, has_gap, detector_version, snap_status) VALUES ("
        "'phone', 'detected', '2026-07-14T18:00:00Z', '2026-07-14T19:00:00Z', "
        "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, "
        "ST_SetSRID(ST_MakePoint(-122.2, 47.7), 4326)::geography, 3200, 44, "
        "ST_GeomFromText('LINESTRING(-122.3 47.6,-122.2 47.7)', 4326), true, 2, 'pending') "
        "RETURNING id"
    )
    return (await row.fetchone())[0]


def _run(coro_factory) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await _reset_schema(pool)
            await coro_factory(pool)
        finally:
            await pool.close()

    asyncio.run(run())


def test_routed_manual_trip_renders_map_and_path_data():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_routed_manual(conn)

        request = _request(pool)
        response = await DETAIL(request, trip_id, user=USER)
        body = response.body.decode()
        assert 'id="map"' in body
        assert "-122.33" in body and "-122.2" in body
        # Detected-only tooling must not appear for a manual trip.
        assert "split-toggle" not in body

    _run(scenario)


def test_plain_manual_trip_with_no_geometry_renders_no_map():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_plain_manual(conn)

        request = _request(pool)
        response = await DETAIL(request, trip_id, user=USER)
        body = response.body.decode()
        assert 'id="map"' not in body

    _run(scenario)


def test_detected_trip_still_renders_map_and_detected_only_tools():
    async def scenario(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_detected(conn)

        request = _request(pool)
        response = await DETAIL(request, trip_id, user=USER)
        body = response.body.decode()
        assert 'id="map"' in body
        assert "split-toggle" in body

    _run(scenario)
