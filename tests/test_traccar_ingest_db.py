"""DB-backed tests for POST /ingest/traccar storage and admission."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from app.account_context import account_id
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.ingest import FailedAuthLimiter, make_router
from app.local_auth import hash_password
from app.ownership import _attribute_legacy_raw_messages
from app.tracking import create_device, revoke_credential, rotate_credential
from conftest import finite_test_grant, reset_account_db
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
    ),
]

FORM = {"content-type": "application/x-www-form-urlencoded"}

REFUSE_POINT_WRITES = """
CREATE FUNCTION public.refuse_point_write() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'points write refused' USING ERRCODE = '42501';
END $$;
CREATE TRIGGER refuse_point_write BEFORE INSERT ON points
FOR EACH ROW EXECUTE FUNCTION public.refuse_point_write();
"""
DROP_REFUSE_POINT_WRITES = """
DROP TRIGGER IF EXISTS refuse_point_write ON points;
DROP FUNCTION IF EXISTS public.refuse_point_write();
"""


def _bare_app(pool, wakes) -> FastAPI:
    app = FastAPI()
    app.state.control_pool = pool.control_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.config = SimpleNamespace(
        ingest_username="owntracks",
        ingest_password="testpw",
        ingest_max_body_bytes=1_000_000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(1000, 60.0)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda *key: wakes.append(key))
    app.include_router(make_router())
    return app


async def _scenario(coro) -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO ingest_credentials (public_id,basic_username,secret_hash,account_id,kind) "
                "VALUES ('test-legacy','owntracks',%s,%s,'legacy')",
                (hash_password("testpw"), pool.principal.account_id),
            )
            issued = await create_device(conn, "Traccar phone")
        wakes = []
        app = _bare_app(pool, wakes)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await coro(SimpleNamespace(pool=pool, client=client, app=app, issued=issued, wakes=wakes))
    finally:
        await raw_pool.close()


def _url(username, key) -> str:
    return "/ingest/traccar?" + urlencode({"username": username, "key": key})


def _post(t, fields, credential=None):
    credential = credential or t.issued
    return t.client.post(
        _url(credential.username, credential.secret), headers=FORM, content=urlencode(fields),
    )


def _fix(**changes):
    fields = {
        "id": "12345678", "lat": "47.6062", "lon": "-122.3321",
        "timestamp": str(int(time.time()) - 60), "accuracy": "4.5", "altitude": "56.0",
        "speed": "10.0", "bearing": "90.0", "batt": "85", "charge": "false",
    }
    fields.update(changes)
    return {key: value for key, value in fields.items() if value is not None}


async def _stored(pool) -> tuple[int, int]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (SELECT count(*) FROM raw_messages), (SELECT count(*) FROM points)"
        )
        return tuple(await cur.fetchone())


async def _receipts(pool) -> int:
    async with pool.admin_pool.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM raw_replay_receipts")).fetchone())[0]


async def _devices(pool) -> int:
    async with pool.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM tracking_devices")).fetchone())[0]


def _failures(t) -> int:
    return sum(len(entries) for entries in t.app.state.ingest_limiter._failures.values())


def test_valid_position_stores_original_strings_and_a_normalized_point():
    async def run(t):
        fields = _fix()
        response = await _post(t, fields)
        assert response.status_code == 200 and response.content == b""
        async with t.pool.connection() as conn:
            raw = await (await conn.execute(
                "SELECT payload, tracking_device_id FROM raw_messages"
            )).fetchall()
            point = await (await conn.execute(
                "SELECT tracking_device_id, device, recorded_at, ST_Y(geom::geometry), "
                "ST_X(geom::geometry), accuracy_m, velocity_kmh, altitude_m, battery_pct, trigger "
                "FROM points"
            )).fetchall()
        assert len(raw) == 1
        payload, raw_device = raw[0]
        assert payload == {**fields, "_source": "traccar"}
        assert "_type" not in payload
        assert t.issued.secret not in json.dumps(payload)
        assert t.issued.username not in json.dumps(payload)
        assert raw_device == t.issued.tracking_device_id
        assert len(point) == 1
        device_id, device, recorded_at, lat, lon, acc, vel, alt, batt, trigger = point[0]
        assert device_id == t.issued.tracking_device_id
        assert device == "12345678"
        assert recorded_at == datetime.fromtimestamp(int(fields["timestamp"]), timezone.utc)
        assert (lat, lon) == pytest.approx((47.6062, -122.3321))
        assert acc == pytest.approx(4.5)
        assert vel == pytest.approx(18.52)
        assert alt == pytest.approx(56.0)
        assert batt == 85
        assert trigger is None
        assert len(t.wakes) == 1

    asyncio.run(_scenario(run))


def test_missing_id_stores_default_device_text():
    async def run(t):
        assert (await _post(t, _fix(id=None))).status_code == 200
        async with t.pool.connection() as conn:
            assert (await (await conn.execute("SELECT device FROM points")).fetchone())[0] == "default"

    asyncio.run(_scenario(run))


def test_exact_replay_is_deduplicated_and_still_wakes_the_detector():
    async def run(t):
        fields = _fix()
        assert (await _post(t, fields)).status_code == 200
        assert (await _post(t, fields)).status_code == 200
        assert await _stored(t.pool) == (1, 1)
        assert await _receipts(t.pool) == 1
        assert len(t.wakes) == 2

    asyncio.run(_scenario(run))


def test_invalid_fix_is_stored_raw_without_a_point():
    async def run(t):
        for fields in (_fix(lon=None), _fix(lat="NaN"), _fix(lat="0", lon="0")):
            response = await _post(t, fields)
            assert response.status_code == 200 and response.content == b""
        assert await _stored(t.pool) == (3, 0)
        assert t.wakes == []

    asyncio.run(_scenario(run))


def test_heartbeat_without_coordinates_stores_nothing():
    async def run(t):
        response = await _post(t, _fix(lat=None, lon=None))
        assert response.status_code == 200 and response.content == b""
        assert await _stored(t.pool) == (0, 0)
        assert await _receipts(t.pool) == 0

    asyncio.run(_scenario(run))


def test_wrong_rotated_and_revoked_credentials_answer_401_and_count():
    async def run(t):
        wrong = SimpleNamespace(username=t.issued.username, secret="not-the-secret")
        assert (await _post(t, _fix(), wrong)).status_code == 401
        assert _failures(t) == 1
        async with t.pool.connection() as conn:
            rotated = await rotate_credential(conn, t.issued.public_id)
        assert (await _post(t, _fix(), t.issued)).status_code == 401
        assert _failures(t) == 2
        assert (await _post(t, _fix(), rotated)).status_code == 200
        async with t.pool.connection() as conn:
            await revoke_credential(conn, rotated.public_id)
        assert (await _post(t, _fix(batt="50"), rotated)).status_code == 401
        assert _failures(t) == 3
        assert await _stored(t.pool) == (1, 1)

    asyncio.run(_scenario(run))


def test_valid_legacy_credential_answers_401_and_provisions_nothing():
    async def run(t):
        before = await _devices(t.pool)
        legacy = SimpleNamespace(username="owntracks", secret="testpw")
        response = await _post(t, _fix(id="aa"), legacy)
        assert response.status_code == 401
        assert _failures(t) == 0
        assert await _devices(t.pool) == before
        async with t.pool.connection() as conn:
            aliases = (await (await conn.execute(
                "SELECT count(*) FROM tracking_device_aliases"
            )).fetchone())[0]
        assert aliases == 0
        assert await _stored(t.pool) == (0, 0)

    asyncio.run(_scenario(run))


def test_storage_capacity_answers_503_and_stores_nothing():
    async def run(t):
        async with t.pool.admin_pool.connection() as conn:
            await finite_test_grant(conn, t.pool.principal.account_id)
            await conn.execute(
                "UPDATE storage_grants SET raw_limit_bytes = 1 WHERE account_id = %s",
                (t.pool.principal.account_id,),
            )
        response = await _post(t, _fix())
        assert response.status_code == 503
        assert int(response.headers["Retry-After"]) > 0
        assert await _stored(t.pool) == (0, 0)
        assert await _receipts(t.pool) == 0

    asyncio.run(_scenario(run))


def test_privilege_error_while_storing_answers_5xx():
    async def run(t):
        async with t.pool.admin_pool.connection() as conn:
            await conn.execute(REFUSE_POINT_WRITES)
        try:
            response = await _post(t, _fix())
        finally:
            async with t.pool.admin_pool.connection() as conn:
                await conn.execute(DROP_REFUSE_POINT_WRITES)
        assert response.status_code >= 500
        assert await _stored(t.pool) == (0, 0)

    asyncio.run(_scenario(run))


def test_cached_fix_repeating_a_stored_timestamp_adds_no_point():
    async def run(t):
        stamp = str(int(time.time()) - 600)
        assert (await _post(t, _fix(timestamp=stamp, batt="80"))).status_code == 200
        # A heartbeat resends the cached fix with fresher battery and bearing.
        assert (await _post(t, _fix(timestamp=stamp, batt="79", bearing="12.0"))).status_code == 200
        assert await _stored(t.pool) == (2, 1)
        async with t.pool.connection() as conn:
            assert (await (await conn.execute("SELECT battery_pct FROM points")).fetchone())[0] == 80

    asyncio.run(_scenario(run))


async def _insert_track(conn, device_id, points) -> None:
    for p in points:
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, "
            "geom, accuracy_m, velocity_kmh) VALUES (%s, %s, 'track', %s, %s, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (account_id(conn), device_id, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
        )


def test_cached_fix_older_than_the_latest_point_is_kept_and_rewinds_the_runner():
    async def run(t):
        device_id = t.issued.tracking_device_id
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with t.pool.connection() as conn:
            await _insert_track(conn, device_id, track)
        runner = DetectorRunner(t.pool, Params())
        assert await runner.run_once() is True

        latest = track[-1]
        older_at = latest.t - timedelta(seconds=293)
        assert older_at < latest.t
        async with t.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT started_at FROM stays WHERE tracking_device_id = %s AND ended_at < %s "
                "ORDER BY ended_at DESC LIMIT 1", (device_id, older_at),
            )).fetchone()
        assert row is not None
        settled_start = row[0]

        fields = _fix(
            lat=repr(latest.lat), lon=repr(latest.lon),
            timestamp=str(int(older_at.timestamp())), speed="0.0",
        )
        assert (await _post(t, fields)).status_code == 200
        async with t.pool.connection() as conn:
            kept = await (await conn.execute(
                "SELECT count(*) FROM points WHERE tracking_device_id = %s AND recorded_at = %s",
                (device_id, older_at),
            )).fetchone()
        assert kept[0] == 1

        seen = {}
        process, rewind = runner._process_device, runner._rewind_start

        async def capture_process(conn, device, dirty_from, full):
            seen["dirty_from"], seen["full"] = dirty_from, full
            return await process(conn, device, dirty_from, full)

        async def capture_rewind(conn, device, dirty_from, full):
            seen["t0"] = await rewind(conn, device, dirty_from, full)
            return seen["t0"]

        runner._process_device, runner._rewind_start = capture_process, capture_rewind
        assert await runner.run_once() is True
        assert seen == {"dirty_from": older_at, "full": False, "t0": settled_start}

    asyncio.run(_scenario(run))


def test_legacy_ownership_attribution_skips_traccar_rows():
    async def run(t):
        assert (await _post(t, _fix(id="default"))).status_code == 200
        owner = t.pool.principal.account_id
        async with t.pool.connection() as conn:
            alias_target = (await (await conn.execute(
                "INSERT INTO tracking_devices (account_id, label) VALUES (%s, 'alias') RETURNING id",
                (owner,),
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO tracking_device_aliases (account_id, original_label, tracking_device_id) "
                "VALUES (%s, 'default', %s)", (owner, alias_target),
            )
        owntracks = {
            "_type": "location", "lat": 47.6, "lon": -122.3, "tst": int(time.time()) - 60,
        }
        async with t.pool.admin_pool.connection() as conn:
            await conn.execute("UPDATE raw_messages SET tracking_device_id = NULL")
            await conn.execute(
                "INSERT INTO raw_messages (account_id, payload) VALUES (%s, %s)",
                (owner, json.dumps(owntracks)),
            )
            await _attribute_legacy_raw_messages(conn, owner)
            rows = await (await conn.execute(
                "SELECT payload ? '_source', tracking_device_id FROM raw_messages ORDER BY id"
            )).fetchall()
        # The OwnTracks contrast row proves attribution ran at all.
        assert [tuple(row) for row in rows] == [(True, None), (False, alias_target)]

    asyncio.run(_scenario(run))
