"""DB-backed tests for /ingest location payload validation.

`_validate_location` must drop any malformed payload with 200-and-log rather
than let it reach `datetime.fromtimestamp` or the points INSERT and raise: a
500 there reads as retryable to OwnTracks, which redelivers the poison
payload forever and wedges that device's upload queue.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.db import make_pool
from app.ingest import FailedAuthLimiter, _validate_location, make_router
from conftest import reset_account_db
from app.local_auth import hash_password

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"owntracks:testpw").decode()
}


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.control_pool = pool.runtime_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.config = SimpleNamespace(
        ingest_username="owntracks",
        ingest_password="testpw",
        ingest_max_body_bytes=1_000_000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(1000, 60.0)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda *key: None)
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
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await coro(pool, client)
    finally:
        await raw_pool.close()


async def _points_count(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM points")
        return (await cur.fetchone())[0]


async def _raw_message_count(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM raw_messages")
        return (await cur.fetchone())[0]


def _base_payload(**overrides) -> dict:
    payload = {
        "_type": "location",
        "tid": "aa",
        "lat": 47.6,
        "lon": -122.3,
        "tst": int(time.time()) - 60,
    }
    payload.update(overrides)
    return payload


def _post(client, payload: dict):
    # json.dumps emits the non-standard NaN/Infinity tokens Python's json
    # module also accepts on decode, which is how a poison tst value can
    # actually reach the wire in these tests.
    body = json.dumps(payload, allow_nan=True).encode()
    return client.post("/ingest", headers=AUTH_HEADER, content=body)


@pytest.mark.parametrize("tst", [1e18, 1.7e12])
def test_poison_tst_is_dropped_not_stored(tst, caplog):
    async def run(pool, client):
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, _base_payload(tst=tst))
        assert response.status_code == 200
        assert await _points_count(pool) == 0
        assert "dropping location payload" in caplog.text

    asyncio.run(_scenario(run))


@pytest.mark.parametrize("tst", [math.nan, math.inf, -math.inf])
def test_validate_location_rejects_non_finite_tst(tst):
    # Unit-level pin for _validate_location's own contract, independent of
    # the earlier jsonb-storage guard (see the poison-payload tests below)
    # that now stops a non-finite tst from ever reaching this function.
    payload = _base_payload(tst=tst)
    assert _validate_location(payload) is not None


@pytest.mark.parametrize("t", [1, {"x": 1}, [1, 2], True])
def test_non_string_trigger_is_dropped_not_stored(t, caplog):
    async def run(pool, client):
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, _base_payload(t=t))
        assert response.status_code == 200
        assert await _points_count(pool) == 0
        assert "dropping location payload" in caplog.text

    asyncio.run(_scenario(run))


def test_valid_string_trigger_is_stored_unchanged():
    async def run(pool, client):
        response = await _post(client, _base_payload(t="p"))
        assert response.status_code == 200
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT trigger FROM points")
            assert (await cur.fetchone())[0] == "p"

    asyncio.run(_scenario(run))


def test_payload_without_trigger_field_still_ingests():
    async def run(pool, client):
        response = await _post(client, _base_payload())
        assert response.status_code == 200
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT trigger FROM points")
            assert (await cur.fetchone())[0] is None

    asyncio.run(_scenario(run))


def test_plausible_future_tst_still_hits_clock_skew_rejection(caplog):
    async def run(pool, client):
        future_tst = int(time.time()) + 3600
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, _base_payload(tst=future_tst))
        assert response.status_code == 200
        assert await _points_count(pool) == 0
        assert "clock skew" in caplog.text

    asyncio.run(_scenario(run))


# The raw_messages archival INSERT runs unconditionally, before the _type
# check and before _validate_location, so a payload jsonb can't store must
# be dropped before that INSERT ever happens. json.loads accepts shapes
# jsonb's strict RFC 8259 parser rejects: bare NaN/Infinity tokens, a string
# containing an embedded NUL character, and a string containing an unpaired
# UTF-16 surrogate. Any of these reaching the INSERT raises from inside the
# open transaction and turns into a 500, which OwnTracks reads as retryable
# and redelivers forever.
POISON_PAYLOADS = [
    pytest.param(_base_payload(tst=math.nan), "non-finite number", id="nan-tst"),
    pytest.param(
        _base_payload(vel=math.inf), "non-finite number", id="non-finite-non-essential-field"
    ),
    pytest.param(
        {"_type": "transition", "tid": "aa", "wtst": -math.inf},
        "non-finite number",
        id="non-finite-non-location-message-type",
    ),
    pytest.param(_base_payload(tid="a\x00b"), "NUL character", id="nul-in-string-field"),
    pytest.param(_base_payload(tid="\ud800"), "unpaired surrogate", id="lone-high-surrogate"),
    pytest.param(_base_payload(tid="\udc00"), "unpaired surrogate", id="lone-low-surrogate"),
]


@pytest.mark.parametrize("payload, reason", POISON_PAYLOADS)
def test_poison_payload_dropped_before_raw_messages_insert(payload, reason, caplog):
    async def run(pool, client):
        with caplog.at_level(logging.WARNING, logger="app.ingest"):
            response = await _post(client, payload)
        assert response.status_code == 200
        assert await _raw_message_count(pool) == 0
        assert await _points_count(pool) == 0
        assert "dropping payload before storage" in caplog.text
        assert reason in caplog.text

    asyncio.run(_scenario(run))


def test_valid_location_payload_is_stored_in_raw_messages_unchanged():
    async def run(pool, client):
        payload = _base_payload()
        response = await _post(client, payload)
        assert response.status_code == 200
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT payload FROM raw_messages")
            stored = (await cur.fetchone())[0]
        assert stored == payload
        assert await _points_count(pool) == 1

    asyncio.run(_scenario(run))


def test_non_location_message_type_is_still_recorded_in_raw_messages():
    async def run(pool, client):
        payload = {"_type": "transition", "tid": "aa", "wtst": int(time.time()) - 60}
        response = await _post(client, payload)
        assert response.status_code == 200
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT payload FROM raw_messages")
            stored = (await cur.fetchone())[0]
        assert stored == payload
        assert await _points_count(pool) == 0

    asyncio.run(_scenario(run))


def test_connection_stays_usable_after_a_poison_payload():
    # The poison-payload guard runs before pool.connection() is even
    # acquired, so it can't leave a transaction aborted -- but this pins
    # that contract end to end rather than relying on that being true.
    async def run(pool, client):
        poison_response = await _post(client, _base_payload(tst=math.nan))
        assert poison_response.status_code == 200
        assert await _raw_message_count(pool) == 0

        valid_payload = _base_payload()
        valid_response = await _post(client, valid_payload)
        assert valid_response.status_code == 200
        assert await _raw_message_count(pool) == 1
        assert await _points_count(pool) == 1

    asyncio.run(_scenario(run))


def test_connection_stays_usable_after_a_surrogate_poisoned_payload():
    async def run(pool, client):
        poison_response = await _post(client, _base_payload(tid="\ud800"))
        assert poison_response.status_code == 200
        assert await _raw_message_count(pool) == 0

        valid_payload = _base_payload()
        valid_response = await _post(client, valid_payload)
        assert valid_response.status_code == 200
        assert await _raw_message_count(pool) == 1
        assert await _points_count(pool) == 1

    asyncio.run(_scenario(run))


def test_valid_surrogate_pair_is_accepted_and_round_trips_unchanged():
    # json.loads decodes a valid 😀 escape pair into the single
    # astral character it represents, not two separate surrogate halves, so
    # this must NOT be caught by the same guard that drops a lone surrogate:
    # over-blocking here would silently drop messages from any device whose
    # name contains an emoji.
    async def run(pool, client):
        payload = _base_payload(tid="\N{GRINNING FACE}")
        response = await _post(client, payload)
        assert response.status_code == 200
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT payload FROM raw_messages")
            stored = (await cur.fetchone())[0]
        assert stored == payload
        assert await _points_count(pool) == 1

    asyncio.run(_scenario(run))
