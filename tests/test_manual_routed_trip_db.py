"""DB-backed tests for POST /trips/manual's routed-entry behavior: resolving
a place-pair or map-picked pair, calling OSRM, and choosing the final stored
distance according to whether the user overrode it. Same fixture
conventions as tests/test_trip_card_edit_db.py: a fresh schema per test, and
the route's endpoint function called directly (bypassing FastAPI's own
dependency injection, which never runs on a bare function call).
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row

from app.db import make_pool, run_migrations
from app.rates import METERS_PER_MILE
from app.ui import MANUAL_ROUTE_UNAVAILABLE_NOTICE, make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}
OSRM_URL = "http://osrm.internal.test:5000"


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


ADD = _endpoint("/trips/manual", "POST")

DEFAULT_FORM = {
    "date": "2026-07-14", "start_time": "09:00", "end_time": "10:00", "distance": "10",
    "category": "unclassified", "purpose": "", "notes": "", "vehicle_id": "",
    "route_mode": "none", "start_place": "", "end_place": "",
    "start_lat": "", "start_lon": "", "end_lat": "", "end_lon": "",
    "routed_distance": "",
}


def _request(pool, *, osrm_url=OSRM_URL, http_client=None):
    config = SimpleNamespace(osrm_url=osrm_url, display_tz=TZ, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=config, osrm_http_client=http_client,
        )),
        session={"csrf": "test"},
    )


async def _add(request, **overrides):
    values = dict(DEFAULT_FORM)
    values.update(overrides)
    return await ADD(request, user=USER, **values)


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_place(conn, name, lat, lon) -> int:
    row = await conn.execute(
        "INSERT INTO places (name, geom) VALUES "
        "(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) RETURNING id",
        (name, lon, lat),
    )
    return (await row.fetchone())[0]


async def _fetch_only_trip(pool) -> dict:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, distance_m, snap_status::text AS snap_status, start_place_id, end_place_id, "
            "path IS NOT NULL AS has_path, ST_AsGeoJSON(path) AS path_geojson, "
            "start_geom IS NOT NULL AS has_start_geom, "
            "end_geom IS NOT NULL AS has_end_geom, "
            "ST_Y(start_geom::geometry) AS start_lat, ST_X(start_geom::geometry) AS start_lon, "
            "ST_Y(end_geom::geometry) AS end_lat, ST_X(end_geom::geometry) AS end_lon "
            "FROM trips"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1, f"expected exactly one trip, found {len(rows)}"
    return rows[0]


async def _trip_count(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM trips")
        return (await cur.fetchone())[0]


def _ok_handler(distance):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{
                "distance": distance,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-122.33, 47.60], [-122.20, 47.70]],
                },
            }],
        })
    return handler


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


def test_two_named_places_store_both_place_ids_and_route_geometry():
    async def scenario(pool):
        async with pool.connection() as conn:
            start_id = await _insert_place(conn, "Home", 47.60, -122.33)
            end_id = await _insert_place(conn, "Work", 47.70, -122.20)

        async with httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(2345.6))) as client:
            request = _request(pool, http_client=client)
            response = await _add(
                request, route_mode="places", distance="",
                start_place=str(start_id), end_place=str(end_id),
            )

        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == "/trips"
        trip = await _fetch_only_trip(pool)
        assert trip["start_place_id"] == start_id
        assert trip["end_place_id"] == end_id
        assert trip["has_path"] is True
        assert trip["has_start_geom"] is True
        assert trip["has_end_geom"] is True
        assert trip["distance_m"] == pytest.approx(2345.6)
        assert trip["snap_status"] is None

    _run(scenario)


def test_two_map_coordinates_store_geometry_with_null_place_ids():
    async def scenario(pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(1500.0))) as client:
            request = _request(pool, http_client=client)
            response = await _add(
                request, route_mode="map", distance="",
                start_lat="47.60", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )

        assert response.status_code == 204
        trip = await _fetch_only_trip(pool)
        assert trip["start_place_id"] is None
        assert trip["end_place_id"] is None
        assert trip["has_path"] is True
        assert trip["has_start_geom"] is True
        assert trip["has_end_geom"] is True
        assert trip["start_lat"] == pytest.approx(47.60)
        assert trip["start_lon"] == pytest.approx(-122.33)
        assert trip["end_lat"] == pytest.approx(47.70)
        assert trip["end_lon"] == pytest.approx(-122.20)
        assert trip["distance_m"] == pytest.approx(1500.0)
        assert trip["snap_status"] is None

    _run(scenario)


def test_filled_distance_differing_from_routed_distance_is_kept_as_override():
    async def scenario(pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(2345.6))) as client:
            request = _request(pool, http_client=client)
            response = await _add(
                request, route_mode="map",
                distance="9.9", routed_distance="1.5",
                start_lat="47.60", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )

        assert response.status_code == 204
        trip = await _fetch_only_trip(pool)
        assert trip["distance_m"] == pytest.approx(9.9 * METERS_PER_MILE)
        assert trip["has_path"] is True

    _run(scenario)


def test_distance_equal_to_routed_distance_is_not_treated_as_override():
    async def scenario(pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(2345.6))) as client:
            request = _request(pool, http_client=client)
            response = await _add(
                request, route_mode="map",
                distance="1.5", routed_distance="1.5",
                start_lat="47.60", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )

        assert response.status_code == 204
        trip = await _fetch_only_trip(pool)
        # The freshly-routed distance wins, not the client-submitted number,
        # even though the two happen to display the same rounded value here.
        assert trip["distance_m"] == pytest.approx(2345.6)

    _run(scenario)


def test_blank_distance_with_successful_routing_stores_routed_distance():
    async def scenario(pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(3210.0))) as client:
            request = _request(pool, http_client=client)
            response = await _add(
                request, route_mode="map", distance="",
                start_lat="47.60", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )

        assert response.status_code == 204
        trip = await _fetch_only_trip(pool)
        assert trip["distance_m"] == pytest.approx(3210.0)
        assert trip["has_path"] is True

    _run(scenario)


def _routing_failure_scenarios():
    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    def non_2xx(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "Error"})

    def no_route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "NoRoute", "routes": []})

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    return {
        "transport_error": transport_error,
        "non_2xx": non_2xx,
        "no_route": no_route,
        "malformed_body": malformed,
    }


@pytest.mark.parametrize("name", list(_routing_failure_scenarios().keys()))
def test_routing_failure_with_filled_distance_saves_legacy_trip_with_notice(name):
    handler = _routing_failure_scenarios()[name]

    async def scenario(pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _add(
                request, route_mode="map", distance="8.5",
                start_lat="47.60", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )

        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == f"/trips?notice={MANUAL_ROUTE_UNAVAILABLE_NOTICE}"
        trip = await _fetch_only_trip(pool)
        assert trip["distance_m"] == pytest.approx(8.5 * METERS_PER_MILE)
        assert trip["has_path"] is False
        assert trip["has_start_geom"] is False
        assert trip["has_end_geom"] is False
        assert trip["start_place_id"] is None
        assert trip["end_place_id"] is None
        assert trip["snap_status"] is None

    _run(scenario)


def test_routing_unavailable_with_blank_distance_returns_400_and_stores_nothing():
    async def scenario(pool):
        request = _request(pool, osrm_url="")
        with pytest.raises(HTTPException) as exc:
            await _add(
                request, route_mode="map", distance="",
                start_lat="47.60", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )
        assert exc.value.status_code == 400
        assert "osrm" not in exc.value.detail.lower()
        assert OSRM_URL not in exc.value.detail
        assert await _trip_count(pool) == 0

    _run(scenario)


def test_route_mode_none_never_calls_osrm():
    async def scenario(pool):
        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json={"code": "Ok", "routes": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _add(request, route_mode="none", distance="10")

        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == "/trips"
        assert called["count"] == 0
        trip = await _fetch_only_trip(pool)
        assert trip["has_path"] is False
        assert trip["snap_status"] is None

    _run(scenario)


def test_tampered_out_of_range_latitude_returns_400_and_stores_nothing():
    async def scenario(pool):
        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _add(
                request, route_mode="map", distance="",
                start_lat="91", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )
        assert exc.value.status_code == 400
        assert "91" not in exc.value.detail
        assert await _trip_count(pool) == 0

    _run(scenario)


def test_tampered_non_numeric_coordinate_returns_400_and_stores_nothing():
    async def scenario(pool):
        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _add(
                request, route_mode="map", distance="",
                start_lat="not-a-number", start_lon="-122.33",
                end_lat="47.70", end_lon="-122.20",
            )
        assert exc.value.status_code == 400
        assert await _trip_count(pool) == 0

    _run(scenario)


def test_tampered_unknown_place_id_returns_400_and_stores_nothing():
    async def scenario(pool):
        async with pool.connection() as conn:
            start_id = await _insert_place(conn, "Home", 47.60, -122.33)

        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _add(
                request, route_mode="places", distance="",
                start_place=str(start_id), end_place="999999",
            )
        assert exc.value.status_code == 400
        assert "999999" not in exc.value.detail
        assert await _trip_count(pool) == 0

    _run(scenario)
