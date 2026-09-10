"""DB-backed, full-HTTP regression test for the trip-page merge forms'
category default.

`tests/test_ui_merge_db.py` calls the merge endpoint functions directly,
which can't exercise a genuinely *missing* form field (a `Form("keep")`
default is only resolved by FastAPI's own request parsing, not a plain
Python call) -- and can't exercise the template's `<select>` at all. This
test drives the real `/trips/{id}/merge_next` route through an HTTP client,
posting no `category` field, the same request a browser sends when the
merge form's category `<select>` is left untouched.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.auth import AuthRedirect
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.main import make_templates
from app.ui import make_router as make_ui_router
from conftest import reset_db
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.config = SimpleNamespace(
        dev_no_auth=True, display_tz=timezone.utc,
        geocode_provider=None, app_version="test", app_git_revision="test",
    )
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
    for point in points:
        await conn.execute(
            "INSERT INTO points (device, recorded_at, received_at, geom, accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (device, point.t, point.t, point.lon, point.lat, point.accuracy_m, point.velocity_kmh),
        )


async def _trips(conn, device):
    cur = await conn.execute(
        "SELECT id, category::text, tag_source::text FROM trips "
        "WHERE device=%s AND source='detected' ORDER BY started_at",
        (device,),
    )
    return await cur.fetchall()


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
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

        # Pre-set both halves to a real, non-first-alphabetical category
        # ("personal", not "business" -- CATEGORIES' first entry, and the
        # option a browser auto-selects from an untouched <select> with no
        # explicit "Keep" option). If the endpoint's Form default or the
        # template fix regresses, the merge would instead post "business"
        # and silently reclassify + human-lock the result. tag_source stays
        # NULL, not 'rule': the merge's own reprocess step re-runs
        # auto-tagging, which would revert a 'rule'-tagged trip with no
        # matching rule back to unclassified regardless of this fix, which
        # isn't what this test is isolating.
        async with pool.connection() as conn:
            for trip_id, _, _ in before:
                await conn.execute(
                    "UPDATE trips SET category = 'personal', tag_source = NULL WHERE id = %s",
                    (trip_id,),
                )

        app = _bare_app(pool)
        app.state.detector_runner = runner
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await _csrf(client)
            first_trip_id = before[0][0]
            response = await client.post(
                f"/trips/{first_trip_id}/merge_next",
                data={},
                headers={"X-CSRF-Token": csrf},
            )
        assert response.status_code == 204

        async with pool.connection() as conn:
            after = await _trips(conn, "A")
        assert len(after) == 1
        merged_id, category, tag_source = after[0]
        assert category == "personal", (
            "an untouched merge_next post (no category field at all) must preserve "
            "the pre-merge category, not fall back to CATEGORIES' first entry"
        )
        assert tag_source != "human", (
            "a merge the user didn't ask to reclassify must not human-lock the result"
        )
    finally:
        await pool.close()


def test_merge_next_with_no_category_field_preserves_pre_merge_category():
    """Regression test for the trip-page merge_next/merge_prev forms: before
    trip.html's category <select> had a "Keep" option and
    app/ui/merge_split.py's endpoints defaulted to Form("keep"), an
    untouched merge form posted whatever CATEGORIES' first entry was (a
    real category, "business") and silently human-locked the merged trip."""
    asyncio.run(_scenario())
