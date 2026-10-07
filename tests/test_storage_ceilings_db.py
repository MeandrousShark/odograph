"""Funded grants and transaction-final logical storage ceilings."""
import asyncio
import os
import shutil
from types import SimpleNamespace

import psycopg
import pytest

import app.db as db_module
from app.account_context import account_id
from app.application_roles import _load_state, prepare_application_roles, validate_application_contract
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.role_setup import RoleSetupError
from app.storage import is_storage_capacity_error, storage_status
from conftest import (drop_and_recreate_schema, full_schema_reset, reset_account_db,
                      seed_tracking_device)


TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable TEST_DATABASE_URL")


def _run(body):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            await body(raw, pool)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_funded_grant_exact_boundary_and_overbudget_net_refund():
    async def body(raw, pool):
        async with pool.connection() as conn:
            owner = account_id(conn)
            before = await storage_status(conn)
            assert before["account_limit_bytes"] == 2 * 1024 ** 3
            assert before["total_bytes"] == before["actual_bytes"] + before["reserved_bytes"]
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET account_limit_bytes=%s,raw_limit_bytes=%s,"
                "enhancement_limit_bytes=1 WHERE account_id=%s",
                (before["total_bytes"] + 256, 256, owner),
            )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO raw_messages(account_id,payload) VALUES(%s,%s::jsonb) "
                "RETURNING id,128+octet_length(convert_to(payload::text,'UTF8'))",
                (owner, '{"text":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}'),
            )
            message_id, charge = await cur.fetchone()
            assert charge < 256
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}'::jsonb)",
                                   (owner,))
        assert is_storage_capacity_error(refused.value)
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET account_limit_bytes=%s,raw_limit_bytes=%s "
                "WHERE account_id=%s", (before["total_bytes"] + 128, 128, owner),
            )
        # Lowering a grant keeps the existing row. A net-negative replacement
        # can still commit while the account remains over its new limit.
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM raw_messages WHERE id=%s", (message_id,))
            await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}'::jsonb)",
                               (owner,))
        async with pool.connection() as conn:
            after = await storage_status(conn)
            assert after["total_bytes"] == before["total_bytes"] + 130
            assert after["account_blocked"] and after["raw_blocked"]
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}'::jsonb)",
                                   (owner,))
        assert is_storage_capacity_error(refused.value)
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM raw_messages")
        async with raw.connection() as conn:
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()")
                          ).fetchone())[0]
    _run(body)


def test_concurrent_device_writes_do_not_overshoot_account_boundary():
    async def body(raw, pool):
        async with pool.connection() as conn:
            owner = account_id(conn)
            devices = (await seed_tracking_device(conn, "phone-a"),
                       await seed_tracking_device(conn, "phone-b"))
            before = await storage_status(conn)
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET account_limit_bytes=%s,raw_limit_bytes=130,"
                "enhancement_limit_bytes=1 "
                "WHERE account_id=%s", (before["total_bytes"] + 130, owner))

        ready = asyncio.Event()
        waiting = 0

        async def write(device):
            nonlocal waiting
            async with pool.connection() as conn:
                waiting += 1
                if waiting == 2:
                    ready.set()
                await ready.wait()
                await conn.execute(
                    "INSERT INTO raw_messages(account_id,tracking_device_id,payload) "
                    "VALUES(%s,%s,'{}'::jsonb)", (owner, device))

        results = await asyncio.gather(*(write(device) for device in devices),
                                       return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(is_storage_capacity_error(result) for result in results
                   if isinstance(result, Exception)) == 1
        async with raw.connection() as conn:
            assert (await (await conn.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE account_id=%s", (owner,)
            )).fetchone())[0] == 1
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()")
                          ).fetchone())[0]
    _run(body)


