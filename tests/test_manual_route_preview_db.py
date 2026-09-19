"""DB-backed tests for POST /trips/manual/route-preview: the manual-trip
form's routing preview, which resolves a place-pair or map-picked pair of
coordinates and calls OSRM, but never writes anything. Same fixture
conventions as tests/test_trip_card_edit_db.py: a reset database per test via
reset_db, and the route's endpoint function called directly (bypassing
FastAPI's own dependency injection, which never runs on a bare function
call) rather than through a live ASGI app.
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

from app.db import make_pool
from app.account_context import account_id
from personal_support import personal_request
from app.ui import make_router
from conftest import reset_account_db

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


PREVIEW = _endpoint("/trips/manual/route-preview", "POST")

DEFAULT_FORM = {
    "route_mode": "none", "start_place": "", "end_place": "",
    "start_lat": "", "start_lon": "", "end_lat": "", "end_lon": "",
}


def _request(pool, *, osrm_url=OSRM_URL, http_client=None):
    config = SimpleNamespace(osrm_url=osrm_url, display_tz=TZ, app_version="test")
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=config, osrm_http_client=http_client,
        )),
        session={"csrf": "test"},
    ))


async def _preview(request, **overrides):
    values = dict(DEFAULT_FORM)
    values.update(overrides)
    return await PREVIEW(request, user=USER, **values)


async def _insert_place(conn, name, lat, lon) -> int:
    row = await conn.execute(
        "INSERT INTO places (account_id, name, geom) VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, "
        "%s), 4326)::geography) RETURNING id",
        (account_id(conn), name, lon, lat,),
    )
    return (await row.fetchone())[0]


def _ok_body(distance=2345.6, coordinates=None):
    return {
        "code": "Ok",
        "routes": [{
            "distance": distance,
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates or [[-122.33, 47.60], [-122.20, 47.70]],
            },
        }],
    }


def test_preview_route_requires_auth_and_csrf():
    routes = [r for r in make_router().routes if r.path == "/trips/manual/route-preview"]
    assert routes and "POST" in routes[0].methods
    names = {dependency.call.__name__ for dependency in routes[0].dependant.dependencies}
    assert names == {"require_user", "require_csrf"}


def _run(coro_factory) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            await coro_factory(pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


def test_preview_resolves_named_places_and_returns_safe_shape():
    async def scenario(pool):
        async with pool.connection() as conn:
            start_id = await _insert_place(conn, "Home", 47.60, -122.33)
            end_id = await _insert_place(conn, "Work", 47.70, -122.20)

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_ok_body())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _preview(
                request, route_mode="places",
                start_place=str(start_id), end_place=str(end_id),
            )

        assert OSRM_URL in captured["url"]
        body = json.loads(response.body)
        assert body["ok"] is True
        assert set(body.keys()) == {"ok", "distance_m", "distance_miles", "geometry", "start", "end"}
        assert body["distance_m"] == pytest.approx(2345.6)
        assert body["distance_miles"] == "1.5"
        assert body["geometry"] == {
            "type": "LineString",
            "coordinates": [[-122.33, 47.60], [-122.20, 47.70]],
        }
        assert body["start"] == pytest.approx([47.60, -122.33])
        assert body["end"] == pytest.approx([47.70, -122.20])
        assert OSRM_URL not in response.body.decode()

    _run(scenario)


def test_preview_resolves_map_coordinates():
    async def scenario(pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body(distance=1000.0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _preview(
                request, route_mode="map",
                start_lat="47.6", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )

        body = json.loads(response.body)
        assert body["ok"] is True
        assert body["start"] == pytest.approx([47.6, -122.33])
        assert body["end"] == pytest.approx([47.7, -122.20])

    _run(scenario)


def test_preview_rejects_out_of_range_map_coordinate():
    async def scenario(pool):
        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _preview(
                request, route_mode="map",
                start_lat="91", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )
        assert exc.value.status_code == 400
        assert "91" not in exc.value.detail

    _run(scenario)


def test_preview_rejects_non_numeric_map_coordinate():
    async def scenario(pool):
        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _preview(
                request, route_mode="map",
                start_lat="not-a-number", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )
        assert exc.value.status_code == 400

    _run(scenario)


def test_preview_rejects_unknown_place_id():
    async def scenario(pool):
        async with pool.connection() as conn:
            start_id = await _insert_place(conn, "Home", 47.60, -122.33)

        request = _request(pool)
        with pytest.raises(HTTPException) as exc:
            await _preview(
                request, route_mode="places",
                start_place=str(start_id), end_place="999999",
            )
        assert exc.value.status_code == 400
        assert "999999" not in exc.value.detail

    _run(scenario)


def test_preview_returns_unavailable_when_osrm_not_configured():
    async def scenario(pool):
        request = _request(pool, osrm_url="")
        response = await _preview(
            request, route_mode="map",
            start_lat="47.6", start_lon="-122.33",
            end_lat="47.7", end_lon="-122.20",
        )
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}

    _run(scenario)


def test_preview_returns_unavailable_when_no_http_client():
    async def scenario(pool):
        request = _request(pool, http_client=None)
        response = await _preview(
            request, route_mode="map",
            start_lat="47.6", start_lon="-122.33",
            end_lat="47.7", end_lon="-122.20",
        )
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}

    _run(scenario)


def test_preview_returns_unavailable_on_transport_error():
    async def scenario(pool):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _preview(
                request, route_mode="map",
                start_lat="47.6", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}
        assert OSRM_URL not in response.body.decode()

    _run(scenario)


def test_preview_returns_unavailable_on_non_2xx_status():
    async def scenario(pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"code": "Error"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _preview(
                request, route_mode="map",
                start_lat="47.6", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}
        assert OSRM_URL not in response.body.decode()

    _run(scenario)


def test_preview_returns_unavailable_on_no_route():
    async def scenario(pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "NoRoute", "routes": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _preview(
                request, route_mode="map",
                start_lat="47.6", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}

    _run(scenario)


def test_preview_returns_unavailable_on_malformed_body():
    async def scenario(pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = _request(pool, http_client=client)
            response = await _preview(
                request, route_mode="map",
                start_lat="47.6", start_lon="-122.33",
                end_lat="47.7", end_lon="-122.20",
            )
        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": False, "reason": "unavailable"}

    _run(scenario)
