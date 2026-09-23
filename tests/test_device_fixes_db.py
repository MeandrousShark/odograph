"""DB-backed tests for the Settings page's last-fix diagnostics query
(`app.ui._fetch_device_fixes`): per-device newest received/recorded
timestamps and point counts, one row per tracking device even across a
`tid` change, two same-labelled devices staying separate, and the
no-points-yet empty case.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.db import make_pool
from app.account_context import account_id
from app.ui import _fetch_device_fixes
from conftest import reset_account_db, seed_tracking_device

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

UTC = timezone.utc
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)


async def _insert_point(conn, tracking_device_id, tid, recorded_at, received_at) -> None:
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, "
        "geom, accuracy_m) VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(-122.0, 47.0), "
        "4326)::geography, 10)",
        (account_id(conn), tracking_device_id, tid, recorded_at, received_at),
    )


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)

        async with pool.connection() as conn:
            fixes = await _fetch_device_fixes(conn)
        assert fixes == []

        async with pool.connection() as conn:
            phone = await seed_tracking_device(conn, "Phone")
            bench = await seed_tracking_device(conn, "Bench tester")
            # Device "Phone": three points, received_at intentionally out of
            # step with recorded_at so the newest-of-each assertion actually
            # distinguishes the two columns rather than passing by accident.
            # Its `tid` changes partway through -- a relabeled OwnTracks
            # tracker ID must not split one device into two rows.
            await _insert_point(conn, phone, "old-tid", T0, T0 + timedelta(seconds=5))
            await _insert_point(
                conn, phone, "new-tid", T0 + timedelta(minutes=10), T0 + timedelta(minutes=10, seconds=5)
            )
            await _insert_point(
                conn, phone, "new-tid", T0 + timedelta(minutes=5), T0 + timedelta(hours=1)
            )
            # Device "Bench tester": one point, well before "Phone"'s newest
            # fixes, so device ordering (alphabetical by name) is also
            # exercised.
            await _insert_point(conn, bench, "test", T0 - timedelta(days=1), T0 - timedelta(days=1))

        async with pool.connection() as conn:
            fixes = await _fetch_device_fixes(conn)

        assert [f["device_label"] for f in fixes] == ["Bench tester", "Phone"]
        bench_row, phone_row = fixes

        assert phone_row["point_count"] == 3
        assert phone_row["newest_recorded_at"] == T0 + timedelta(minutes=10)
        # The out-of-order received_at on the middle-recorded_at point is
        # the newest received_at overall.
        assert phone_row["newest_received_at"] == T0 + timedelta(hours=1)
        # The most recent point (by recorded_at) carries the newer tid.
        assert phone_row["latest_tid"] == "new-tid"

        assert bench_row["point_count"] == 1
        assert bench_row["newest_recorded_at"] == T0 - timedelta(days=1)
        assert bench_row["newest_received_at"] == T0 - timedelta(days=1)
        assert bench_row["latest_tid"] == "test"
    finally:
        await raw_pool.close()


def test_fetch_device_fixes_reports_per_device_counts_and_newest_timestamps():
    asyncio.run(_scenario())


async def _same_label_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            first = await seed_tracking_device(conn, "Shared")
            second = await seed_tracking_device(conn, "Shared")
            await _insert_point(conn, first, "shared", T0, T0)
            await _insert_point(conn, second, "shared", T0 + timedelta(minutes=1), T0 + timedelta(minutes=1))

        async with pool.connection() as conn:
            fixes = await _fetch_device_fixes(conn)

        assert len(fixes) == 2
        assert {f["tracking_device_id"] for f in fixes} == {first, second}
        assert all(f["device_label"] == "Shared" for f in fixes)
        assert [f["point_count"] for f in fixes] == [1, 1]
    finally:
        await raw_pool.close()


def test_fetch_device_fixes_keeps_two_devices_with_the_same_label_separate():
    asyncio.run(_same_label_scenario())
