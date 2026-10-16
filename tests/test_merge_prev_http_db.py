"""DB-backed, full-HTTP coverage for the trip page's "Merge with previous"
direction.

The trip page posts one merge field set to either `/trips/{id}/merge_prev`
or `/trips/{id}/merge_next`. `tests/test_merge_next_category_default_db.py`
drives the next direction through the real route; this mirrors it for the
previous direction, from the later trip, with the fields the shared form
sends when only Purpose and Notes are filled in.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.auth import AuthRedirect
from app.config import Config
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.main import make_templates
from app.ui import make_router as make_ui_router
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.control_pool = pool.control_pool
    app.state.dev_principal = pool.principal
    app.state.make_detector_runner = lambda bound: DetectorRunner(bound, Params())
    with patch.dict(os.environ, {
        "DATABASE_URL": TEST_DB, "SESSION_SECRET": "test-secret-long-enough-for-config", "DEV_NO_AUTH": "1",
    }, clear=True):
        app.state.config = Config.from_env()
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request, exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(make_ui_router())
    return app


async def _csrf(client: httpx.AsyncClient) -> str:
    page = await client.get("/settings")
    return CSRF_RE.search(page.text).group(1)


async def _insert_points(conn, points, device):
    stream = await seed_tracking_device(conn, device)
    for point in points:
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (account_id(conn), stream, device, point.t, point.t, point.lon, point.lat, point.accuracy_m, point.velocity_kmh),
        )


async def _trips(conn, device):
    cur = await conn.execute(
        "SELECT id, category::text, tag_source::text, purpose, notes FROM trips "
        "WHERE device=%s AND source='detected' ORDER BY started_at",
        (device,),
    )
    return await cur.fetchall()


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, track, "A")

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            before = await _trips(conn, "A")
        assert len(before) == 2

        # Same setup as the merge_next test: a real, non-first category with
        # no tag_source, so "keep" is distinguishable from a silent default.
        async with pool.connection() as conn:
            for trip_id, *_ in before:
                await conn.execute(
                    "UPDATE trips SET category = 'personal', tag_source = NULL WHERE id = %s",
                    (trip_id,),
                )

        app = _bare_app(pool)
        app.state.detector_runner = runner
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            first_trip_id, second_trip_id = before[0][0], before[1][0]

            # The earlier trip has no previous neighbor to merge with.
            refused = await client.post(
                f"/trips/{first_trip_id}/merge_prev",
                data={"category": "keep", "vehicle_id": "keep"},
                headers={"X-CSRF-Token": csrf},
            )
            assert refused.status_code == 400
            assert "no adjacent trip" in refused.json()["detail"].lower()

            response = await client.post(
                f"/trips/{second_trip_id}/merge_prev",
                data={
                    "category": "keep", "purpose": "Client visit",
                    "notes": "Merged at the client's office", "vehicle_id": "keep",
                },
                headers={"X-CSRF-Token": csrf},
            )
        assert response.status_code == 204

        async with pool.connection() as conn:
            after = await _trips(conn, "A")
        assert len(after) == 1
        merged_id, category, tag_source, purpose, notes = after[0]
        assert response.headers["HX-Redirect"] == f"/trips/{merged_id}"
        assert category == "personal"
        assert tag_source != "human"
        assert purpose == "Client visit"
        assert notes == "Merged at the client's office"
    finally:
        await raw_pool.close()


def test_merge_prev_merges_the_later_trip_into_its_predecessor_over_http():
    asyncio.run(_scenario())
