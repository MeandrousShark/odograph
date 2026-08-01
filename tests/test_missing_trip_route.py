"""Tests for the OSRM `/route` road-distance suggestion. No database
needed -- `route_distance_m` is pure
request-building/response-parsing logic exercised against a fake transport,
the same `httpx.MockTransport` pattern tests/test_snap_db.py uses for `/match`.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.snap import route_distance_m


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_route_distance_m_parses_ok_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 2345.6}]})

    async def run():
        async with _client(handler) as client:
            return await route_distance_m(client, "http://osrm", 47.0, -122.0, 47.02, -122.0)

    distance = asyncio.run(run())
    assert distance == 2345.6
    assert "/route/v1/driving/-122.000000,47.000000;-122.000000,47.020000" in captured["url"]


def test_route_distance_m_returns_none_on_no_route():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "NoRoute", "routes": []})

    async def run():
        async with _client(handler) as client:
            return await route_distance_m(client, "http://osrm", 47.0, -122.0, 47.02, -122.0)

    assert asyncio.run(run()) is None


def test_route_distance_m_raises_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async def run():
        async with _client(handler) as client:
            return await route_distance_m(client, "http://osrm", 47.0, -122.0, 47.02, -122.0)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(run())


def test_route_distance_m_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "Error"})

    async def run():
        async with _client(handler) as client:
            return await route_distance_m(client, "http://osrm", 47.0, -122.0, 47.02, -122.0)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
