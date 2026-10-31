from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app.accounts import create_admin
from app.db import make_pool
from conftest import reset_db
from scripts import dev_seed
from scripts.dev_seed import PACIFIC, main_async, target_week

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


def test_week_scenario_seeds_a_complete_past_week_for_the_development_account():
    async def scenario():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            await reset_db(owner)
            before = datetime.now(timezone.utc)
            await main_async(TEST_DB, "week")
            after = datetime.now(timezone.utc)
            week = target_week(before)
            assert target_week(after) == week, "test straddled a week boundary"
            today = before.astimezone(PACIFIC).date()
            assert week.monday == today - timedelta(days=today.weekday() + 7)
            days = [week.monday + timedelta(days=i) for i in range(7)]

            async with owner.connection() as conn:
                account = await (await conn.execute(
                    "SELECT id, display_name FROM accounts WHERE email='development@localhost.invalid'"
                )).fetchone()
                assert account[1] == "Sam Rivera"
                assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)

                rows = await (await conn.execute(
                    "SELECT (started_at AT TIME ZONE 'America/Los_Angeles')::date, category::text, "
                    "distance_m, source::text, started_at, ended_at FROM trips "
                    "WHERE started_at >= %s AND started_at < %s ORDER BY started_at",
                    (week.start, week.end),
                )).fetchall()
                expense_count = (await (await conn.execute(
                    "SELECT count(*) FROM expenses WHERE incurred_on BETWEEN %s AND %s",
                    (days[0], days[-1]),
                )).fetchone())[0]
                rate_years = {r[0] for r in await (await conn.execute(
                    "SELECT year FROM mileage_rates WHERE account_id=%s", (account[0],)
                )).fetchall()}
                late_points = (await (await conn.execute(
                    "SELECT count(*) FROM points WHERE recorded_at >= %s AND recorded_at < %s "
                    "AND recorded_at > %s",
                    (week.start, week.end, after),
                )).fetchone())[0]

            # Exactly the scenario's detected trips: every day driven, none manual.
            assert {r[3] for r in rows} == {"detected"}
            by_day = {d: [r for r in rows if r[0] == d] for d in days}
            assert [len(by_day[d]) for d in days] == [2, 2, 4, 2, 2, 2, 2]
            miles = {d: sum(r[2] for r in by_day[d]) / 1609.344 for d in days}
            assert min(miles.values()) > 1
            assert min(miles.values()) < 5 and max(miles.values()) > 50

            by_category = {}
            for r in rows:
                by_category[r[1]] = by_category.get(r[1], 0.0) + r[2]
            assert set(by_category) == {"business", "personal", "unclassified"}
            assert by_category["business"] > by_category["personal"]
            assert by_category["business"] > sum(by_category.values()) * 0.6

            assert all(r[5] <= after for r in rows)
            assert late_points == 0
            assert expense_count >= 1
            assert {week.monday.year, (week.monday + timedelta(days=6)).year} <= rate_years
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_week_scenario_keeps_the_default_dataset_and_reseeds_identically():
    async def scenario():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            await reset_db(owner)
            snapshots = []
            for _ in range(2):
                await main_async(TEST_DB, "week")
                async with owner.connection() as conn:
                    snapshots.append((
                        await (await conn.execute(
                            "SELECT source::text, category::text, count(*) FROM trips "
                            "GROUP BY 1, 2 ORDER BY 1, 2")).fetchall(),
                        await (await conn.execute("SELECT count(*) FROM places")).fetchone(),
                        await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone(),
                        await (await conn.execute("SELECT count(*) FROM odometer_readings")).fetchone(),
                        await (await conn.execute("SELECT count(*) FROM expenses")).fetchone(),
                    ))
            assert snapshots[0] == snapshots[1]
            trips, places, vehicles, odometer, expenses = snapshots[0]
            assert places == (4,) and vehicles == (2,) and odometer == (2,)
            assert expenses == (6,)
            # The default dataset's 17 detected trips plus the scenario's 16.
            assert sum(n for src, _cat, n in trips if src == "detected") == 17 + 16
            assert sum(n for src, _cat, n in trips if src == "manual") == 3
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_refusal_comes_before_migrations_and_role_setup(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("ran against a refused database")

    async def scenario():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            await reset_db(owner)
            async with owner.connection() as conn:
                await create_admin(conn, "real@example.com", "unused-hash")
            monkeypatch.setattr(dev_seed, "run_migrations", must_not_run)
            monkeypatch.setattr(dev_seed, "application_role_pools", must_not_run)
            with pytest.raises(RuntimeError, match="requires the synthetic development account"):
                await main_async(TEST_DB)
        finally:
            await owner.close()

    asyncio.run(scenario())


def _fresh_database_url(name: str) -> str:
    return make_conninfo(TEST_DB, dbname=name)


def test_gate_handles_a_database_with_no_schema_or_an_old_one():
    name = "odograph_seed_gate"

    async def scenario():
        admin = await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True)
        await admin.execute(f"DROP DATABASE IF EXISTS {name}")
        await admin.execute(f"CREATE DATABASE {name}")
        try:
            async with await psycopg.AsyncConnection.connect(_fresh_database_url(name), autocommit=True) as conn:
                await dev_seed._refuse_a_real_database(conn)
                await conn.execute("CREATE TABLE schema_migrations (version int PRIMARY KEY)")
                with pytest.raises(RuntimeError, match="no accounts table"):
                    await dev_seed._refuse_a_real_database(conn)
        finally:
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            await admin.close()

    asyncio.run(scenario())
