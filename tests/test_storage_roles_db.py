"""Protected logical accounting privileges, drift refusal and restore repair."""
from __future__ import annotations

import asyncio
import os

import pytest
from psycopg import errors, sql

from app import application_roles
from app.account_context import AccountPool, AccountPrincipal
from app.accounts import create_admin
from app.application_roles import (
    MIGRATE_ROLE, OWNED_TABLES, STORAGE_FUNCTIONS, STORAGE_TABLES,
    application_role_pools, finalize_application_restore, prepare_application_roles,
    validate_application_contract,
)
from app.role_setup import RoleSetupError
from app.tracking import create_device
from tests.test_application_roles_db import _scenario

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


async def _seed(owner, pools):
    async with pools.control.connection() as conn:
        first = await create_admin(conn, "storage@example.invalid", "unused-test-hash")
    async with owner.connection() as conn:
        await conn.execute("DROP INDEX accounts_singleton_idx")
        await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) "
                           "VALUES(73,'other@example.invalid','unused-test-hash',true)")
    bound = AccountPool(pools.runtime, AccountPrincipal(first["id"], True, 1))
    async with bound.connection() as conn:
        await create_device(conn, "phone")
    return first["id"], bound


def test_protected_usage_is_account_scoped_and_has_no_runtime_mutation_api():
    async def check(owner, pools, state):
        account, bound = await _seed(owner, pools)
        assert not set(STORAGE_TABLES) & set(OWNED_TABLES)
        async with bound.connection() as conn:
            for table in STORAGE_TABLES:
                rows = await (await conn.execute(sql.SQL(
                    "SELECT DISTINCT account_id FROM {}"
                ).format(sql.Identifier(table)))).fetchall()
                assert rows == [(account,)]
                for statement in (
                    sql.SQL("INSERT INTO {} SELECT * FROM {}").format(
                        sql.Identifier(table), sql.Identifier(table)),
                    sql.SQL("UPDATE {} SET account_id=account_id").format(sql.Identifier(table)),
                    sql.SQL("DELETE FROM {}").format(sql.Identifier(table)),
                    sql.SQL("TRUNCATE {}").format(sql.Identifier(table)),
                ):
                    with pytest.raises(errors.InsufficientPrivilege):
                        async with conn.transaction():
                            await conn.execute(statement)
            for function in ("reconcile_storage_usage", "storage_usage_consistent"):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(sql.SQL("SELECT public.{}()").format(sql.Identifier(function)))
        for pool in (pools.control, pools.runtime):
            async with pool.connection() as conn:
                for function in STORAGE_FUNCTIONS:
                    assert await (await conn.execute(
                        "SELECT has_function_privilege(current_user,%s,'EXECUTE')", (function,)
                    )).fetchone() == (False,)
                if pool is pools.runtime:
                    for table in STORAGE_TABLES:
                        assert await (await conn.execute(sql.SQL(
                            "SELECT count(*) FROM {}"
                        ).format(sql.Identifier(table)))).fetchone() == (0,)
                else:
                    for table in STORAGE_TABLES:
                        with pytest.raises(errors.InsufficientPrivilege):
                            async with conn.transaction():
                                await conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
    asyncio.run(_scenario(check))


def test_managed_definer_owner_can_write_all_charged_rows_under_forced_rls():
    async def check(owner, pools, state):
        account, _ = await _seed(owner, pools)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname=%s", (MIGRATE_ROLE,)
            )).fetchone() == (False,)
            for table in OWNED_TABLES + application_roles.PROTECTED_TABLES:
                assert await (await conn.execute(
                    "SELECT roles,cmd,qual,with_check FROM pg_policies "
                    "WHERE schemaname='public' AND tablename=%s AND policyname='migration_writer'", (table,)
                )).fetchone() == ([MIGRATE_ROLE], "ALL", "true", "true")
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE odograph_migrate")
                assert await (await conn.execute("SELECT count(*) FROM account_usage")).fetchone() == (2,)
                await conn.execute("UPDATE account_usage SET actual_bytes=actual_bytes WHERE account_id=%s", (account,))
                await conn.execute("SELECT public.storage_usage_consistent()")
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("statement,cause", [
    ("GRANT UPDATE ON public.account_usage TO odograph_runtime", "table privilege"),
    ("ALTER POLICY account_isolation ON public.account_usage USING (true)", "policy set"),
    ("GRANT EXECUTE ON FUNCTION public.reconcile_storage_usage() TO odograph_control", "function privilege"),
    ("ALTER FUNCTION public.storage_apply_statement() OWNER TO odograph_bootstrap", "public.storage_apply_statement"),
])
def test_storage_catalog_drift_refuses_serving(statement, cause):
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            with pytest.raises(RoleSetupError, match=cause):
                async with conn.transaction(force_rollback=True):
                    await conn.execute(statement)
                    await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


