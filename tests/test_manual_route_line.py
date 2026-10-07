"""Tests for the OSRM `/route` geometry helper used by routed manual trip
entry. No database needed -- `route_line` is pure request-building/response-
parsing logic exercised against a fake transport, the same
`httpx.MockTransport` pattern tests/test_missing_trip_route.py uses for
`route_distance_m`.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import Request

from app.snap import (
    PROVIDER_ROUTE_MAX_VERTICES, ProviderOutputTooLarge, RoutedLine,
    _parse_route_line, route_line,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(handler, **kwargs):
    async def run():
        async with _client(handler) as client:
            return await route_line(
                client, "http://osrm",
                kwargs.get("from_lat", 47.0), kwargs.get("from_lon", -122.0),
                kwargs.get("to_lat", 47.02), kwargs.get("to_lon", -122.0),
            )

    return asyncio.run(run())


def _ok_body(distance=2345.6, coordinates=None, extra_geometry_keys=None):
    geometry = {
        "type": "LineString",
        "coordinates": coordinates if coordinates is not None else [[-122.0, 47.0], [-122.0, 47.02]],
    }
    if extra_geometry_keys:
        geometry.update(extra_geometry_keys)
    return {"code": "Ok", "routes": [{"distance": distance, "geometry": geometry}]}


def test_route_line_parses_ok_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ok_body())

    result = _run(handler)
    assert result == RoutedLine(
        distance_m=2345.6,
        geojson={"type": "LineString", "coordinates": [[-122.0, 47.0], [-122.0, 47.02]]},
    )
    assert "overview=full" in captured["url"]
    assert "geometries=geojson" in captured["url"]
    assert "/route/v1/driving/-122.000000,47.000000;-122.000000,47.020000" in captured["url"]


def test_route_line_drops_extra_geometry_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(extra_geometry_keys={"bbox": [1, 2, 3, 4], "properties": {"x": 1}}),
        )

    result = _run(handler)
    assert result.geojson == {
        "type": "LineString",
        "coordinates": [[-122.0, 47.0], [-122.0, 47.02]],
    }
    assert set(result.geojson.keys()) == {"type", "coordinates"}


def test_route_line_rejects_excess_provider_vertices_without_clipping():
    coordinates = [[-122.0, 47.0]] * PROVIDER_ROUTE_MAX_VERTICES
    assert len(_parse_route_line(_ok_body(coordinates=coordinates)).geojson["coordinates"]) == (
        PROVIDER_ROUTE_MAX_VERTICES
    )
    with pytest.raises(ProviderOutputTooLarge):
        _parse_route_line(_ok_body(coordinates=coordinates + [[-122.0, 47.02]]))


def test_route_line_returns_none_on_bad_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "NoRoute", "routes": [{"distance": 1.0, "geometry": {}}]})

    assert _run(handler) is None


def test_route_line_returns_none_on_empty_routes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "Ok", "routes": []})

    assert _run(handler) is None


def test_route_line_returns_none_on_nonfinite_distance():
    def handler(request: httpx.Request) -> httpx.Response:
        # json.dumps emits a bare NaN token, which json.loads accepts even
        # though it isn't valid JSON -- exactly the case parse_finite_number
        # guards against.
        raw = json.dumps(_ok_body(distance=float("nan")))
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    assert _run(handler) is None


def test_route_line_returns_none_on_missing_distance():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body()
        del body["routes"][0]["distance"]
        return httpx.Response(200, json=body)

    assert _run(handler) is None


def test_route_line_returns_none_on_negative_distance():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(distance=-1.0))

    assert _run(handler) is None


def test_route_line_returns_none_on_zero_distance():
    # A zero-length route (the two endpoints coincide) isn't a usable route,
    # and the hand-entered manual-trip path never accepts a zero distance
    # either, so this must be rejected the same as a negative one.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(distance=0.0))

    assert _run(handler) is None


def test_route_line_returns_none_on_missing_geometry():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body()
        del body["routes"][0]["geometry"]
        return httpx.Response(200, json=body)

    assert _run(handler) is None


def test_route_line_returns_none_on_wrong_geometry_type():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body()
        body["routes"][0]["geometry"]["type"] = "Point"
        return httpx.Response(200, json=body)

    assert _run(handler) is None


def test_route_line_returns_none_on_too_few_coordinates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(coordinates=[[-122.0, 47.0]]))

    assert _run(handler) is None


def test_route_line_returns_none_on_non_pair_coordinate_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body(coordinates=[[-122.0, 47.0, 1.0], [-122.0, 47.02]])
        )

    assert _run(handler) is None


def test_route_line_returns_none_on_string_coordinate_value():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body(coordinates=[["-122.0", 47.0], [-122.0, 47.02]])
        )

    assert _run(handler) is None


def test_route_line_returns_none_on_boolean_coordinate_value():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body(coordinates=[[True, 47.0], [-122.0, 47.02]])
        )

    assert _run(handler) is None


def test_route_line_returns_none_on_out_of_range_longitude():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body(coordinates=[[-181.0, 47.0], [-122.0, 47.02]])
        )

    assert _run(handler) is None


def test_route_line_returns_none_on_out_of_range_latitude():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body(coordinates=[[-122.0, 91.0], [-122.0, 47.02]])
        )

    assert _run(handler) is None


def test_route_line_raises_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(httpx.ConnectError):
        _run(handler)


def test_route_line_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "Error"})

    with pytest.raises(httpx.HTTPStatusError):
        _run(handler)


@pytest.mark.capacity_contract
def test_route_line_supports_direct_library_calls_without_admission():
    from app.capacity import current_owner
    assert current_owner() is None
    assert _run(lambda request: httpx.Response(200, json=_ok_body())).distance_m == 2345.6


@pytest.mark.capacity_contract
@pytest.mark.parametrize("path, failure", [
    ("/trips/manual/route-preview", None),
    ("/trips/manual/route-preview", "oversized"),
    ("/trips/manual/route-preview", "timeout"),
    ("/trips/manual", "oversized"), ("/trips/manual", "timeout"),
])
def test_manual_routing_retains_owner_and_original_form_limits(monkeypatch, path, failure):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import threading

    from fastapi import FastAPI, Request
    from app.account_context import AccountPrincipal
    from app.auth import require_csrf, require_user
    from app.capacity import AdmissionManager, current_owner
    from app.ui import make_router
    import app.snap as snap

    async def run():
        app = FastAPI()
        settings = SimpleNamespace(capacity_auth_body_timeout_s=.02) if failure == "timeout" else None
        manager = app.state.capacity = AdmissionManager(settings)
        app.state.control_pool = object()
        lease_live = False
        parsed_in_thread = False
        provider_called = False
        db_borrows = 0

        @asynccontextmanager
        async def lease(pool, account):
            nonlocal lease_live
            assert current_owner().lane == "foreground"
            lease_live = True
            try:
                yield
            finally:
                lease_live = False

        class CoordinatesPool:
            @asynccontextmanager
            async def connection(self):
                nonlocal db_borrows
                db_borrows += 1
                yield object()  # The map-coordinate path needs no SQL.

        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            request.state.account_pool = CoordinatesPool()
            request.state.config = SimpleNamespace(osrm_url="http://osrm")
            return {"id": 1}

        original_parse = snap._parse_route_line
        def parse(body):
            nonlocal parsed_in_thread
            assert lease_live and current_owner().lane == "foreground"
            assert threading.current_thread() is not threading.main_thread()
            parsed_in_thread = True
            return original_parse(body)

        def provider(request):
            nonlocal provider_called
            provider_called = True
            assert lease_live and current_owner().lane == "foreground"
            return httpx.Response(200, json=_ok_body())

        monkeypatch.setattr("app.capacity_routes.external_account_work", lease)
        monkeypatch.setattr(snap, "_parse_route_line", parse)
        app.dependency_overrides[require_user] = identity
        app.dependency_overrides[require_csrf] = lambda: None
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as osrm:
            app.state.osrm_http_client = osrm
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                if failure == "timeout":
                    async def slow_body():
                        yield b"route_mode=map&"
                        await asyncio.sleep(.04)
                        yield b"start_lat=47&start_lon=-122&end_lat=47.02&end_lon=-122"
                    response = await client.post(path, content=slow_body(),
                        headers={"content-type": "application/x-www-form-urlencoded"})
                else:
                    response = await client.post(path, data={
                        "route_mode": "map", "start_lat": "x" * 70000 if failure == "oversized" else "47",
                        "start_lon": "-122", "end_lat": "47.02", "end_lon": "-122",
                    })
        if failure:
            assert response.status_code == (413 if failure == "oversized" else 503)
            assert not parsed_in_thread and not provider_called
            assert db_borrows == 0
        else:
            assert response.status_code == 200
            assert response.json()["ok"] is True
            assert response.json()["distance_m"] == 2345.6
            assert parsed_in_thread and provider_called
        assert not lease_live and not any(manager._active.values())

    asyncio.run(run())
