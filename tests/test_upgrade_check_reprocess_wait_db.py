"""The upgrade drill's reprocess wait agrees with the runner's device eligibility."""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.account_context import account_id
from app.db import make_pool
from app.detector.runner import _PENDING_DEVICES_SQL, DETECTOR_VERSION
from conftest import reset_account_db, seed_tracking_device

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

UPGRADE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upgrade_check.sh"
STARTED = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
ENDED = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)


def _wait_sql(version: int) -> str:
    match = re.search(
        r'^\s+sql="(SELECT NOT EXISTS \(SELECT 1 FROM accounts a .*AS detector_reprocessed)"$',
        UPGRADE_SCRIPT.read_text(), re.MULTILINE,
    )
    assert match is not None
    return match.group(1).replace("${version}", str(version))


async def _point(conn, owner, device):
    await conn.execute(
        "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,received_at,geom) "
        "VALUES(%s,%s,'phone',%s,%s,ST_SetSRID(ST_MakePoint(-122.483,37.7694),4326)::geography)",
        (owner, device, STARTED, STARTED))


async def _trip(conn, owner, device, imported=False):
    # The storage envelope trigger refuses detector output without retained
    # points; skip it so the trip clause is tested on its own.
    await conn.execute("SET LOCAL session_replication_role = replica")
    await conn.execute(
        "INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,"
        "distance_m,category,imported) VALUES(%s,%s,'phone','detected',%s,%s,1000,'business',%s)",
        (owner, device, STARTED, ENDED, imported))
    await conn.execute("SET LOCAL session_replication_role = origin")


async def _override(conn, owner, device):
    await conn.execute(
        "INSERT INTO trip_boundary_overrides(account_id,tracking_device_id,device,kind,"
        "range_start,range_end) VALUES(%s,%s,'phone','discard',%s,%s)",
        (owner, device, STARTED, ENDED))


STALE = DETECTOR_VERSION - 1

# name: (stored detector_version, checkpoint set, data writer, admin change)
SCENARIOS = {
    "unused-device": (0, False, None, None),
    "points-only": (0, False, _point, None),
    "boundary-override-only": (0, False, _override, None),
    "detected-trip-only": (0, False, _trip, None),
    "imported-trip-only": (0, False, lambda c, o, d: _trip(c, o, d, imported=True), None),
    "checkpoint-only": (0, True, None, None),
    "stale-version-only": (STALE, False, None, None),
    "current-version-with-override": (DETECTOR_VERSION, True, _override, None),
    "disabled-device": (STALE, True, _override,
                        "UPDATE tracking_devices SET enabled = false WHERE id = %s"),
    "revoked-device": (STALE, True, _override,
                       "UPDATE tracking_devices SET revoked_at = now() WHERE id = %s"),
    "disabled-account": (STALE, True, _override,
                         "UPDATE accounts SET is_enabled = false "
                         "WHERE id = (SELECT account_id FROM tracking_devices WHERE id = %s)"),
}
PENDING = {
    "points-only", "boundary-override-only", "detected-trip-only", "checkpoint-only",
    "stale-version-only",
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_reprocess_wait_mirrors_the_runner_device_eligibility(scenario):
    version, checkpoint, writer, admin_change = SCENARIOS[scenario]

    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn)
            owner = account_id(conn)
        async with raw.connection() as conn:
            if writer is not None:
                await writer(conn, owner, device)
            await conn.execute(
                "UPDATE detector_state SET detector_version = %s, last_run_at = %s "
                "WHERE tracking_device_id = %s",
                (version, ENDED if checkpoint else None, device))
            if admin_change is not None:
                await conn.execute(admin_change, (device,))
            # Workers run enabled accounts only (app/account_workers.py).
            cur = await conn.execute(
                "SELECT a.id FROM accounts a JOIN tracking_devices d ON d.account_id = a.id "
                "WHERE d.id = %s AND a.is_enabled", (device,))
            owners = [row[0] for row in await cur.fetchall()]
            runner_pending = []
            for owner in owners:
                cur = await conn.execute(_PENDING_DEVICES_SQL, (owner, DETECTOR_VERSION))
                runner_pending += [row[0] for row in await cur.fetchall()]
            settled = (await (await conn.execute(_wait_sql(DETECTOR_VERSION))).fetchone())[0]
            await conn.execute(
                "UPDATE detector_state SET detector_version = %s WHERE tracking_device_id = %s",
                (DETECTOR_VERSION, device))
            after = (await (await conn.execute(_wait_sql(DETECTOR_VERSION))).fetchone())[0]
        return runner_pending, settled, after, device

    async def scenario_run():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            return await body(raw)
        finally:
            await raw.close()

    runner_pending, settled, after, device = asyncio.run(scenario_run())
    assert (runner_pending == [device]) is (scenario in PENDING)
    assert settled is (scenario not in PENDING)
    assert after is True
