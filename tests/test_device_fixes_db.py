"""DB-backed tests for the Settings page's last-fix diagnostics query
(`app.ui._fetch_device_fixes`): per-device newest received/recorded
timestamps and point counts, plus the no-points-yet empty case.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.db import make_pool, run_migrations
from app.ui import _fetch_device_fixes

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

UTC = timezone.utc
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_point(conn, device, recorded_at, received_at) -> None:
    await conn.execute(
        "INSERT INTO points (device, recorded_at, received_at, geom, accuracy_m) "
        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(-122.0, 47.0), 4326)::geography, 10)",
        (device, recorded_at, received_at),
    )


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        async with pool.connection() as conn:
            fixes = await _fetch_device_fixes(conn)
        assert fixes == []

        async with pool.connection() as conn:
            # Device "phone": three points, received_at intentionally out of
            # step with recorded_at so the newest-of-each assertion actually
            # distinguishes the two columns rather than passing by accident.
            await _insert_point(conn, "phone", T0, T0 + timedelta(seconds=5))
            await _insert_point(
                conn, "phone", T0 + timedelta(minutes=10), T0 + timedelta(minutes=10, seconds=5)
            )
            await _insert_point(
                conn, "phone", T0 + timedelta(minutes=5), T0 + timedelta(hours=1)
            )
            # Device "test": one point, well before "phone"'s newest fixes,
            # so device ordering (alphabetical) is also exercised.
            await _insert_point(conn, "test", T0 - timedelta(days=1), T0 - timedelta(days=1))

        async with pool.connection() as conn:
            fixes = await _fetch_device_fixes(conn)

        assert [f["device"] for f in fixes] == ["phone", "test"]
        phone = fixes[0]
        assert phone["point_count"] == 3
        assert phone["newest_recorded_at"] == T0 + timedelta(minutes=10)
        # The out-of-order received_at on the middle-recorded_at point is
        # the newest received_at overall.
        assert phone["newest_received_at"] == T0 + timedelta(hours=1)

        test_device = fixes[1]
        assert test_device["point_count"] == 1
        assert test_device["newest_recorded_at"] == T0 - timedelta(days=1)
        assert test_device["newest_received_at"] == T0 - timedelta(days=1)
    finally:
        await pool.close()


def test_fetch_device_fixes_reports_per_device_counts_and_newest_timestamps():
    asyncio.run(_scenario())
