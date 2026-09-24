"""DB-backed tests for /ingest's stored-message-type allowlist.

`app/ingest.py` inserts every admitted message into `raw_messages` verbatim,
gated by a check for `_type` added specifically for this: OwnTracks' Publish
Settings button (and a remote `dump` cmd) sends `_type: "dump"` with a
`configuration` object holding the tracker's plaintext username, password,
and URL, and storing that message verbatim would put the password in the
database and its backups. Only `location`, `transition`, `waypoint`, and
`waypoints` messages are stored; every other type -- including `dump`, an
unrecognized type, or a payload with no `_type` at all -- is acknowledged
(200) and dropped before the INSERT, and only the (safely rendered,
truncated) type ever reaches the log.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.db import make_pool
from app.ingest import FailedAuthLimiter, make_router
from app.local_auth import hash_password
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
    ),
]

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"owntracks:testpw").decode()
}

SECRET_PASSWORD = "s3cr3t-tracker-password-must-never-be-logged"


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.control_pool = pool.control_pool
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
        transport = httpx.ASGITransport(
            app=_bare_app(pool), raise_app_exceptions=False
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await coro(pool, client)
    finally:
        await raw_pool.close()


async def _stored(pool) -> tuple[int, int]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (SELECT count(*) FROM raw_messages), (SELECT count(*) FROM points)"
        )
        return await cur.fetchone()


def _post(client, payload: dict):
    return client.post(
        "/ingest", headers=AUTH_HEADER, content=json.dumps(payload).encode()
    )


def _location_payload():
    return {
        "_type": "location", "tid": "aa",
        "lat": 47.6, "lon": -122.3, "tst": int(time.time()) - 60,
    }


def test_dump_payload_answers_200_stores_nothing_and_never_logs_the_password(caplog):
    """The Publish Settings button (and a remote `dump` cmd) sends this.

    Its `configuration` object carries the tracker's plaintext username,
    password, and URL -- the whole reason this allowlist exists.
    """
    async def run(pool, client):
        payload = {
            "_type": "dump",
            "configuration": {
                "username": "owntracks",
                "password": SECRET_PASSWORD,
                "url": "https://example.test/ingest",
            },
        }
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, payload)
        assert response.status_code == 200
        assert await _stored(pool) == (0, 0)
        assert SECRET_PASSWORD not in caplog.text

    asyncio.run(_scenario(run))


def test_object_typed_message_never_leaks_its_contents_to_the_log(caplog):
    """A non-string _type is rendered by its JSON kind only (<dict> here),
    never its contents, so a payload shaped like a malformed dump -- whose
    _type is itself an object holding a password -- can't leak it that way.

    Checks a 20-character prefix of SECRET_PASSWORD, not the whole string:
    the renderer this replaces truncated str(_type) to 40 characters, and
    the dict's "{'password': '...'" wrapper alone spent 14 of that budget,
    so it leaked only the password's first 26 characters -- never the whole
    44-character string, which is why a whole-string check wouldn't have
    caught it.
    """
    async def run(pool, client):
        payload = {"_type": {"password": SECRET_PASSWORD}}
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, payload)
        assert response.status_code == 200
        assert await _stored(pool) == (0, 0)
        assert SECRET_PASSWORD[:20] not in caplog.text

    asyncio.run(_scenario(run))


def test_unknown_message_type_answers_200_and_stores_nothing(caplog):
    async def run(pool, client):
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, {"_type": "status"})
        assert response.status_code == 200
        assert await _stored(pool) == (0, 0)
        assert "'status'" in caplog.text

    asyncio.run(_scenario(run))


def test_missing_message_type_answers_200_and_stores_nothing(caplog):
    async def run(pool, client):
        with caplog.at_level(logging.INFO, logger="app.ingest"):
            response = await _post(client, {"tid": "aa"})
        assert response.status_code == 200
        assert await _stored(pool) == (0, 0)
        assert "missing" in caplog.text

    asyncio.run(_scenario(run))


@pytest.mark.parametrize("msg_type", ["transition", "waypoint", "waypoints"])
def test_stored_non_location_type_is_recorded_but_creates_no_point(msg_type):
    async def run(pool, client):
        response = await _post(client, {"_type": msg_type, "tid": "aa"})
        assert response.status_code == 200
        assert await _stored(pool) == (1, 0)

    asyncio.run(_scenario(run))


def test_valid_location_still_stores_one_raw_row_and_one_point():
    """Contrast case, so the allowlist above cannot pass vacuously."""
    async def run(pool, client):
        response = await _post(client, _location_payload())
        assert response.status_code == 200
        assert await _stored(pool) == (1, 1)

    asyncio.run(_scenario(run))
