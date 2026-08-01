"""DB-backed tests for the /ingest body-size cap. Auth is checked before
the cap either way (there's no
body read on that path), but the unauthenticated case is included as a
regression guard against ever reordering the two checks.
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

from app.db import make_pool, run_migrations
from app.ingest import FailedAuthLimiter, make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"owntracks:testpw").decode()
}
CAP = 200


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.config = SimpleNamespace(
        ingest_username="owntracks",
        ingest_password="testpw",
        ingest_max_body_bytes=CAP,
    )
    app.state.ingest_limiter = FailedAuthLimiter(1000, 60.0)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda: None)
    app.include_router(make_router())
    return app


async def _scenario(coro) -> None:
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        transport = httpx.ASGITransport(app=_bare_app(pool))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await coro(pool, client)
    finally:
        await pool.close()


async def _raw_message_count(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM raw_messages")
        return (await cur.fetchone())[0]


async def _chunked_body(total_bytes: int):
    sent = 0
    while sent < total_bytes:
        chunk = b"x" * min(10, total_bytes - sent)
        sent += len(chunk)
        yield chunk


def test_oversized_declared_content_length_is_dropped_without_storing(caplog):
    async def run(pool, client):
        with caplog.at_level(logging.WARNING, logger="app.ingest"):
            response = await client.post(
                "/ingest", headers=AUTH_HEADER, content=b"{" + b"x" * (CAP + 100)
            )
        assert response.status_code == 200
        assert await _raw_message_count(pool) == 0
        assert "oversized body" in caplog.text

    asyncio.run(_scenario(run))


def test_oversized_streamed_body_without_content_length_is_dropped(caplog):
    async def run(pool, client):
        with caplog.at_level(logging.WARNING, logger="app.ingest"):
            response = await client.post(
                "/ingest", headers=AUTH_HEADER, content=_chunked_body(CAP + 100)
            )
        assert response.status_code == 200
        assert await _raw_message_count(pool) == 0
        assert "oversized body" in caplog.text

    asyncio.run(_scenario(run))


def test_unauthenticated_oversized_request_gets_401_not_200():
    async def run(pool, client):
        response = await client.post("/ingest", content=b"x" * (CAP + 100))
        assert response.status_code == 401
        assert await _raw_message_count(pool) == 0

    asyncio.run(_scenario(run))


def test_normal_payload_within_cap_still_ingests():
    async def run(pool, client):
        payload = json.dumps({
            "_type": "location", "tid": "aa",
            "lat": 47.6, "lon": -122.3, "tst": int(time.time()) - 60,
        }).encode()
        assert len(payload) < CAP
        response = await client.post("/ingest", headers=AUTH_HEADER, content=payload)
        assert response.status_code == 200
        assert await _raw_message_count(pool) == 1
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM points")
            assert (await cur.fetchone())[0] == 1

    asyncio.run(_scenario(run))
