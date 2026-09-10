"""DB-backed test for the trip-detail merge-button EXISTS probes vs.
portable-imported trips.

Route handler called directly (bypassing FastAPI's dependency injection,
same pattern as tests/test_ui_merge_db.py and tests/test_review_db.py) with
a real Jinja2Templates instance so TemplateResponse.context can be asserted
on without needing a running app or HTTP client.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.main import make_templates
from app.ui import make_router
from conftest import reset_db
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

DEVICE = "TESTDEV"
TZ = timezone.utc


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/{trip_id}":
            return route.endpoint
    raise AssertionError("trip detail route missing")


TRIP_DETAIL = _endpoint()


def _request(pool):
    config = SimpleNamespace(display_tz=TZ, app_version="test", detector_params=Params())
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    )


async def _insert_points(conn, points) -> None:
    for p in points:
        await conn.execute(
            "INSERT INTO points (device, recorded_at, received_at, geom, "
            " accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (DEVICE, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
        )


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)

        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, track)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, started_at, ended_at FROM trips WHERE device = %s "
                "AND source = 'detected' ORDER BY started_at",
                (DEVICE,),
            )
            trip_a, trip_b = await cur.fetchall()

            # A portable-imported trip (app/portable/importer.py) sitting right
            # before trip_a: no backing points in this instance, so it must
            # never count as a real "adjacent trip" for the merge button, even
            # though it's the nearest trip to trip_a in started_at order.
            await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, imported) "
                "VALUES (%s, 'detected', %s, %s, 1000, true)",
                (DEVICE, trip_a[1] - timedelta(hours=1), trip_a[1] - timedelta(minutes=30)),
            )

        request = _request(pool)

        response = await TRIP_DETAIL(request, trip_a[0], {"sub": "test"})
        assert response.context["has_prev_trip"] is False, (
            "the only earlier trip is a portable import with no points in this "
            "instance; it must not offer a merge-with-previous button"
        )
        assert response.context["has_next_trip"] is True, (
            "trip_b is a genuine adjacent detected trip; the guard must not "
            "disable a real merge candidate"
        )

        response = await TRIP_DETAIL(request, trip_b[0], {"sub": "test"})
        assert response.context["has_prev_trip"] is True
        assert response.context["has_next_trip"] is False
    finally:
        await pool.close()


def test_trip_detail_merge_probe_ignores_imported_neighbor():
    """app/ui/trips.py's trip-detail merge-button EXISTS probes filtered only on
    source = 'detected', unlike the actual neighbor lookup
    (_merge_with_neighbor) which also requires NOT imported. After an
    import, the first live-tracked trip showed a "merge with previous"
    button that always 400ed with "No adjacent trip to merge with". This is
    the regression test for that guard."""
    asyncio.run(_scenario())
