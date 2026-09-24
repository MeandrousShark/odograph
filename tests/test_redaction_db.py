"""DB-backed half of the redaction-claim tests (tests/test_redaction.py
holds the DB-free ones). Needs a real Postgres because the "dropping
location payload" ingest branch runs after the raw payload is already
INSERTed into raw_messages, and the snap-worker case exercises a real
`trips` row end to end.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.db import make_pool
from app.ingest import FailedAuthLimiter, make_router as make_ingest_router
from app.snap import SnapWorker
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id
from app.local_auth import hash_password

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"owntracks:testpw").decode()
}

# A distinctive string that would never legitimately appear in these logs --
# stands in for anything an ingest payload might carry (a device id, a
# coordinate-bearing tag, free text), so its absence from caplog proves the
# body/payload itself never reached a log line.
MARKER = "OWNTRACKS-PAYLOAD-MARKER-98765"


def _ingest_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.control_pool = pool.control_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.config = SimpleNamespace(
        ingest_username="owntracks", ingest_password="testpw", ingest_max_body_bytes=65536,
    )
    app.state.ingest_limiter = FailedAuthLimiter(1000, 60.0)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda *key: None)
    app.include_router(make_ingest_router())
    return app


async def _post_ingest(pool, content) -> httpx.Response:
    transport = httpx.ASGITransport(app=_ingest_app(pool))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post("/ingest", headers=AUTH_HEADER, content=content)


def _with_pool(coro):
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,account_id,kind) "
                    "VALUES ('redaction-legacy','owntracks',%s,%s,'legacy')",
                    (hash_password("testpw"), pool.principal.account_id),
                )
            await coro(pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


def test_unparseable_body_drop_never_logs_the_body(caplog):
    async def run(pool):
        with caplog.at_level(logging.WARNING, logger="app.ingest"):
            response = await _post_ingest(pool, f"not json {{{MARKER}".encode())
        assert response.status_code == 200
        assert MARKER not in caplog.text
        assert "dropping unparseable body" in caplog.text

    _with_pool(run)


def test_non_object_payload_drop_never_logs_the_body(caplog):
    async def run(pool):
        with caplog.at_level(logging.WARNING, logger="app.ingest"):
            response = await _post_ingest(pool, f'["{MARKER}"]'.encode())
        assert response.status_code == 200
        assert MARKER not in caplog.text
        assert "dropping non-object payload" in caplog.text

    _with_pool(run)


def test_invalid_location_payload_drop_never_logs_the_body(caplog):
    async def run(pool):
        # A structurally valid object, but a rejected location fix -- past
        # the raw_messages INSERT, so this is the one branch that could
        # plausibly still have the payload in scope when it logs.
        payload = {
            "_type": "location", "tid": MARKER,
            "lat": 999.0, "lon": -122.0, "tst": int(time.time()),
        }
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post_ingest(pool, json.dumps(payload).encode())
        assert response.status_code == 200
        assert MARKER not in caplog.text
        assert "999.0" not in caplog.text
        assert "dropping location payload" in caplog.text

    _with_pool(run)


def test_oversized_body_drop_never_logs_the_body(caplog):
    async def run(pool):
        with caplog.at_level(logging.WARNING, logger="app.ingest"):
            response = await _post_ingest(pool, MARKER.encode() * 10000)
        assert response.status_code == 200
        assert MARKER not in caplog.text
        assert "oversized body" in caplog.text

    _with_pool(run)


# --- snap worker: an unsnappable trip's log carries the trip id, not a coordinate ---


DEVICE = "REDACT-TEST"
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)


async def _insert_unsnappable_trip(conn) -> int:
    device = await seed_tracking_device(conn, DEVICE)
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, snap_status) "
        "VALUES (%s, %s, %s, 'detected', %s, %s, 1000, 1, 2, 'pending') RETURNING id",
        (account_id(conn), device, DEVICE, T0, T0 + timedelta(seconds=60)),
    )
    trip_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, accuracy_m, trip_id) "
        "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
        (account_id(conn), device, DEVICE, T0, T0, -122.335678, 47.601234, 10.0, trip_id),
    )
    return trip_id


def test_snap_worker_unsnappable_trip_logs_trip_id_not_coordinates(caplog):
    async def run(pool):
        async with pool.connection() as conn:
            trip_id = await _insert_unsnappable_trip(conn)

        worker = SnapWorker(pool, None, "http://osrm", 0.5, 250)
        with caplog.at_level(logging.WARNING, logger="app.snap"):
            await worker._snap_one(trip_id)

        assert str(trip_id) in caplog.text
        assert "47.601234" not in caplog.text
        assert "-122.335678" not in caplog.text

    _with_pool(run)
