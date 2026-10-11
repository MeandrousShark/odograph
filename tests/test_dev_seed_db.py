from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.accounts import create_admin
from app.db import make_pool
from conftest import reset_db
from scripts.dev_seed import main_async

TEST_DB = os.environ.get("TEST_DATABASE_URL")
REPO_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


def test_seed_and_reseed_use_the_same_explicit_account_and_tracker():
    async def scenario():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            await reset_db(owner)
            snapshots = []
            for _ in range(2):
                await main_async(TEST_DB)
                async with owner.connection() as conn:
                    account = await (await conn.execute(
                        "SELECT id FROM accounts WHERE email='development@localhost.invalid'"
                    )).fetchone()
                    device = await (await conn.execute(
                        "SELECT account_id,id FROM tracking_devices WHERE label='QA-IPHONE'"
                    )).fetchone()
                    assert device[0] == account[0]
                    cur = await conn.execute(
                        "SELECT account_id,source::text,category::text,count(*),sum(point_count) "
                        "FROM trips GROUP BY 1,2,3 ORDER BY 1,2,3"
                    )
                    trips = await cur.fetchall()
                    assert all(row[0] == account[0] for row in trips)
                    assert len(trips) == 6
                    snapshots.append((account, device, trips))
                    assert await (await conn.execute(
                        "SELECT count(*) FROM points WHERE account_id<>%s OR tracking_device_id<>%s",
                        (account[0], device[1]),
                    )).fetchone() == (0,)
            assert snapshots[0] == snapshots[1]
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_seed_refuses_an_existing_nonsynthetic_owner_before_wiping_data():
    async def scenario():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            await reset_db(owner)
            async with owner.connection() as conn:
                account = await create_admin(conn, "other@example.invalid", "test-only-hash")
                await conn.execute(
                    "UPDATE vehicles SET name='Preserve this vehicle' WHERE account_id=%s",
                    (account["id"],),
                )
            with pytest.raises(RuntimeError, match="synthetic development account"):
                await main_async(TEST_DB)
            async with owner.connection() as conn:
                assert await (await conn.execute(
                    "SELECT account_id,name FROM vehicles"
                )).fetchall() == [(account["id"], "Preserve this vehicle")]
                assert await (await conn.execute("SELECT count(*) FROM tracking_devices")).fetchone() == (0,)
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_seed_runs_as_a_real_process_with_its_own_operation_owner():
    """The autouse owned_thread fixture never reaches a child process, so this
    proves the script satisfies the capacity contract on its own."""
    async def reset():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            await reset_db(owner)
        finally:
            await owner.close()

    asyncio.run(reset())
    result = subprocess.run(
        [sys.executable, "scripts/dev_seed.py", "--database-url", TEST_DB, "--wipe"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "trips by source/category" in result.stdout

    async def detected_trip_count():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            async with owner.connection() as conn:
                return await (await conn.execute(
                    "SELECT count(*) FROM trips WHERE source='detected'")).fetchone()
        finally:
            await owner.close()

    assert asyncio.run(detected_trip_count())[0] > 0
