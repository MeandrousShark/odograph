from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors

from app.account_settings import AccountSettings, CONFIG_PREFERENCE_COLUMNS
from app.accounts import create_admin
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.local_auth import verify_password
from app.ownership import OwnershipMigrationError, import_legacy_configuration
from conftest import drop_and_recreate_schema, full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")
BOOTSTRAP_SQL = Path(__file__).resolve().parents[1] / "scripts/sql/account_bootstrap.sql"


def _legacy_config(**changes):
    defaults = AccountSettings(display_tz=ZoneInfo("America/New_York"))
    values = {name: getattr(defaults, name) for name in CONFIG_PREFERENCE_COLUMNS}
    values.update(ingest_username="legacy-test", ingest_password="disposable-test-secret")
    values.update(changes)
    return SimpleNamespace(**values)


@asynccontextmanager
async def _legacy_database():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await drop_and_recreate_schema(pool)
        async with pool.connection() as conn:
            await conn.execute("CREATE TABLE schema_migrations(version int PRIMARY KEY, applied_at timestamptz DEFAULT now())")
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version >= 26:
                    break
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (version,))
        yield pool
    finally:
        await full_schema_reset(pool)
        await pool.close()


async def _fetch(pool, query, params=()):
    async with pool.connection() as conn:
        return await (await conn.execute(query, params)).fetchall()


async def _fresh_bootstrap():
    async with _legacy_database() as pool:
        await run_migrations(pool, _legacy_config(email_to="must-not-inherit@example.test"))
        assert await _fetch(pool, "SELECT count(*) FROM vehicles") == [(0,)]
        assert await _fetch(pool, "SELECT count(*) FROM mileage_rates") == [(0,)]
        assert await _fetch(pool, "SELECT count(*) FROM ingest_credentials") == [(0,)]
        async with pool.connection() as conn:
            await conn.execute(BOOTSTRAP_SQL.read_text())
            account = await create_admin(conn, "first@example.test", "hash", display_timezone="Europe/London")
        owner = account["id"]
        assert await _fetch(pool, "SELECT account_id,name,is_default FROM vehicles") == [(owner, "My Car", True)]
        assert await _fetch(pool, "SELECT account_id,display_tz,email_to,email_monthly_summary,ntfy_topic,auto_assign_default_vehicle FROM account_settings") == [(owner, "Europe/London", "", False, "", False)]
        assert await _fetch(pool, "SELECT count(*) FROM tag_rules WHERE account_id=%s", (owner,)) == [(2,)]
        assert await _fetch(pool, "SELECT count(*) FROM mileage_rates WHERE account_id=%s", (owner,)) == [(2,)]
        with pytest.raises(errors.UniqueViolation):
            async with pool.connection() as conn:
                await create_admin(conn, "second@example.test", "hash")
        with pytest.raises(errors.CheckViolation):
            async with pool.connection() as conn:
                await conn.execute("UPDATE accounts SET is_admin=false")


def test_fresh_migration_and_guarded_bootstrap_create_only_owned_neutral_defaults():
    asyncio.run(_fresh_bootstrap())


async def _valued_without_account(table, insert):
    async with _legacy_database() as pool:
        async with pool.connection() as conn:
            await conn.execute(insert)
        before = await _fetch(pool, f"SELECT count(*) FROM {table}")
        with pytest.raises(OwnershipMigrationError):
            await run_migrations(pool, _legacy_config())
        assert await _fetch(pool, f"SELECT count(*) FROM {table}") == before
        assert await _fetch(pool, "SELECT max(version) FROM schema_migrations") == [(25,)]
        assert await _fetch(pool, "SELECT to_regclass('tracking_devices')") == [(None,)]


@pytest.mark.parametrize("table,insert", [
    ("raw_messages", "INSERT INTO raw_messages(payload) VALUES('{}')"),
    ("geocode_cache", "INSERT INTO geocode_cache(lat,lon,address) VALUES(10,20,'private')"),
    ("email_deliveries", "INSERT INTO email_deliveries(kind,period_end,sent) VALUES('weekly_nudge',now(),true)"),
    ("vehicles", "UPDATE vehicles SET name='Personal name'"),
    ("tag_rules", "UPDATE tag_rules SET category='business' WHERE a_kind='home'"),
    ("mileage_rates", "UPDATE mileage_rates SET rate_per_mi=0.5 WHERE year=2025"),
    ("app_settings", "UPDATE app_settings SET auto_assign_default_vehicle=true"),
])
def test_ownerless_personal_history_or_edited_seeds_refuse_without_mutations(table, insert):
    asyncio.run(_valued_without_account(table, insert))


