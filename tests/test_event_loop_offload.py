"""Regression test for the export event-loop-blocking fix: CPU-bound
serialization must run off the event loop via asyncio.to_thread, not on it.

Verified by racing a "canary" coroutine (ticks on its own asyncio.sleep)
against the export endpoint with json.dumps monkeypatched to also do a real,
synchronous time.sleep(). If the serialize step isn't offloaded, that sleep
blocks the whole event loop and the canary is frozen for the entire window;
if it is offloaded (as it now is), the canary keeps ticking throughout.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

import app.portable as portable_module
from app.db import make_pool, run_migrations

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

BLOCK_S = 0.4
CANARY_INTERVAL_S = 0.02


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


def _endpoint(path: str, method: str):
    for route in portable_module.make_router().routes:
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} missing")


EXPORT_DATA = _endpoint("/settings/export/data", "GET")


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


async def _canary(tick_times: list[float]) -> None:
    while True:
        await asyncio.sleep(CANARY_INTERVAL_S)
        tick_times.append(time.monotonic())


def test_export_data_serialize_offload_keeps_event_loop_responsive(monkeypatch):
    real_dumps = json.dumps

    def blocking_dumps(*args, **kwargs):
        # Stands in for a large real bundle's serialization cost: a real,
        # synchronous block, not an awaitable one.
        time.sleep(BLOCK_S)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(portable_module.json, "dumps", blocking_dumps)

    async def scenario():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await _reset_schema(pool)
            tick_times: list[float] = []
            canary_task = asyncio.create_task(_canary(tick_times))
            block_start = time.monotonic()
            response = await EXPORT_DATA(_request(pool), {"sub": "test"})
            block_end = time.monotonic()
            canary_task.cancel()
            try:
                await canary_task
            except asyncio.CancelledError:
                pass
            return response, tick_times, block_start, block_end
        finally:
            await pool.close()

    response, tick_times, block_start, block_end = asyncio.run(scenario())
    assert response.status_code == 200

    # If the serialize step actually ran off the event loop, the canary
    # should have ticked repeatedly *during* the blocking window; if it ran
    # on the loop, no canary tick could land inside that window at all.
    ticks_during_block = [t for t in tick_times if block_start < t < block_end]
    assert len(ticks_during_block) > 1, (
        f"canary only ticked {len(ticks_during_block)} times during the "
        f"{BLOCK_S}s blocking window; event loop was blocked"
    )