def test_counter_drift_refuses_startup_and_explicit_restore_reconciles():
    async def check(owner, pools, state):
        account, _ = await _seed(owner, pools)
        async with owner.connection() as conn:
            before = await (await conn.execute(
                "SELECT * FROM account_usage ORDER BY account_id"
            )).fetchall()
            await conn.execute("UPDATE account_usage SET actual_bytes=actual_bytes+1 WHERE account_id=%s", (account,))
        with pytest.raises(RoleSetupError, match="storage accounting data"):
            await prepare_application_roles(TEST_DB)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT * FROM account_usage ORDER BY account_id"
            )).fetchall() != before
        await finalize_application_restore(TEST_DB)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT * FROM account_usage ORDER BY account_id"
            )).fetchall() == before
            await conn.execute("SELECT public.storage_usage_consistent()")
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("statement,cause", [
    ("ALTER TABLE public.points DISABLE TRIGGER storage_charge_insert", "storage trigger definition"),
    ("ALTER TABLE public.points ENABLE ALWAYS TRIGGER storage_charge_insert", "storage trigger definition"),
    ("DROP TRIGGER storage_charge_insert ON public.points", "storage trigger set"),
    ("DROP TRIGGER storage_avatar_change ON public.accounts;"
     "CREATE TRIGGER storage_avatar_change AFTER UPDATE ON public.accounts "
     "FOR EACH ROW EXECUTE FUNCTION public.storage_avatar_change()", "storage trigger definition"),
    ("CREATE OR REPLACE FUNCTION public.storage_apply_statement() RETURNS trigger "
     "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp "
     "AS $body$ BEGIN RETURN NULL; END $body$", "function definition"),
    ("ALTER FUNCTION public.storage_charge_points(public.points) VOLATILE", "function definition"),
    ("DROP TRIGGER storage_charge_update ON public.points;"
     "CREATE TRIGGER storage_charge_update AFTER UPDATE ON public.points "
     "REFERENCING OLD TABLE AS wrong_old NEW TABLE AS wrong_new "
     "FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement()", "storage trigger definition"),
    ("DROP TRIGGER storage_charge_delete ON public.points;"
     "CREATE TRIGGER storage_charge_delete AFTER DELETE ON public.points "
     "FOR EACH STATEMENT EXECUTE FUNCTION public.storage_apply_statement()", "storage trigger definition"),
])
# The tampering is committed, so the schema is replayed after.
@pytest.mark.usefixtures("restores_test_schema")
def test_accounting_trigger_or_charge_definition_tampering_refuses_startup(statement, cause):
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            await conn.execute(statement)
        with pytest.raises(RoleSetupError, match=cause):
            await prepare_application_roles(TEST_DB)
        with pytest.raises(RoleSetupError, match=cause):
            await finalize_application_restore(TEST_DB)
    asyncio.run(_scenario(check))


def test_real_account_purge_cascades_protected_usage_and_envelopes():
    from app.account_lifecycle import purge_account, request_account_deletion

    async def check(owner, pools, state):
        account, _ = await _seed(owner, pools)
        target = AccountPool(pools.runtime, AccountPrincipal(73, True, 1))
        async with target.connection() as conn:
            await create_device(conn, "phone")
            stream = (await (await conn.execute(
                "SELECT id FROM tracking_devices WHERE account_id=73"
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom) "
                "SELECT 73,%s,'phone','2026-07-01'::timestamptz+n*interval '1 minute',"
                "ST_SetSRID(ST_MakePoint(1,2),4326)::geography FROM generate_series(0,2) n",
                (stream,),
            )
            await conn.execute(
                "INSERT INTO stays(account_id,tracking_device_id,device,started_at,ended_at,centroid,point_count) "
                "VALUES(73,%s,'phone','2026-07-01','2026-07-01T00:01Z',"
                "ST_SetSRID(ST_MakePoint(1,2),4326)::geography,1)", (stream,),
            )
            await conn.execute(
                "INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,distance_m,path) "
                "VALUES(73,%s,'phone','detected','2026-07-01T00:01Z','2026-07-01T00:02Z',100,"
                "ST_GeomFromText('LINESTRING(1 2,2 3)',4326))", (stream,),
            )
        async with target.connection() as conn:
            await conn.execute(
                "INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon,attempted_at,"
                "next_attempt_at,failure_count,failure_reason) "
                "VALUES(73,2,1,now(),now()+interval '1 hour',3,'transport')")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE geocode_discovery SET cursor_trip_id=4,generation=3,round_generation=2 "
                "WHERE account_id=73")
            assert await (await conn.execute(
                "SELECT count(*) FROM geocode_discovery WHERE account_id=73")).fetchone() == (1,)
            preserved = await (await conn.execute(
                "SELECT * FROM account_usage WHERE account_id=%s", (account,)
            )).fetchone()
            assert (await (await conn.execute(
                "SELECT reserved_bytes FROM account_usage WHERE account_id=73"
            )).fetchone())[0] > 0
        admin = {"id": account, "is_admin": True, "is_enabled": True, "auth_version": 1}
        async with pools.control.connection() as conn:
            assert await request_account_deletion(
                conn, admin, 73, email="other@example.invalid", acknowledge=True
            ) == "scheduled"
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET deletion_deadline=now()-interval '1 second' WHERE id=73")
        async with pools.control.connection() as conn:
            assert await purge_account(
                conn, admin, 73, email="other@example.invalid", confirm=True,
                verified_password_hash="unused-test-hash",
            ) == "purged"
        async with owner.connection() as conn:
            for table in STORAGE_TABLES + OWNED_TABLES + application_roles.GEOCODE_PROTECTED_TABLES:
                assert await (await conn.execute(sql.SQL(
                    "SELECT count(*) FROM {} WHERE account_id=73"
                ).format(sql.Identifier(table)))).fetchone() == (0,)
            assert await (await conn.execute(
                "SELECT * FROM account_usage WHERE account_id=%s", (account,)
            )).fetchone() == preserved
            assert await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone() == (True,)
    asyncio.run(_scenario(check))