async def _migrated_account(monkeypatch):
    monkeypatch.setenv("MILEAGE_RATE_2026", "0.725123")
    async with _legacy_database() as pool:
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO accounts(id,email,password_hash) VALUES(42,'legacy@example.test','preserved-hash')")
            await conn.execute("UPDATE app_settings SET auto_assign_default_vehicle=true")
            await conn.execute("UPDATE detector_state SET last_run_at='2025-01-01Z',detector_version=2")
            await conn.execute("""
                INSERT INTO trips(id,device,started_at,ended_at,distance_m,category,tag_source,notes,exclusion,vehicle_id)
                OVERRIDING SYSTEM VALUE VALUES(88,'phone','2024-01-01Z','2024-01-01T01:00Z',1000,'business','human','kept','not_deductible',1)
            """)
            await conn.execute("""
                INSERT INTO points(id,device,recorded_at,geom,trip_id) OVERRIDING SYSTEM VALUE
                VALUES(77,'phone','2024-01-01T00:10Z',ST_SetSRID(ST_MakePoint(20,10),4326)::geography,88)
            """)
            await conn.execute("INSERT INTO trip_boundary_overrides(device,kind,point_id) VALUES('phone','force',77)")
            await conn.execute("INSERT INTO raw_messages(received_at,payload) VALUES('2024-01-01Z','{\"_type\":\"location\",\"lat\":10,\"lon\":20,\"tst\":1704067200,\"tid\":\"raw-only\"}')")
            await conn.execute("INSERT INTO raw_messages(payload) VALUES('{\"_type\":\"transition\"}')")
            await conn.execute("INSERT INTO email_deliveries(kind,period_end,sent) VALUES('weekly_nudge','2024-01-01Z',true)")
        cfg = _legacy_config(email_to="legacy@example.test", ntfy_topic="old-topic", email_monthly_summary=True)
        await run_migrations(pool, cfg)
        assert await _fetch(pool, "SELECT account_id,id,device,distance_m,category::text,tag_source::text,notes,exclusion::text FROM trips") == [(42,88,"phone",1000,"business","human","kept","not_deductible")]
        assert await _fetch(pool, "SELECT account_id,id,trip_id,ST_AsText(geom::geometry) FROM points") == [(42,77,88,"POINT(20 10)")]
        assert await _fetch(pool, "SELECT account_id,label FROM tracking_devices ORDER BY label") == [(42,"phone"),(42,"raw-only")]
        assert await _fetch(pool, "SELECT count(*) FROM detector_state WHERE account_id=42 AND last_run_at='2025-01-01Z' AND detector_version=2") == [(2,)]
        assert await _fetch(pool, "SELECT account_id,tracking_device_id IS NULL FROM raw_messages ORDER BY id") == [(42,False),(42,True)]
        assert await _fetch(pool, "SELECT rate_per_mi FROM mileage_rates WHERE account_id=42 AND year=2026") == [(Decimal("0.725123"),)]
        assert await _fetch(pool, "SELECT account_id,auto_assign_default_vehicle,display_tz,email_to,ntfy_topic FROM account_settings") == [(42,True,"America/New_York","legacy@example.test","old-topic")]
        credential = (await _fetch(pool, "SELECT public_id,basic_username,secret_hash,account_id,kind FROM ingest_credentials"))[0]
        assert credential[0] != credential[1]
        assert credential[1] == "legacy-test" and credential[3:] == (42,"legacy")
        assert verify_password("disposable-test-secret", credential[2])
        async with pool.connection() as conn:
            await conn.execute("UPDATE ingest_credentials SET revoked_at=now()")
            await conn.execute("UPDATE account_settings SET email_to='chosen@example.test'")
            await import_legacy_configuration(conn, _legacy_config(email_to="wrong@example.test"), environ={"MILEAGE_RATE_2026":"2"})
        await run_migrations(pool, cfg)
        assert await _fetch(pool, "SELECT revoked_at IS NOT NULL FROM ingest_credentials") == [(True,)]
        assert await _fetch(pool, "SELECT email_to FROM account_settings") == [("chosen@example.test",)]
        assert await _fetch(pool, "SELECT rate_per_mi FROM mileage_rates WHERE year=2026") == [(Decimal("0.725123"),)]
        assert await _fetch(pool, "SELECT account_id,sent FROM email_deliveries") == [(42,True)]


def test_non_one_owner_history_checkpoint_preferences_and_adapter_import_once(monkeypatch):
    asyncio.run(_migrated_account(monkeypatch))


