"""Durable worker state retains account isolation and a pinned schema contract."""
from __future__ import annotations

import asyncio
import os

import pytest
from psycopg import errors

from app import application_roles
from app.account_context import AccountPool, AccountPrincipal
from app.application_roles import finalize_application_restore, prepare_application_roles, validate_application_contract
from app.role_setup import RoleSetupError
from tests.test_application_roles_db import _scenario
from tests.test_storage_roles_db import _seed

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


def test_retry_crud_and_discovery_helpers_are_scoped_without_counter_write_authority():
    async def check(owner, pools, state):
        account, bound = await _seed(owner, pools)
        other = AccountPool(pools.runtime, AccountPrincipal(73, True, 1))
        for principal, pool in ((account, bound), (73, other)):
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon) VALUES(%s,2,1)", (principal,))
        async with bound.connection() as conn:
            assert await (await conn.execute("SELECT account_id FROM geocode_retry")).fetchall() == [(account,)]
            assert await (await conn.execute("SELECT account_id FROM geocode_discovery")).fetchall() == [(account,)]
            assert (await conn.execute("UPDATE geocode_retry SET failure_count=1,failure_reason='transport'")).rowcount == 1
            assert (await conn.execute("UPDATE geocode_retry SET failure_count=2 WHERE account_id=73")).rowcount == 0
            assert (await conn.execute("DELETE FROM geocode_retry WHERE account_id=73")).rowcount == 0
            for statement in (
                "INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon) VALUES(73,3,1)",
                "INSERT INTO geocode_discovery(account_id) VALUES(73)",
                "UPDATE geocode_discovery SET generation=generation+1",
                "DELETE FROM geocode_discovery",
                "UPDATE account_usage SET actual_bytes=0",
                "SELECT public.geocode_discover_page(73)",
                "SELECT public.geocode_record_coordinate_turn(73)",
                "SELECT public.geocode_representative_source(73,2,1)",
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(statement)
            page = await (await conn.execute("SELECT * FROM public.geocode_discover_page(%s)", (account,))).fetchone()
            assert page[:2] == (0, 0)
            await conn.execute("SELECT public.geocode_record_coordinate_turn(%s)", (account,))
            assert await (await conn.execute("SELECT last_unit::text FROM geocode_discovery")).fetchone() == ("coordinate",)
            assert (await conn.execute("DELETE FROM geocode_retry")).rowcount == 1
            assert await (await conn.execute(
                "SELECT * FROM public.geocode_representative_source(%s,2,1)", (account,))).fetchall() == []
        async with pools.runtime.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM geocode_retry")).fetchone() == (0,)
            assert await (await conn.execute("SELECT count(*) FROM geocode_discovery")).fetchone() == (0,)
            for function in ("geocode_discover_page", "geocode_record_coordinate_turn"):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(f"SELECT public.{function}(%s)", (account,))
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT public.geocode_representative_source(%s,2,1)", (account,))
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT account_id,failure_count FROM geocode_retry")).fetchall() == [(73, 0)]
            assert await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone() == (True,)
            for signature in application_roles.GEOCODE_FUNCTIONS:
                for role in ("odograph_runtime", "odograph_control", "odograph_bootstrap"):
                    assert await (await conn.execute(
                        "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, signature),
                    )).fetchone() == (application_roles.FUNCTIONS[signature] == role,)
    asyncio.run(_scenario(check))


def test_representative_source_exposes_only_own_eligible_endpoint():
    async def check(owner, pools, state):
        account, bound = await _seed(owner, pools)
        other = AccountPool(pools.runtime, AccountPrincipal(73, True, 1))
        own_trip = None
        for principal, pool in ((account, bound), (73, other)):
            async with pool.connection() as conn:
                trip = (await (await conn.execute(
                    "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,start_geom) "
                    "VALUES(%s,'manual','manual','2026-07-01','2026-07-01',100,"
                    "ST_SetSRID(ST_MakePoint(1,2),4326)::geography) RETURNING id", (principal,)
                )).fetchone())[0]
                if principal == account:
                    own_trip = trip
                assert await (await conn.execute(
                    "SELECT * FROM public.geocode_representative_source(%s,2,1)", (principal,)
                )).fetchall() == [(trip, 1, None, None, None, "start")]
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute("SELECT public.geocode_representative_source(%s,2,1)",
                                           (73 if principal == account else account,))
        async with bound.connection() as conn:
            assert await (await conn.execute(
                "SELECT * FROM public.geocode_representative_source(%s,2,1)", (account,)
            )).fetchall() == [(own_trip, 1, None, None, None, "start")]
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("statement,cause", [
    ("ALTER TABLE geocode_retry DROP CONSTRAINT geocode_retry_failure_count_check", "geocode constraint"),
    ("ALTER TABLE geocode_retry DROP CONSTRAINT geocode_retry_rounded_lat_check", "geocode constraint"),
    ("ALTER TABLE geocode_retry DROP CONSTRAINT geocode_retry_account_id_fkey", "geocode constraint"),
    ("ALTER TABLE geocode_retry ALTER COLUMN next_attempt_at DROP NOT NULL", "geocode column"),
    ("ALTER TABLE geocode_retry ALTER COLUMN failure_reason TYPE text", "geocode column"),
    ("ALTER TABLE geocode_discovery ALTER COLUMN cursor_trip_id SET DEFAULT 500", "geocode column"),
    ("ALTER TABLE trips ALTER COLUMN geocode_generation SET DEFAULT 2", "geocode column"),
    ("ALTER TABLE tracking_devices DROP CONSTRAINT tracking_devices_geocode_generation_check", "geocode constraint"),
    ("ALTER TYPE geocode_failure_reason ADD VALUE 'unbounded'", "geocode enum"),
    ("ALTER TYPE geocode_work_unit ADD VALUE 'other'", "geocode enum"),
    ("DROP INDEX geocode_retry_due_idx", "geocode index set"),
    ("DROP INDEX trips_geocode_start_idx; CREATE INDEX trips_geocode_start_idx ON trips(account_id,id)", "geocode index definition"),
    ("ALTER TABLE trips DISABLE TRIGGER geocode_trip_generation", "storage trigger definition"),
    ("ALTER TABLE tracking_devices DISABLE TRIGGER geocode_device_generation", "storage trigger definition"),
    ("DROP TRIGGER geocode_intents_delete ON geocode_cache", "storage trigger set"),
    ("DROP TRIGGER geocode_intents_update ON trips; "
     "CREATE TRIGGER geocode_intents_update AFTER UPDATE ON trips "
     "REFERENCING OLD TABLE AS wrong_old NEW TABLE AS wrong_new "
     "FOR EACH STATEMENT EXECUTE FUNCTION public.geocode_endpoint_intents()", "storage trigger definition"),
    ("GRANT UPDATE ON geocode_discovery TO odograph_runtime", "table privilege"),
    ("GRANT EXECUTE ON FUNCTION public.geocode_discover_page(bigint) TO odograph_control", "function privilege"),
    ("CREATE OR REPLACE FUNCTION public.geocode_record_coordinate_turn(owner_id bigint) RETURNS void "
     "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp "
     "AS $body$ BEGIN RETURN; END $body$", "function definition"),
])
def test_durable_worker_schema_drift_refuses_startup(statement, cause):
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            with pytest.raises(RoleSetupError, match=cause):
                async with conn.transaction(force_rollback=True):
                    await conn.execute(statement)
                    await validate_application_contract(conn, state)
        await prepare_application_roles(TEST_DB)
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("statement,cause", [
    ("ALTER TABLE geocode_discovery DROP CONSTRAINT geocode_discovery_generation_check", "geocode constraint"),
    ("DROP INDEX geocode_retry_due_idx", "geocode index set"),
    ("CREATE OR REPLACE FUNCTION public.geocode_record_coordinate_turn(owner_id bigint) RETURNS void "
     "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp "
     "AS $body$ BEGIN RETURN; END $body$", "function definition"),
])
# The drift is committed, so the schema is replayed after.
@pytest.mark.usefixtures("restores_test_schema")
def test_worker_contract_drift_is_not_repaired_by_startup_or_restore(statement, cause):
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            await conn.execute(statement)
        with pytest.raises(RoleSetupError, match=cause):
            await prepare_application_roles(TEST_DB)
        with pytest.raises(RoleSetupError, match=cause):
            await finalize_application_restore(TEST_DB)
    asyncio.run(_scenario(check))


def test_explicit_restore_preserves_retry_progress_and_reconciles_operational_charges():
    async def check(owner, pools, state):
        account, bound = await _seed(owner, pools)
        async with bound.connection() as conn:
            await conn.execute(
                "INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon,attempted_at,next_attempt_at,"
                "failure_count,failure_reason) VALUES(%s,2,1,'2026-07-01','2026-07-01T01:00Z',4,'transport')",
                (account,))
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE geocode_discovery SET cursor_trip_id=500,generation=4,round_generation=3,"
                "scanned_generation=2,last_unit='discovery' WHERE account_id=%s", (account,))
            retries = await (await conn.execute("SELECT * FROM geocode_retry ORDER BY account_id")).fetchall()
            discovery = await (await conn.execute("SELECT * FROM geocode_discovery ORDER BY account_id")).fetchall()
            charges = await (await conn.execute("SELECT * FROM account_usage ORDER BY account_id")).fetchall()
            await conn.execute("UPDATE account_usage SET actual_bytes=actual_bytes+1 WHERE account_id=%s", (account,))
        with pytest.raises(RoleSetupError, match="storage accounting data"):
            await prepare_application_roles(TEST_DB)
        await finalize_application_restore(TEST_DB)
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT * FROM geocode_retry ORDER BY account_id")).fetchall() == retries
            assert await (await conn.execute("SELECT * FROM geocode_discovery ORDER BY account_id")).fetchall() == discovery
            assert await (await conn.execute("SELECT * FROM account_usage ORDER BY account_id")).fetchall() == charges
            assert await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone() == (True,)
    asyncio.run(_scenario(check))
