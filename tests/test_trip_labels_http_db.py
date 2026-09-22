"""DB-backed HTTP coverage for manual trip endpoint labels.

The archive edit route receives all of its values through FastAPI form
coercion, so this exercises a real ASGI request to prove an empty submitted
label clears instead of being mistaken for an omitted field. The detail label
route is covered through the same HTTP boundary for successful saves,
validation errors, auth, and endpoint eligibility.
"""
from __future__ import annotations

import asyncio
import os
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.auth import AuthRedirect
from app.db import make_pool
from app.account_context import account_id
from personal_support import configure_personal_app, fixture_device
from app.main import make_templates
from app.ui import make_router
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
TZ = ZoneInfo("America/Los_Angeles")
CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')


def _bare_app(pool, *, dev_no_auth: bool = True) -> FastAPI:
    app = FastAPI()
    configure_personal_app(app, pool)
    app.state.config = SimpleNamespace(
        dev_no_auth=dev_no_auth,
        display_tz=TZ,
        app_version="test",
        detector_params=SimpleNamespace(min_trip_distance_m=300.0),
    )
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request, exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(make_router())
    return app


def _scenario(coro_factory) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            await coro_factory(pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


async def _insert_manual(conn, **overrides) -> int:
    values = {
        "started_at": "2026-07-14T16:00:00Z",
        "ended_at": "2026-07-14T17:00:00Z",
        "distance_m": 1609.344,
        "category": "unclassified",
        "purpose": "Errand",
        "notes": "Keep this note",
        "vehicle_id": None,
    }
    values.update(overrides)
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category,"
        " purpose, notes, vehicle_id, start_label, end_label) VALUES (%s, 'manual', 'manual', %s, "
        "%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (account_id(conn), *(tuple(values.values()) + (None, None)),),
    )
    return (await cur.fetchone())[0]


async def _insert_detected(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, "
        "start_geom, end_geom, distance_m, point_count, category, detector_version, snap_status) "
        "VALUES (%s, %s, 'phone', 'detected', '2026-07-14T18:00:00Z', '2026-07-14T19:00:00Z', "
        "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, ST_SetSRID(ST_MakePoint(-122.2, "
        "47.7), 4326)::geography, 3200, 2, 'business', 2, 'failed') RETURNING id", (account_id(conn), await fixture_device(conn, 'phone'),)
    )
    return (await cur.fetchone())[0]


async def _insert_routed_manual(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, start_geom, end_geom,"
        " distance_m, category) VALUES (%s, 'manual', 'manual', '2026-07-14T20:00:00Z', "
        "'2026-07-14T21:00:00Z', ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, "
        "ST_SetSRID(ST_MakePoint(-122.2, 47.7), 4326)::geography, 1609.344, 'unclassified') "
        "RETURNING id", (account_id(conn),)
    )
    return (await cur.fetchone())[0]


async def _labels(pool, trip_id: int) -> tuple:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT start_label, end_label, purpose, notes FROM trips WHERE id = %s",
            (trip_id,),
        )
        return await cur.fetchone()


async def _unrelated_fields(pool, trip_id: int) -> tuple:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT started_at, ended_at, distance_m, category::text, exclusion::text, "
            "purpose, notes, vehicle_id, tag_source::text FROM trips WHERE id = %s",
            (trip_id,),
        )
        return await cur.fetchone()


