"""DB-backed tests for the missing-trip badge's prefill flow through the
`manual_trip_page()` route itself: the
`manual_date`/`manual_start`/`manual_notes`/`bridge_trip` query params it
receives, and the one-shot OSRM `/route` suggestion resolved only when that
link is actually followed. Route handler called directly (bypassing
FastAPI's dependency injection), same pattern as tests/test_review_db.py.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("UTC")
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=TZ)


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/manual" and "GET" in (route.methods or set()):
            return route.endpoint
    raise AssertionError("manual trip page route missing")


MANUAL_PAGE = _endpoint()


def _request(pool, osrm_url="", osrm_http_client=None):
    config = SimpleNamespace(
        display_tz=TZ, trips_page_size=25, osrm_url=osrm_url, app_version="test",
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
            osrm_http_client=osrm_http_client,
        )),
        session={"csrf": "test-csrf"},
    )


async def _insert_detected_trip(conn, device, started_at, ended_at, lat, lon) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, start_geom, end_geom) "
        "VALUES (%s, 'detected', %s, %s, 1000, 2, 2, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) RETURNING id",
        (device, started_at, ended_at, lon, lat, lon, lat),
    )
    return (await cur.fetchone())[0]


async def _call_page(request, **prefill):
    return await MANUAL_PAGE(
        request, {"sub": "test"},
        manual_date=prefill.get("manual_date", ""),
        manual_start=prefill.get("manual_start", ""),
        manual_notes=prefill.get("manual_notes", ""),
        bridge_trip=prefill.get("bridge_trip", ""),
    )


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip_a = await _insert_detected_trip(
                conn, "IDX", T0, T0 + timedelta(minutes=10), 47.0000, -122.0000
            )
            trip_b = await _insert_detected_trip(
                conn, "IDX", T0 + timedelta(minutes=30), T0 + timedelta(minutes=40),
                47.0200, -122.0000,
            )

        # No prefill params at all: unchanged, no manual_prefill.
        response = await _call_page(_request(pool))
        assert response.context["manual_prefill"] is None

        # Prefill params present but OSRM unconfigured: identical prefill,
        # no suggestion (acceptance criterion 6, unconfigured half).
        response = await _call_page(
            _request(pool, osrm_url=""),
            manual_date="2026-07-01", manual_start="08:10",
            manual_notes="bridge: A → B", bridge_trip=str(trip_b),
        )
        prefill = response.context["manual_prefill"]
        assert prefill == {
            "date": "2026-07-01", "start_time": "08:10",
            "notes": "bridge: A → B", "osrm_hint": None,
        }

        # OSRM configured and bridge_trip resolves real coordinates:
        # exactly one /route call, hint rendered (acceptance criterion 6,
        # configured half).
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 2414.016}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as osrm_client:
            response = await _call_page(
                _request(pool, osrm_url="http://osrm", osrm_http_client=osrm_client),
                manual_date="2026-07-01", manual_start="08:10",
                manual_notes="bridge: A → B", bridge_trip=str(trip_b),
            )
        assert len(calls) == 1
        assert response.context["manual_prefill"]["osrm_hint"] == "~1.5 mi by road"

        # A stale/malformed bridge_trip degrades to no hint, not an error.
        response = await _call_page(
            _request(pool, osrm_url="http://osrm", osrm_http_client=osrm_client),
            manual_date="2026-07-01", manual_start="08:10", bridge_trip="not-an-id",
        )
        assert response.context["manual_prefill"]["osrm_hint"] is None
    finally:
        await pool.close()


def test_index_manual_prefill_and_osrm_suggestion():
    asyncio.run(_scenario())