@pytest.mark.parametrize("repair_drift", [False, True])
def test_maintenance_reconciliation_preserves_existing_overbudget_data(repair_drift):
    async def body(raw, pool):
        async with pool.connection() as conn:
            owner = account_id(conn)
            await conn.execute(
                "INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{\"preserved\":true}')",
                (owner,),
            )
            before = await (await conn.execute(
                "SELECT actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes "
                "FROM account_usage WHERE account_id=%s", (owner,),
            )).fetchone()
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET account_limit_bytes=1,raw_limit_bytes=1,"
                "enhancement_limit_bytes=1 WHERE account_id=%s", (owner,),
            )
        if repair_drift:
            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE account_usage SET actual_bytes=0,reserved_bytes=0,raw_bytes=0,"
                    "enhancement_bytes=0 WHERE account_id=%s", (owner,),
                )
                assert not (await (await conn.execute(
                    "SELECT public.storage_usage_consistent()"
                )).fetchone())[0]
        async with raw.connection() as conn:
            await conn.execute("SELECT public.reconcile_storage_usage()")
        async with pool.connection() as conn:
            assert await (await conn.execute(
                "SELECT actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes "
                "FROM account_usage WHERE account_id=%s", (owner,),
            )).fetchone() == before
            assert (await storage_status(conn))["account_blocked"]
            assert (await (await conn.execute("SELECT count(*) FROM raw_messages")).fetchone())[0] == 1
        async with raw.connection() as conn:
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()"))
                    .fetchone())[0]
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}')", (owner,))
        assert is_storage_capacity_error(refused.value)
        async with pool.connection() as conn:
            await conn.execute("UPDATE raw_messages SET payload=payload WHERE account_id=%s", (owner,))
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM raw_messages WHERE account_id=%s", (owner,))
        async with raw.connection() as conn:
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()"))
                    .fetchone())[0]
    _run(body)


def test_grant_budget_creation_disable_and_purge_release():
    async def body(raw, pool):
        async with raw.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            await conn.execute("UPDATE storage_policy SET instance_budget_bytes=3*2147483648,"
                               "instance_reserve_bytes=2147483648 WHERE id=1")
            cur = await conn.execute("SELECT COUNT(*) FROM storage_grants")
            assert (await cur.fetchone())[0] == 1
        async with raw.connection() as conn:
            await conn.execute("INSERT INTO accounts(email,password_hash,is_admin) "
                               "VALUES('second@example.invalid','unused',false)")
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with raw.connection() as conn:
                await conn.execute("INSERT INTO accounts(email,password_hash,is_admin) "
                                   "VALUES('third@example.invalid','unused',false)")
        assert is_storage_capacity_error(refused.value)
        async with raw.connection() as conn:
            assert (await (await conn.execute("SELECT COUNT(*) FROM storage_grants")
                          ).fetchone())[0] == 2
            await conn.execute("UPDATE accounts SET is_enabled=false "
                               "WHERE email='second@example.invalid'")
            assert (await (await conn.execute("SELECT COUNT(*) FROM storage_grants")
                          ).fetchone())[0] == 2
            await conn.execute("DELETE FROM accounts WHERE email='second@example.invalid'")
            assert (await (await conn.execute("SELECT COUNT(*) FROM storage_grants")
                          ).fetchone())[0] == 1
    _run(body)


def test_restart_limits_regrant_existing_accounts_and_reject_unfunded_budget():
    async def body(raw, pool):
        async with pool.connection() as conn:
            owner = account_id(conn)
            before = await storage_status(conn)
        limit = before["total_bytes"] - 1
        reserve = 100

        def configured(budget, account_limit):
            return SimpleNamespace(
                storage_account_limit_bytes=account_limit,
                storage_raw_limit_bytes=1,
                storage_enhancement_limit_bytes=1,
                storage_instance_budget_bytes=budget,
                storage_instance_reserve_bytes=reserve,
            )

        await prepare_application_roles(TEST_DB, storage_config=configured(limit + reserve, limit))
        async with pool.connection() as conn:
            status = await storage_status(conn)
            assert status["account_limit_bytes"] == limit
            assert status["account_blocked"]
        with pytest.raises(RoleSetupError, match="application database setup failed"):
            await prepare_application_roles(TEST_DB,
                                            storage_config=configured(limit + reserve - 1, limit))
        async with raw.connection() as conn:
            assert (await (await conn.execute(
                "SELECT account_limit_bytes FROM storage_grants WHERE account_id=%s", (owner,)
            )).fetchone()) == (limit,)
            assert (await (await conn.execute(
                "SELECT instance_budget_bytes,instance_reserve_bytes FROM storage_policy WHERE id=1"
            )).fetchone()) == (limit + reserve, reserve)
        await prepare_application_roles(TEST_DB,
                                        storage_config=configured(2 * limit + reserve, 2 * limit))
        async with pool.connection() as conn:
            assert (await storage_status(conn))["account_limit_bytes"] == 2 * limit
    _run(body)