async def _bootstrap_rollback():
    async with _legacy_database() as pool:
        await run_migrations(pool)
        async with pool.connection() as conn:
            await conn.execute(BOOTSTRAP_SQL.read_text())
        with pytest.raises(RuntimeError, match="abort"):
            async with pool.connection() as conn:
                await create_admin(conn, "abort@example.test", "hash")
                raise RuntimeError("abort")
        assert await _fetch(pool, "SELECT count(*) FROM accounts") == [(0,)]
        assert await _fetch(pool, "SELECT first_account_id FROM instance_state") == [(None,)]
        assert await _fetch(pool, "SELECT count(*) FROM vehicles") == [(0,)]
        async with pool.connection() as conn:
            await create_admin(conn, "retry@example.test", "hash")
        assert await _fetch(pool, "SELECT count(*) FROM accounts") == [(1,)]


def test_first_account_identity_guard_and_defaults_rollback_together():
    asyncio.run(_bootstrap_rollback())


async def _bad_relationship():
    async with _legacy_database() as pool:
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO accounts(id,email,password_hash) VALUES(42,'a@example.test','hash')")
            await conn.execute("INSERT INTO trips(device,started_at,ended_at,distance_m) VALUES('other',now(),now(),1)")
            await conn.execute("INSERT INTO points(device,recorded_at,geom,trip_id) VALUES('phone',now(),ST_SetSRID(ST_MakePoint(20,10),4326)::geography,1)")
        with pytest.raises(OwnershipMigrationError, match="relationships"):
            await run_migrations(pool)
        assert await _fetch(pool, "SELECT max(version) FROM schema_migrations") == [(25,)]


def test_cross_device_legacy_relationship_refuses_migration():
    asyncio.run(_bad_relationship())


async def _owned_constraints():
    async with _legacy_database() as pool:
        await run_migrations(pool)
        async with pool.connection() as conn:
            await conn.execute(BOOTSTRAP_SQL.read_text())
            first = (await create_admin(conn, "a@example.test", "hash"))["id"]
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("INSERT INTO accounts(id,email,password_hash) VALUES(99,'b@example.test','hash')")
            a_device = (await (await conn.execute("INSERT INTO tracking_devices(account_id,label) VALUES(%s,'same') RETURNING id", (first,))).fetchone())[0]
            b_device = (await (await conn.execute("INSERT INTO tracking_devices(account_id,label) VALUES(99,'same') RETURNING id")).fetchone())[0]
            other_a_device = (await (await conn.execute("INSERT INTO tracking_devices(account_id,label) VALUES(%s,'same') RETURNING id", (first,))).fetchone())[0]
            trip = (await (await conn.execute("INSERT INTO trips(account_id,tracking_device_id,device,started_at,ended_at,distance_m) VALUES(%s,%s,'same',now(),now(),1) RETURNING id", (first,a_device))).fetchone())[0]
            vehicle = (await (await conn.execute("SELECT id FROM vehicles WHERE account_id=%s", (first,))).fetchone())[0]
            await conn.execute("INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment,trip_id) VALUES(%s,%s,'2024-01-01','fuel',10,'business_use_allocated',%s)", (first,vehicle,trip))
            await conn.execute("INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,trip_id) VALUES(%s,%s,'same',now(),ST_SetSRID(ST_MakePoint(20,10),4326)::geography,%s)", (first,a_device,trip))
            for owner, device in ((99,b_device),(first,other_a_device)):
                with pytest.raises(errors.ForeignKeyViolation):
                    async with conn.transaction():
                        await conn.execute("INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,trip_id) VALUES(%s,%s,'same',now(),ST_SetSRID(ST_MakePoint(20,10),4326)::geography,%s)", (owner,device,trip))
            with pytest.raises(errors.ForeignKeyViolation):
                async with conn.transaction():
                    await conn.execute("INSERT INTO odometer_readings(account_id,vehicle_id,recorded_at,odometer_m) VALUES(99,%s,now(),1)", (vehicle,))
            await conn.execute("DELETE FROM trips WHERE account_id=%s AND id=%s", (first,trip))
        assert await _fetch(pool, "SELECT account_id,trip_id FROM expenses") == [(first,None)]
        assert await _fetch(pool, "SELECT account_id,tracking_device_id,trip_id FROM points") == [(first,a_device,None)]
        assert await _fetch(pool, "SELECT count(*) FROM tracking_devices WHERE label='same'") == [(3,)]


def test_owned_foreign_keys_reject_other_account_or_stream_and_optional_delete_keeps_owner():
    asyncio.run(_owned_constraints())


