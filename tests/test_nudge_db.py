"""DB-backed delivery-ledger tests for the nudge worker."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.db import make_pool, run_migrations
from app.nudge import NudgeWorker

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

TZ = ZoneInfo("America/Los_Angeles")
WINDOW_END = datetime(2026, 7, 12, 18, tzinfo=TZ)


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_trip(conn, started_at, category="unclassified") -> None:
    await conn.execute(
        "INSERT INTO trips (device, started_at, ended_at, distance_m, category) "
        "VALUES ('phone', %s, %s, 1000, %s)",
        (started_at, started_at + timedelta(minutes=15), category),
    )


async def _ledger_scenario():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, WINDOW_END - timedelta(days=1))
            await _insert_trip(conn, WINDOW_END - timedelta(days=8))
            await _insert_trip(conn, WINDOW_END - timedelta(days=1), "business")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = NudgeWorker(
                pool, client, "https://ntfy.example.com", "mileage", "", "", "", "", TZ, 18,
            )
            await worker.run_once(WINDOW_END + timedelta(hours=1))
            await worker.run_once(WINDOW_END + timedelta(days=1))

        assert calls == 1
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT trip_count FROM nudge_delivery_windows WHERE window_ends_at = %s", (WINDOW_END,)
            )
            assert await cur.fetchone() == (1,)
    finally:
        await pool.close()


def test_nudge_delivery_ledger_counts_only_window_trips_and_prevents_duplicate():
    asyncio.run(_ledger_scenario())


async def _retry_scenario():
    responses = [500, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(responses.pop(0))

    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, WINDOW_END - timedelta(days=1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = NudgeWorker(
                pool, client, "https://ntfy.example.com", "mileage", "", "", "", "", TZ, 18,
            )
            with pytest.raises(httpx.HTTPStatusError):
                await worker.run_once(WINDOW_END + timedelta(hours=1))
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT count(*) FROM nudge_delivery_windows")
                assert await cur.fetchone() == (0,)
            await worker.run_once(WINDOW_END + timedelta(days=1))

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT trip_count FROM nudge_delivery_windows")
            assert await cur.fetchone() == (1,)
    finally:
        await pool.close()


def test_nudge_retries_after_failed_ntfy_response_without_marking_window_done():
    asyncio.run(_retry_scenario())