def test_prepaid_core_output_commits_at_account_ceiling():
    async def body(raw, pool):
        async with pool.connection() as conn:
            owner = account_id(conn)
            stream = await seed_tracking_device(conn, "phone")
            await conn.execute(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom) "
                "VALUES(%s,%s,'phone',now(),ST_SetSRID(ST_MakePoint(1,2),4326)::geography)",
                (owner, stream),
            )
            before = await storage_status(conn)
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET account_limit_bytes=%s,raw_limit_bytes=1,"
                "enhancement_limit_bytes=1 WHERE account_id=%s",
                (before["total_bytes"], owner),
            )
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO stays(account_id,tracking_device_id,device,started_at,ended_at,"
                "centroid,point_count) VALUES(%s,%s,'phone',now(),now(),"
                "ST_SetSRID(ST_MakePoint(1,2),4326)::geography,1)",
                (owner, stream),
            )
            assert (await storage_status(conn))["total_bytes"] == before["total_bytes"]
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m) "
                    "VALUES(%s,'manual','manual',now(),now(),1)", (owner,),
                )
        assert is_storage_capacity_error(refused.value)
        async with raw.connection() as conn:
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()")
                          ).fetchone())[0]
    _run(body)


def test_avatar_replacement_and_clear_use_transaction_net_charge():
    async def body(raw, pool):
        async with pool.connection() as conn:
            owner = account_id(conn)
            before = await storage_status(conn)
        async with pool.control_pool.connection() as conn:
            await conn.execute("UPDATE accounts SET avatar_bytes=%s,avatar_mime='image/png',"
                               "avatar_updated_at=now() "
                               "WHERE id=%s", (b"abcd", owner))
        async with raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET account_limit_bytes=%s,"
                               "raw_limit_bytes=1,enhancement_limit_bytes=1 WHERE account_id=%s",
                               (before["total_bytes"] + 4 + 9, owner))
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with pool.control_pool.connection() as conn:
                await conn.execute("UPDATE accounts SET avatar_bytes=%s WHERE id=%s",
                                   (b"abcdefgh", owner))
        assert is_storage_capacity_error(refused.value)
        async with raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET account_limit_bytes=%s WHERE account_id=%s",
                               (before["total_bytes"] + 1, owner))
        async with pool.control_pool.connection() as conn:
            await conn.execute("UPDATE accounts SET avatar_bytes=NULL,avatar_mime=NULL,"
                               "avatar_updated_at=NULL WHERE id=%s",
                               (owner,))
        async with pool.connection() as conn:
            assert (await storage_status(conn))["total_bytes"] == before["total_bytes"]
    _run(body)


@pytest.mark.parametrize("schema_version", [40, 41])
def test_pre_s2_role_contract_restores_without_new_schema(monkeypatch, tmp_path, schema_version):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            await drop_and_recreate_schema(raw)
            historical_dir = tmp_path / "migrations"
            historical_dir.mkdir()
            for source in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if int(source.name.split("_", 1)[0]) <= schema_version:
                    shutil.copy(source, historical_dir / source.name)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", historical_dir)
            await run_migrations(raw)
            await prepare_application_roles(TEST_DB)
            async with raw.connection() as conn:
                await validate_application_contract(conn, await _load_state(conn))
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await full_schema_reset(raw)
            await raw.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("statement,cause", [
    ("DROP INDEX raw_replay_receipts_lookup_idx", "S2 index set"),
    ("ALTER TABLE storage_grants DROP CONSTRAINT storage_grants_raw_limit_bytes_check",
     "S2 constraint definition"),
    ("ALTER TABLE account_usage DISABLE TRIGGER storage_ceiling_final",
     "storage trigger definition"),
    ("CREATE OR REPLACE FUNCTION public.storage_check_ceiling() RETURNS trigger "
     "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp "
     "AS $body$ BEGIN RETURN NULL; END $body$", "function definition"),
    ("CREATE OR REPLACE FUNCTION public.reconcile_storage_usage() RETURNS void "
     "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp "
     "AS $body$ BEGIN RETURN; END $body$", "function definition"),
])
def test_quota_contract_drift_refuses_startup(statement, cause):
    async def body(raw, pool):
        async with raw.connection() as conn:
            state = await _load_state(conn)
            with pytest.raises(RoleSetupError, match=cause):
                async with conn.transaction(force_rollback=True):
                    await conn.execute(statement)
                    await validate_application_contract(conn, state)
    _run(body)