async def _interrupted_migration(monkeypatch):
    import app.ownership as ownership_module
    async with _legacy_database() as pool:
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO accounts(id,email,password_hash) VALUES(42,'a@example.test','hash')")
        original = ownership_module.import_legacy_configuration
        async def interrupted(conn, config):
            await original(conn, config)
            raise RuntimeError("interrupted migration")
        monkeypatch.setattr(ownership_module, "import_legacy_configuration", interrupted)
        with pytest.raises(RuntimeError, match="interrupted migration"):
            await run_migrations(pool, _legacy_config())
        assert await _fetch(pool, "SELECT max(version) FROM schema_migrations") == [(25,)]
        assert await _fetch(pool, "SELECT to_regclass('tracking_devices')") == [(None,)]
        assert await _fetch(pool, "SELECT count(*) FROM app_settings") == [(1,)]
        monkeypatch.setattr(ownership_module, "import_legacy_configuration", original)
        await run_migrations(pool, _legacy_config())
        assert await _fetch(pool, "SELECT count(*) FROM ingest_credentials") == [(1,)]
        assert await _fetch(pool, "SELECT account_id FROM vehicles") == [(42,)]


def test_interrupted_config_import_rolls_back_schema_backfill_marker_and_credentials(monkeypatch):
    asyncio.run(_interrupted_migration(monkeypatch))


async def _concurrent_bootstrap():
    async with _legacy_database() as pool:
        await run_migrations(pool)
        async with pool.connection() as conn:
            await conn.execute(BOOTSTRAP_SQL.read_text())
        started = asyncio.Event()
        release = asyncio.Event()
        async def first():
            async with pool.connection() as conn:
                account = await create_admin(conn, "first@example.test", "hash")
                started.set()
                await release.wait()
                return account
        async def second():
            await started.wait()
            async with pool.connection() as conn:
                return await create_admin(conn, "second@example.test", "hash")
        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await started.wait()
        try:
            async def wait_until_database_blocks_second():
                while True:
                    rows = await _fetch(pool, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'SELECT public.bootstrap_first_account%%'")
                    if rows[0][0]:
                        return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_until_database_blocks_second(), 5)
            assert not second_task.done()
        finally:
            release.set()
        await first_task
        with pytest.raises(errors.UniqueViolation):
            await second_task
        assert await _fetch(pool, "SELECT email FROM accounts") == [("first@example.test",)]
        assert await _fetch(pool, "SELECT count(*) FROM vehicles") == [(1,)]


def test_concurrent_bootstrap_serializes_identity_and_personal_defaults():
    asyncio.run(_concurrent_bootstrap())


async def _unexpected_multiple_accounts():
    async with _legacy_database() as pool:
        async with pool.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("INSERT INTO accounts(email,password_hash) VALUES('a@example.test','hash'),('b@example.test','hash')")
        with pytest.raises(OwnershipMigrationError, match="at most one"):
            await run_migrations(pool)
        assert await _fetch(pool, "SELECT max(version) FROM schema_migrations") == [(25,)]
        assert await _fetch(pool, "SELECT count(*) FROM accounts") == [(2,)]


def test_unexpected_multiple_accounts_are_not_silently_reassigned():
    asyncio.run(_unexpected_multiple_accounts())


async def _drifted_singleton_index(definition, has_account):
    async with _legacy_database() as pool:
        async with pool.connection() as conn:
            if has_account:
                await conn.execute(
                    "INSERT INTO accounts(id,email,password_hash) "
                    "VALUES(42,'existing@example.test','preserved-hash')"
                )
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute(definition)
        before = await _fetch(pool, "SELECT id,email,password_hash FROM accounts")
        with pytest.raises(OwnershipMigrationError, match="account guards are inconsistent"):
            await run_migrations(pool, _legacy_config())
        assert await _fetch(pool, "SELECT max(version) FROM schema_migrations") == [(25,)]
        assert await _fetch(pool, "SELECT to_regclass('tracking_devices')") == [(None,)]
        assert await _fetch(pool, "SELECT id,email,password_hash FROM accounts") == before
        assert await _fetch(pool, "SELECT name FROM vehicles") == [("My Car",)]


@pytest.mark.parametrize("definition", [
    "CREATE UNIQUE INDEX accounts_singleton_idx ON accounts(id)",
    "CREATE UNIQUE INDEX accounts_singleton_idx ON accounts((true)) WHERE is_enabled",
])
@pytest.mark.parametrize("has_account", [False, True])
def test_same_name_wrong_key_or_partial_singleton_index_refuses_migration(definition, has_account):
    asyncio.run(_drifted_singleton_index(definition, has_account))
