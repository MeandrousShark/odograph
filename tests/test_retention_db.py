"""DB-backed regression test for RetentionWorker.

Like tests/test_snap_db.py, this needs a real Postgres and is skipped unless
TEST_DATABASE_URL is set. It seeds raw_messages with rows straddling the
retention window (using explicit `received_at` values rather than relying on
`now()` at insert time, since the window is computed relative to `now()` at
delete time) and asserts run_once() deletes exactly the rows older than the
window while leaving recent rows untouched, and that a second run_once() is a
no-op (idempotent, with nothing left to prune).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.db import make_pool
from app.retention import RetentionWorker
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

NOW = datetime.now(timezone.utc)


async def _insert_raw_message(conn, received_at, payload) -> int:
    cur = await conn.execute(
        "INSERT INTO raw_messages (account_id, received_at, payload) VALUES (%s, %s, %s) RETURNING id",
        (account_id(conn), received_at, json.dumps(payload)),
    )
    return (await cur.fetchone())[0]


async def _all_ids(conn) -> set[int]:
    cur = await conn.execute("SELECT id FROM raw_messages")
    return {row[0] for row in await cur.fetchall()}


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        old_at = NOW - timedelta(days=400)
        recent_at = NOW - timedelta(days=10)

        async with pool.connection() as conn:
            old_ids = {
                await _insert_raw_message(conn, old_at, {"_type": "location", "n": i})
                for i in range(3)
            }
            recent_ids = {
                await _insert_raw_message(conn, recent_at, {"_type": "location", "n": i})
                for i in range(2)
            }

        worker = RetentionWorker(pool, retention_days=365)
        await worker.run_once()

        async with pool.connection() as conn:
            surviving = await _all_ids(conn)
        assert surviving == recent_ids, (
            "run_once() must delete only rows older than the retention window"
        )
        assert surviving.isdisjoint(old_ids)

        # Idempotent: nothing left old enough to prune, so a second run is a no-op.
        await worker.run_once()
        async with pool.connection() as conn:
            surviving_again = await _all_ids(conn)
        assert surviving_again == recent_ids
    finally:
        await raw_pool.close()


def test_retentionworker_prunes_only_rows_older_than_window():
    asyncio.run(_scenario())


async def _large_window_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        recent_at = NOW - timedelta(days=10)

        async with pool.connection() as conn:
            recent_ids = {
                await _insert_raw_message(conn, recent_at, {"_type": "location", "n": i})
                for i in range(2)
            }

        # A retention window far larger than any row's age must never delete
        # recent rows, mirroring the "insurance kept indefinitely" intent
        # described in app/retention.py for large/disabled-adjacent windows.
        worker = RetentionWorker(pool, retention_days=3650)
        await worker.run_once()

        async with pool.connection() as conn:
            surviving = await _all_ids(conn)
        assert surviving == recent_ids
    finally:
        await raw_pool.close()


def test_retentionworker_large_window_keeps_recent_rows():
    asyncio.run(_large_window_scenario())