def test_http_label_clear_detail_validation_and_eligibility():
    async def run(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_manual(conn)
            detected_id = await _insert_detected(conn)
            routed_id = await _insert_routed_manual(conn)

        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            detail = await client.get(f"/trips/{trip_id}")
            assert detail.status_code == 200
            csrf = CSRF_RE.search(detail.text).group(1)
            assert 'name="start_label" value="" maxlength="100"' in detail.text

            no_csrf = await client.post(
                f"/trips/{trip_id}/labels", data={"start_label": "Blocked"}
            )
            assert no_csrf.status_code == 403

            edit_data = {
                "category": "unclassified", "purpose": "Errand",
                "notes": "Keep this note", "vehicle_id": "",
                "date": "2026-07-14", "start_time": "09:00",
                "end_time": "10:00", "distance": "1",
                "start_label": "Home", "end_label": "Office",
            }
            added = await client.post(
                f"/trips/{trip_id}/edit", headers={"X-CSRF-Token": csrf}, data=edit_data
            )
            assert added.status_code == 200
            assert await _labels(pool, trip_id) == (
                "Home", "Office", "Errand", "Keep this note"
            )

            archive_invalid = await client.post(
                f"/trips/{trip_id}/edit",
                headers={"X-CSRF-Token": csrf},
                data={**edit_data, "start_label": "X" * 101},
            )
            assert archive_invalid.status_code == 200
            assert 'role="alert"' in archive_invalid.text
            assert await _labels(pool, trip_id) == (
                "Home", "Office", "Errand", "Keep this note"
            )

            omitted = dict(edit_data)
            omitted.pop("start_label")
            omitted.pop("end_label")
            omitted["notes"] = "Updated unrelated field"
            preserved = await client.post(
                f"/trips/{trip_id}/edit", headers={"X-CSRF-Token": csrf}, data=omitted
            )
            assert preserved.status_code == 200
            assert await _labels(pool, trip_id) == (
                "Home", "Office", "Errand", "Updated unrelated field"
            )

            cleared = await client.post(
                f"/trips/{trip_id}/edit",
                headers={"X-CSRF-Token": csrf},
                data={**omitted, "start_label": "", "end_label": ""},
            )
            assert cleared.status_code == 200
            assert await _labels(pool, trip_id) == (
                None, None, "Errand", "Updated unrelated field"
            )

            unrelated_before_detail = await _unrelated_fields(pool, trip_id)
            detail_save = await client.post(
                f"/trips/{trip_id}/labels",
                headers={"X-CSRF-Token": csrf},
                data={"start_label": "Home", "end_label": "Office"},
            )
            assert detail_save.status_code == 204
            assert detail_save.headers["hx-redirect"] == f"/trips/{trip_id}"
            assert await _unrelated_fields(pool, trip_id) == unrelated_before_detail
            assert await _labels(pool, trip_id) == (
                "Home", "Office", "Errand", "Updated unrelated field"
            )

            dashboard_cleared = await client.post(
                f"/trips/{trip_id}/edit",
                headers={"X-CSRF-Token": csrf},
                data={
                    **omitted, "start_label": "", "end_label": "",
                    "dashboard_week": "2026-07-13",
                },
            )
            assert dashboard_cleared.status_code == 200
            assert dashboard_cleared.headers["hx-refresh"] == "true"
            assert await _unrelated_fields(pool, trip_id) == unrelated_before_detail
            assert await _labels(pool, trip_id) == (
                None, None, "Errand", "Updated unrelated field"
            )

            detail_restore = await client.post(
                f"/trips/{trip_id}/labels",
                headers={"X-CSRF-Token": csrf},
                data={"start_label": "A" * 100, "end_label": "Office"},
            )
            assert detail_restore.status_code == 204
            assert await _unrelated_fields(pool, trip_id) == unrelated_before_detail
            assert await _labels(pool, trip_id) == (
                "A" * 100, "Office", "Errand", "Updated unrelated field"
            )
            refreshed = await client.get(f"/trips/{trip_id}")
            assert refreshed.status_code == 200
            assert "A" * 100 in refreshed.text
            assert "Office" in refreshed.text

            invalid = await client.post(
                f"/trips/{trip_id}/labels",
                headers={"X-CSRF-Token": csrf},
                data={"start_label": "B" * 101, "end_label": "Office"},
            )
            assert invalid.status_code == 200
            assert 'role="alert"' in invalid.text
            assert 'value="' + ("B" * 101) + '"' in invalid.text
            assert await _labels(pool, trip_id) == (
                "A" * 100, "Office", "Errand", "Updated unrelated field"
            )

            detail_clear = await client.post(
                f"/trips/{trip_id}/labels",
                headers={"X-CSRF-Token": csrf},
                data={"start_label": "", "end_label": ""},
            )
            assert detail_clear.status_code == 204
            assert await _unrelated_fields(pool, trip_id) == unrelated_before_detail
            assert await _labels(pool, trip_id) == (
                None, None, "Errand", "Updated unrelated field"
            )

            ineligible = await client.post(
                f"/trips/{detected_id}/labels",
                headers={"X-CSRF-Token": csrf},
                data={"start_label": "Detected endpoint"},
            )
            assert ineligible.status_code == 400
            assert await _labels(pool, detected_id) == (None, None, None, None)

            routed_ineligible = await client.post(
                f"/trips/{routed_id}/labels",
                headers={"X-CSRF-Token": csrf},
                data={"end_label": "Routed endpoint"},
            )
            assert routed_ineligible.status_code == 400
            assert await _labels(pool, routed_id) == (None, None, None, None)

        unauthenticated_app = _bare_app(pool, dev_no_auth=False)
        unauthenticated_transport = httpx.ASGITransport(app=unauthenticated_app)
        async with httpx.AsyncClient(
            transport=unauthenticated_transport, base_url="http://testserver",
            follow_redirects=False,
        ) as unauthenticated_client:
            unauthenticated = await unauthenticated_client.get(f"/trips/{trip_id}")
            assert unauthenticated.status_code == 303
            assert unauthenticated.headers["location"] == "/login"

    _scenario(run)
