"""DB-backed tests for the manual trip form's Search route mode: create and
preview store/return the same shape as map mode, the provider gate, label
rejection, the routing-unavailable fallback, the page flag and account
isolation. Same direct-endpoint conventions as
tests/test_manual_routed_trip_db.py.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row

from app.db import make_pool
from app.main import make_templates
from app.rates import METERS_PER_MILE
from app.ui import MANUAL_ROUTE_UNAVAILABLE_NOTICE, make_router
from conftest import add_test_account, reset_account_db
from personal_support import personal_request

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}
OSRM_URL = "http://osrm.internal.test:5000"
PROVIDER = object()
COORDS = dict(start_lat="47.60", start_lon="-122.33", end_lat="47.70", end_lon="-122.20")


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


ADD = _endpoint("/trips/manual", "POST")
PREVIEW = _endpoint("/trips/manual/route-preview", "POST")
PAGE = _endpoint("/trips/manual", "GET")

CREATE_FORM = {
    "date": "2026-07-14", "start_time": "09:00", "end_time": "10:00", "distance": "",
    "category": "unclassified", "purpose": "", "notes": "", "vehicle_id": "",
    "route_mode": "search", "start_place": "", "end_place": "",
    "start_lat": "", "start_lon": "", "end_lat": "", "end_lon": "",
    "routed_distance": "", "exclusion": "", "start_label": "", "end_label": "",
}
PREVIEW_FORM = {
    "route_mode": "search", "start_place": "", "end_place": "",
    "start_lat": "", "start_lon": "", "end_lat": "", "end_lon": "",
}


def _request(pool, *, provider=PROVIDER, osrm_url=OSRM_URL, http_client=None):
    config = SimpleNamespace(
        osrm_url=osrm_url, display_tz=TZ, app_version="test", geocode_provider=provider,
        trips_page_size=25,
    )
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=config, osrm_http_client=http_client,
            templates=make_templates(config),
        )),
        session={"csrf": "test"},
    ))


async def _add(request, **overrides):
    return await ADD(request, user=USER, **{**CREATE_FORM, **overrides})


async def _preview(request, **overrides):
    return await PREVIEW(request, user=USER, **{**PREVIEW_FORM, **overrides})


async def _trips(pool) -> list[dict]:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT distance_m, snap_status::text AS snap_status, start_place_id, end_place_id, "
            "start_label, end_label, path IS NOT NULL AS has_path, "
            "start_geom IS NOT NULL AS has_start_geom, end_geom IS NOT NULL AS has_end_geom, "
            "ST_Y(start_geom::geometry) AS start_lat, ST_X(start_geom::geometry) AS start_lon, "
            "ST_Y(end_geom::geometry) AS end_lat, ST_X(end_geom::geometry) AS end_lon "
            "FROM trips"
        )
        return await cur.fetchall()


def _osrm_ok(distance=1500.0):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{
                "distance": distance,
                "geometry": {"type": "LineString", "coordinates": [[-122.33, 47.60], [-122.20, 47.70]]},
            }],
        })
    return handler


def _run(coro_factory) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            await coro_factory(pool, raw_pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


def test_search_create_stores_the_same_shape_as_map_mode():
    async def scenario(pool, raw_pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_osrm_ok(1500.0))) as client:
            request = _request(pool, http_client=client)
            for mode in ("map", "search"):
                response = await _add(request, route_mode=mode, **COORDS)
                assert response.status_code == 204
                assert response.headers["HX-Redirect"] == "/trips"

        map_trip, search_trip = await _trips(pool)
        assert search_trip == map_trip
        assert search_trip["start_place_id"] is None and search_trip["end_place_id"] is None
        assert search_trip["start_label"] is None and search_trip["end_label"] is None
        assert search_trip["has_path"] and search_trip["has_start_geom"] and search_trip["has_end_geom"]
        assert search_trip["start_lat"] == pytest.approx(47.60)
        assert search_trip["end_lon"] == pytest.approx(-122.20)
        assert search_trip["distance_m"] == pytest.approx(1500.0)

    _run(scenario)


def test_search_create_keeps_a_typed_distance_as_an_override():
    async def scenario(pool, raw_pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_osrm_ok(1500.0))) as client:
            request = _request(pool, http_client=client)
            await _add(request, distance="9.9", routed_distance="0.9", **COORDS)

        (trip,) = await _trips(pool)
        assert trip["distance_m"] == pytest.approx(9.9 * METERS_PER_MILE)
        assert trip["has_path"] is True

    _run(scenario)


def test_search_create_and_preview_are_refused_without_a_geocoder():
    async def scenario(pool, raw_pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_osrm_ok())) as client:
            request = _request(pool, provider=None, http_client=client)
            with pytest.raises(HTTPException) as created:
                await _add(request, **COORDS)
            with pytest.raises(HTTPException) as previewed:
                await _preview(request, **COORDS)

        for exc in (created, previewed):
            assert exc.value.status_code == 400
            assert "47.6" not in exc.value.detail
        assert await _trips(pool) == []

    _run(scenario)


@pytest.mark.parametrize("override", [
    {"start_lat": "91"}, {"end_lon": "-181"}, {"start_lon": ""}, {"end_lat": "not-a-number"},
])
def test_search_create_and_preview_reject_bad_coordinates_without_echo(override):
    async def scenario(pool, raw_pool):
        request = _request(pool)
        values = {**COORDS, **override}
        with pytest.raises(HTTPException) as created:
            await _add(request, **values)
        with pytest.raises(HTTPException) as previewed:
            await _preview(request, **values)
        for exc in (created, previewed):
            assert exc.value.status_code == 400
            for value in override.values():
                assert value == "" or value not in exc.value.detail
        assert await _trips(pool) == []

    _run(scenario)


def test_search_create_rejects_labels_before_touching_the_database():
    async def scenario(pool, raw_pool):
        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _add(request, start_label="Airport", **COORDS)
        assert exc.value.status_code == 400
        assert await _trips(pool) == []

    _run(scenario)


def test_search_routing_unavailable_with_typed_distance_matches_map_mode():
    async def scenario(pool, raw_pool):
        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"code": "Error"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as client:
            request = _request(pool, http_client=client)
            responses = [
                await _add(request, route_mode=mode, distance="8.5", **COORDS)
                for mode in ("map", "search")
            ]

        for response in responses:
            assert response.status_code == 204
            assert response.headers["HX-Redirect"] == f"/trips?notice={MANUAL_ROUTE_UNAVAILABLE_NOTICE}"
        map_trip, search_trip = await _trips(pool)
        assert search_trip == map_trip
        assert search_trip["distance_m"] == pytest.approx(8.5 * METERS_PER_MILE)
        assert search_trip["has_path"] is False
        assert search_trip["start_place_id"] is None and search_trip["end_place_id"] is None

    _run(scenario)


def test_search_routing_unavailable_with_blank_distance_is_400():
    async def scenario(pool, raw_pool):
        request = _request(pool, osrm_url="")
        with pytest.raises(HTTPException) as exc:
            await _add(request, **COORDS)
        assert exc.value.status_code == 400
        assert await _trips(pool) == []

    _run(scenario)


def test_search_preview_returns_the_map_mode_shape():
    async def scenario(pool, raw_pool):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_osrm_ok(2345.6))) as client:
            request = _request(pool, http_client=client)
            bodies = [
                json.loads((await _preview(request, route_mode=mode, **COORDS)).body)
                for mode in ("map", "search")
            ]

        assert bodies[0] == bodies[1]
        assert set(bodies[1]) == {"ok", "distance_m", "distance_miles", "geometry", "start", "end"}
        assert bodies[1]["start"] == pytest.approx([47.60, -122.33])

    _run(scenario)


def test_search_preview_without_osrm_is_unavailable_not_an_error():
    async def scenario(pool, raw_pool):
        response = await _preview(_request(pool, osrm_url=""), **COORDS)
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}

    _run(scenario)


@pytest.mark.parametrize("provider,present", [(PROVIDER, True), (None, False)])
def test_manual_page_offers_search_only_with_a_geocoder(provider, present):
    async def scenario(pool, raw_pool):
        response = await PAGE(_request(pool, provider=provider), USER)
        body = response.body.decode()
        assert ('value="search"' in body) is present
        assert ('data-route-panel="search"' in body) is present
        assert 'value="map"' in body and 'value="places"' in body

    _run(scenario)


def test_search_mode_trip_is_invisible_to_another_account():
    async def scenario(pool, raw_pool):
        other = await add_test_account(raw_pool, 42, email="other@example.invalid")
        async with httpx.AsyncClient(transport=httpx.MockTransport(_osrm_ok())) as client:
            await _add(_request(pool, http_client=client), **COORDS)

        assert len(await _trips(pool)) == 1
        assert await _trips(other) == []
        async with other.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM trips")
            assert (await cur.fetchone())[0] == 0

    _run(scenario)
