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

from app.snap import RoutedLine, route_line


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
