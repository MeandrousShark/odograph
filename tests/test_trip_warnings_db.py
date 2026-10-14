"""DB-backed coverage for trip list warning acknowledgements through the
real routes, the restricted runtime role and row-level security.

The detected fixture has three trips on one device: a long first trip, a
shorter second trip with a recording gap, and a third trip that starts 5 km
from where the second ended, so it shows a possible missing trip whose
predecessor is the second. tests/test_trip_warnings.py covers the pure
matching and templates.

The target database is destroyed and recreated. Set TEST_DATABASE_URL only to
a throwaway Postgres/PostGIS instance.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from psycopg import errors
from psycopg.rows import dict_row
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.account_context import account_id
from app.auth import AuthRedirect
from app.config import Config
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner, load_trip_points
from app.main import make_templates
from app.missing_trip import missing_trip_badge
from app.trip_warnings import trip_warnings
from app.ui import make_router
from app.ui._common import TRIP_COLUMNS
from conftest import add_test_account, reset_account_db, seed_tracking_device
from tests.synth import Drive, Gap, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

CSRF_RE = re.compile(r'X-CSRF-Token": "([^"]+)"')
B_ID = 84
WEEK = "2026-07-01"
TRACK = [
    Stationary(900), Drive(km=20), Stationary(1200),
    Drive(km=4), Gap(700, move_km=3), Drive(km=4), Stationary(1200),
    Gap(7200, move_km=5, bearing_deg=0), Stationary(900), Drive(km=3), Stationary(900),
]


def _bare_app(pool, runner) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.control_pool = pool.control_pool
    app.state.dev_principal = pool.principal
    app.state.make_detector_runner = lambda bound: DetectorRunner(bound, Params())
    with patch.dict(os.environ, {
        "DATABASE_URL": TEST_DB, "SESSION_SECRET": "test-secret", "DEV_NO_AUTH": "1",
    }, clear=True):
        app.state.config = Config.from_env()
    app.state.templates = make_templates(app.state.config)
    app.state.detector_runner = runner
    app.add_middleware(SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False)

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request, exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(make_router())
    return app


async def _seed(pool, label: str) -> int:
    async with pool.connection() as conn:
        stream = await seed_tracking_device(conn, label)
        for p in build_track(TRACK):
            await conn.execute(
                "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, "
                "accuracy_m, velocity_kmh) VALUES (%s, %s, %s, %s, %s, "
                "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
                (account_id(conn), stream, label, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
            )
    assert await DetectorRunner(pool, Params()).run_once() is True
    return stream


async def _trips(pool) -> list[dict]:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE account_id = %s ORDER BY started_at",
                          (account_id(conn),))
        return await cur.fetchall()


async def _warnings(pool) -> dict[int, object]:
    return {trip["id"]: trip_warnings(trip, missing_trip_badge(trip, 1000.0, trip["started_at"].tzinfo))
            for trip in await _trips(pool)}


async def _trip_rows(admin, owner: int) -> list[str]:
    async with admin.connection() as conn:
        cur = await conn.execute(
            "SELECT row_to_json(t)::text FROM trips t WHERE account_id = %s ORDER BY id", (owner,))
        return [row[0] for row in await cur.fetchall()]


async def _acks(admin, owner: int) -> list[tuple]:
    async with admin.connection() as conn:
        cur = await conn.execute(
            "SELECT trip_id, kind::text, trip_started_at, trip_ended_at, prev_trip_id, prev_ended_at "
            "FROM trip_warning_acks WHERE account_id = %s ORDER BY trip_id, kind", (owner,))
        return await cur.fetchall()


async def _storage_consistent(admin) -> bool:
    async with admin.connection() as conn:
        return (await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone())[0]


class _Client:
    def __init__(self, client: httpx.AsyncClient, csrf: str):
        self.client, self.csrf = client, csrf

    async def post(self, path: str, data: dict | None = None, csrf: bool = True) -> httpx.Response:
        headers = {"X-CSRF-Token": self.csrf} if csrf else {}
        return await self.client.post(path, data=data or {}, headers=headers)

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path)


async def _scenario(check, *, second_account: bool = False, seed_second: bool = True) -> None:
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        pool = await reset_account_db(raw)
        stream = await _seed(pool, "PHONE")
        runner = DetectorRunner(pool, Params())
        other = None
        if second_account:
            other = await add_test_account(raw, B_ID)
            if seed_second:
                await _seed(other, "OTHER")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_bare_app(pool, runner)),
                                     base_url="http://testserver") as http:
            csrf = CSRF_RE.search((await http.get("/settings")).text).group(1)
            client = _Client(http, csrf)
            if other is None:
                await check(SimpleNamespace(raw=raw, pool=pool, runner=runner, stream=stream, client=client))
                return
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_bare_app(other, DetectorRunner(other, Params()))),
                base_url="http://testserver",
            ) as other_http:
                other_csrf = CSRF_RE.search((await other_http.get("/settings")).text).group(1)
                await check(SimpleNamespace(
                    raw=raw, pool=pool, runner=runner, stream=stream, client=client,
                    other=other, other_client=_Client(other_http, other_csrf)))
    finally:
        await raw.close()


async def _fixture_ids(pool) -> tuple[int, int, int]:
    trips = await _trips(pool)
    assert len(trips) == 3
    first, gap, missing = trips
    assert not first["has_gap"] and gap["has_gap"] and not missing["has_gap"]
    assert missing["prev_trip_id"] == gap["id"] and missing["prev_end_gap_m"] > 1000
    return first["id"], gap["id"], missing["id"]


def test_acknowledge_quiets_lists_keeps_detail_and_undo_restores():
    async def check(s):
        owner = s.pool.principal.account_id
        _, gap_id, missing_id = await _fixture_ids(s.pool)
        # Classified, so the warnings are the only statuses on the rows.
        async with s.raw.connection() as conn:
            await conn.execute("UPDATE trips SET category = 'business' WHERE account_id = %s", (owner,))
        rows_before = await _trip_rows(s.raw, owner)

        dashboard = (await s.client.get(f"/?week={WEEK}")).text
        assert "1 possible missing trip" in dashboard
        assert dashboard.count("Recording gap") == 1

        # Archive row: the response row is quiet and carries the write marker.
        response = await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")
        assert response.status_code == 200
        assert response.headers["X-Archive-Write"] == "success"
        assert f'id="trip-{gap_id}"' in response.text
        assert "Recording gap" not in response.text
        assert "trip-archive-row-statuses" not in response.text
        assert "Acknowledge warnings" not in response.text
        gap = next(t for t in await _trips(s.pool) if t["id"] == gap_id)
        assert await _acks(s.raw, owner) == [
            (gap_id, "recording_gap", gap["started_at"], gap["ended_at"], None, None)]
        assert await _storage_consistent(s.raw)

        # Dashboard row: the row and the attention strip both update.
        response = await s.client.post(
            f"/trips/{missing_id}/warnings/acknowledge", {"dashboard_week": WEEK})
        assert response.status_code == 200
        assert 'class="dashboard-trip-row' in response.text
        assert "Possible missing trip" not in response.text
        assert "possible missing trip" not in response.text
        assert 'hx-swap-oob' in response.text
        assert 'aria-label="More trip actions" autofocus' in response.text

        dashboard = (await s.client.get(f"/?week={WEEK}")).text
        assert "possible missing trip" not in dashboard.lower()
        assert "Recording gap" not in dashboard
        card = (await s.client.get(f"/trips/{missing_id}/card")).text
        assert "missing-trip-badge" not in card
        # The bridge prefill stays available after acknowledgement.
        assert "Add missing trip" in card

        # Trip Detail still shows both warnings, each with its undo control.
        detail = (await s.client.get(f"/trips/{missing_id}")).text
        assert "Possible missing trip" in detail and "Acknowledged" in detail
        detail = (await s.client.get(f"/trips/{gap_id}")).text
        assert "Recording gap" in detail and "Show in lists again" in detail

        # Reload: the acknowledgement persists.
        assert all(not w.any_listed for w in (await _warnings(s.pool)).values())

        response = await s.client.post(
            f"/trips/{gap_id}/warnings/unacknowledge", {"kind": "recording_gap", "view": "detail"})
        assert response.status_code == 200
        assert f'id="trip-warnings-{gap_id}"' in response.text
        assert "Recording gap" in response.text and "Acknowledged" not in response.text
        assert 'tabindex="-1" autofocus' in response.text
        assert "Recording gap" in (await s.client.get(f"/trips/{gap_id}/card")).text
        assert [ack[:2] for ack in await _acks(s.raw, owner)] == [(missing_id, "missing_trip")]

        response = await s.client.post(
            f"/trips/{missing_id}/warnings/unacknowledge", {"kind": "missing_trip", "view": "detail"})
        assert response.status_code == 200
        assert "1 possible missing trip" in (await s.client.get(f"/?week={WEEK}")).text
        assert await _acks(s.raw, owner) == []
        assert await _storage_consistent(s.raw)

        # Acknowledging never touches the trip rows: has_gap, category,
        # tag_source and every other column are byte-for-byte unchanged.
        assert await _trip_rows(s.raw, owner) == rows_before
    asyncio.run(_scenario(check))


def test_acknowledge_is_idempotent_and_validates_input():
    async def check(s):
        owner = s.pool.principal.account_id
        first_id, gap_id, _ = await _fixture_ids(s.pool)
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        before = await _acks(s.raw, owner)
        async with s.raw.connection() as conn:
            stamp = (await (await conn.execute(
                "SELECT acknowledged_at FROM trip_warning_acks WHERE trip_id = %s", (gap_id,))).fetchone())[0]
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        assert await _acks(s.raw, owner) == before
        async with s.raw.connection() as conn:
            assert (await (await conn.execute(
                "SELECT acknowledged_at FROM trip_warning_acks WHERE trip_id = %s", (gap_id,))).fetchone())[0] == stamp

        # A trip without warnings has nothing to acknowledge.
        assert (await s.client.post(f"/trips/{first_id}/warnings/acknowledge")).status_code == 200
        assert [ack[0] for ack in await _acks(s.raw, owner)] == [gap_id]

        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge", csrf=False)).status_code == 403
        assert (await s.client.post(
            f"/trips/{gap_id}/warnings/unacknowledge", {"kind": "recording_gap"}, csrf=False)).status_code == 403
        assert (await s.client.post(
            f"/trips/{gap_id}/warnings/unacknowledge", {"kind": "unclassified"})).status_code == 400
        assert (await s.client.post(f"/trips/{gap_id}/warnings/unacknowledge")).status_code == 422
        assert (await s.client.post(
            f"/trips/{gap_id}/warnings/acknowledge", {"view": "review"})).status_code == 400
        assert (await s.client.post("/trips/999999/warnings/acknowledge")).status_code == 404
        assert (await s.client.post(
            "/trips/999999/warnings/unacknowledge", {"kind": "recording_gap"})).status_code == 404
        assert [ack[0] for ack in await _acks(s.raw, owner)] == [gap_id]
    asyncio.run(_scenario(check))


def test_two_accounts_cannot_reach_each_others_acknowledgements():
    async def check(s):
        owner = s.pool.principal.account_id
        _, gap_id, missing_id = await _fixture_ids(s.pool)
        _, other_gap_id, other_missing_id = await _fixture_ids(s.other)
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200
        mine = await _acks(s.raw, owner)
        assert len(mine) == 2

        # B guesses A's trip IDs: every route answers 404 and changes nothing.
        for trip_id in (gap_id, missing_id):
            assert (await s.other_client.post(f"/trips/{trip_id}/warnings/acknowledge")).status_code == 404
            assert (await s.other_client.post(
                f"/trips/{trip_id}/warnings/unacknowledge", {"kind": "recording_gap"})).status_code == 404
            assert (await s.other_client.post(
                f"/trips/{trip_id}/warnings/unacknowledge",
                {"kind": "missing_trip", "view": "detail"})).status_code == 404
            assert (await s.other_client.get(f"/trips/{trip_id}")).status_code == 404
        assert await _acks(s.raw, owner) == mine
        assert await _acks(s.raw, B_ID) == []

        # A's acknowledgements never quiet B's lists.
        other_dashboard = (await s.other_client.get(f"/?week={WEEK}")).text
        assert "1 possible missing trip" in other_dashboard
        assert "Recording gap" in other_dashboard
        assert all(w.any_listed for trip_id, w in (await _warnings(s.other)).items()
                   if trip_id in (other_gap_id, other_missing_id))

        # Under RLS on the restricted runtime role, B sees no ack rows and
        # cannot write one against A's trip under either account ID.
        async with s.other.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM trip_warning_acks")).fetchone())[0] == 0
            assert (await conn.execute(
                "DELETE FROM trip_warning_acks WHERE trip_id = %s", (gap_id,))).rowcount == 0
            with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO trip_warning_acks(account_id,trip_id,kind,trip_started_at,trip_ended_at) "
                        "VALUES(%s,%s,'recording_gap',now(),now())", (owner, gap_id))
            with pytest.raises(errors.ForeignKeyViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO trip_warning_acks(account_id,trip_id,kind,trip_started_at,trip_ended_at) "
                        "VALUES(%s,%s,'recording_gap',now(),now())", (B_ID, gap_id))
        assert await _acks(s.raw, owner) == mine
        assert await _storage_consistent(s.raw)
    asyncio.run(_scenario(check, second_account=True))


async def _split(s, trip_id: int, fraction: float) -> None:
    async with s.pool.connection() as conn:
        points = await load_trip_points(conn, trip_id)
    response = await s.client.post(
        f"/trips/{trip_id}/split", {"point_id": points[int(len(points) * fraction)][0]})
    assert response.status_code == 204


def test_redetection_keeps_and_split_or_merge_reshows_a_gap_acknowledgement():
    async def check(s):
        _, gap_id, _ = await _fixture_ids(s.pool)
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        original = {t["id"]: (t["started_at"], t["ended_at"]) for t in await _trips(s.pool)}

        # In-place re-detection keeps the ID and span, so the ack holds.
        await s.runner.reprocess_device_now(s.stream)
        assert {t["id"]: (t["started_at"], t["ended_at"]) for t in await _trips(s.pool)} == original
        assert (await _warnings(s.pool))[gap_id].gap_acknowledged

        # Split early: the larger later half keeps the ID with a new start.
        await _split(s, gap_id, 0.3)
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert gap_id in trips and trips[gap_id]["started_at"] != original[gap_id][0]
        assert trips[gap_id]["ended_at"] == original[gap_id][1] and trips[gap_id]["has_gap"]
        assert (await _warnings(s.pool))[gap_id].listed_gap

        # Merging the halves back restores the acknowledged span exactly,
        # so the stale row matches again.
        earlier_half = trips[gap_id]["prev_trip_id"]
        assert earlier_half not in original
        response = await s.client.post(
            f"/trips/{gap_id}/merge_prev", {"category": "keep", "vehicle_id": "keep"})
        assert response.status_code == 204
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert earlier_half not in trips and (trips[gap_id]["started_at"], trips[gap_id]["ended_at"]) == original[gap_id]
        assert (await _warnings(s.pool))[gap_id].gap_acknowledged

        # Split late: the larger earlier half keeps the ID with a new end.
        await _split(s, gap_id, 0.7)
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert trips[gap_id]["started_at"] == original[gap_id][0]
        assert trips[gap_id]["ended_at"] != original[gap_id][1] and trips[gap_id]["has_gap"]
        assert (await _warnings(s.pool))[gap_id].listed_gap

        # Re-acknowledging replaces the stale row with the current span.
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        assert (await _warnings(s.pool))[gap_id].gap_acknowledged
        acks = await _acks(s.raw, s.pool.principal.account_id)
        assert [(a[0], a[2], a[3]) for a in acks] == [(gap_id, *(trips[gap_id][k] for k in ("started_at", "ended_at")))]

        # Merging with the following trip changes the kept row's span.
        first_id = min(original)
        response = await s.client.post(
            f"/trips/{first_id}/merge_next", {"category": "keep", "vehicle_id": "keep"})
        assert response.status_code == 204
        warnings = await _warnings(s.pool)
        assert all(not w.gap_acknowledged for w in warnings.values())
        assert any(w.listed_gap for w in warnings.values())
        assert await _storage_consistent(s.raw)
    asyncio.run(_scenario(check))


def test_predecessor_changes_reshow_a_missing_trip_acknowledgement():
    async def check(s):
        first_id, gap_id, missing_id = await _fixture_ids(s.pool)
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200
        assert (await _warnings(s.pool))[missing_id].missing_acknowledged

        await s.runner.reprocess_device_now(s.stream)
        assert (await _warnings(s.pool))[missing_id].missing_acknowledged

        # Splitting the predecessor late hands the end to a new trip ID.
        await _split(s, gap_id, 0.7)
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert trips[missing_id]["prev_trip_id"] not in (gap_id, None)
        assert (await _warnings(s.pool))[missing_id].listed_missing is not None

        # Deleting the new predecessor makes the older trip the predecessor.
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200
        new_prev = trips[missing_id]["prev_trip_id"]
        assert (await s.client.post(f"/trips/{new_prev}/delete", {"fragment": "true"})).status_code == 200
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert new_prev not in trips and trips[missing_id]["prev_trip_id"] == gap_id
        assert (await _warnings(s.pool))[missing_id].listed_missing is not None

        # Merging the predecessor into the longer trip before it keeps that
        # trip's ID, so the predecessor changes.
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200
        response = await s.client.post(
            f"/trips/{gap_id}/merge_prev", {"category": "keep", "vehicle_id": "keep"})
        assert response.status_code == 204
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert gap_id not in trips and trips[missing_id]["prev_trip_id"] == first_id
        assert (await _warnings(s.pool))[missing_id].listed_missing is not None
        assert await _storage_consistent(s.raw)
    asyncio.run(_scenario(check))


def test_predecessor_keeping_its_id_and_end_keeps_a_missing_trip_acknowledgement():
    """Observed behavior of the recorded fingerprint, pinned so a change is
    deliberate: an early split, or merging the predecessor with a shorter trip
    before it, leaves the predecessor's ID and end unchanged, so the gap this warning describes is
    the same one that was acknowledged."""
    async def check(s):
        first_id, gap_id, missing_id = await _fixture_ids(s.pool)
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200

        await _split(s, gap_id, 0.3)
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert trips[missing_id]["prev_trip_id"] == gap_id
        assert (await _warnings(s.pool))[missing_id].missing_acknowledged

        earlier_half = [t["id"] for t in trips.values() if t["id"] not in (first_id, gap_id, missing_id)]
        assert len(earlier_half) == 1
        # Merging the predecessor with that shorter earlier half keeps the
        # predecessor's ID and end.
        response = await s.client.post(
            f"/trips/{gap_id}/merge_prev", {"category": "keep", "vehicle_id": "keep"})
        assert response.status_code == 204
        trips = {t["id"]: t for t in await _trips(s.pool)}
        assert earlier_half[0] not in trips and trips[missing_id]["prev_trip_id"] == gap_id
        assert (await _warnings(s.pool))[missing_id].missing_acknowledged
    asyncio.run(_scenario(check))


def test_deleting_the_trip_cascades_its_acknowledgements_and_storage():
    async def check(s):
        owner = s.pool.principal.account_id
        _, gap_id, missing_id = await _fixture_ids(s.pool)
        async with s.raw.connection() as conn:
            usage = (await (await conn.execute(
                "SELECT actual_bytes FROM account_usage WHERE account_id = %s", (owner,))).fetchone())[0]
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200
        async with s.raw.connection() as conn:
            charged = (await (await conn.execute(
                "SELECT actual_bytes FROM account_usage WHERE account_id = %s", (owner,))).fetchone())[0]
        assert charged == usage + 2 * 128 + len("recording_gap") + len("missing_trip")
        assert await _storage_consistent(s.raw)

        # A re-acknowledgement that replaces a stale row is an update.
        async with s.raw.connection() as conn:
            await conn.execute(
                "UPDATE trip_warning_acks SET trip_started_at = trip_started_at - interval '1 minute' "
                "WHERE trip_id = %s", (gap_id,))
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        assert await _storage_consistent(s.raw)

        assert (await s.client.post(f"/trips/{gap_id}/delete", {"fragment": "true"})).status_code == 200
        assert [ack[0] for ack in await _acks(s.raw, owner)] == [missing_id]
        assert await _storage_consistent(s.raw)
    asyncio.run(_scenario(check))


def test_portable_export_omits_and_import_leaves_acknowledgements():
    from test_portable_import_db import _bare_app as portable_app, _export, _import

    async def check(s):
        owner = s.pool.principal.account_id
        _, gap_id, missing_id = await _fixture_ids(s.pool)
        assert (await s.client.post(f"/trips/{gap_id}/warnings/acknowledge")).status_code == 200
        assert (await s.client.post(f"/trips/{missing_id}/warnings/acknowledge")).status_code == 200
        mine = await _acks(s.raw, owner)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=portable_app(s.pool)),
                                     base_url="http://testserver") as http:
            bundle = await _export(http)
            text = json.dumps(bundle)
            assert "trip_warning_acks" not in text and "acknowledg" not in text.lower()
            assert "has_gap" in bundle["trips"][0]
            assert not any(key.startswith(("gap_ack", "missing_ack", "prev_")) for key in bundle["trips"][0])
            # A target holding acknowledged trips is not clean, so import
            # refuses before any write.
            csrf = CSRF_RE.search((await http.get("/settings")).text).group(1)
            assert (await _import(http, csrf, bundle)).status_code == 409
        assert await _acks(s.raw, owner) == mine

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=portable_app(s.other)),
                                     base_url="http://testserver") as http:
            csrf = CSRF_RE.search((await http.get("/settings")).text).group(1)
            response = await _import(http, csrf, bundle)
            assert response.status_code == 200, response.text
        async with s.raw.connection() as conn:
            assert (await (await conn.execute(
                "SELECT count(*) FROM trips WHERE account_id = %s", (B_ID,))).fetchone())[0] == 3
        assert await _acks(s.raw, B_ID) == []
        assert await _acks(s.raw, owner) == mine
        assert await _storage_consistent(s.raw)
    asyncio.run(_scenario(check, second_account=True, seed_second=False))
