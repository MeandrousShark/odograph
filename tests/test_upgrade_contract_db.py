"""Migrations after 026 must keep an upgraded installation's role contract.

Startup applies pending migrations, then prepare_application_roles. Only a
new installation or an explicit restore provisions grants and policies; an
upgrade only validates them. A fresh-database test therefore cannot catch a
later migration that adds a table without its own ownership, grants and
policies, because provisioning covers it there. These tests replay the
upgrade order instead: provision at an earlier schema, apply every later
migration, then prepare again without provisioning. Schema 26 and 27
installations were provisioned under the prepared contract, with row-level
security disabled; migration 028 must activate it.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from psycopg import errors, sql

import app.db as db_module
from app import application_roles
from app.application_roles import (
    _load_state, finalize_application_restore, prepare_application_roles,
    validate_application_contract,
)
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.role_setup import RoleSetupError
from app.account_context import CONTROL_ROLE, RUNTIME_ROLE
from app.role_setup import BOOTSTRAP_ROLE
from conftest import drop_and_recreate_schema

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS"),
              pytest.mark.usefixtures("restores_test_schema_after_module")]

PREPARED_SCHEMA = 26
ACTIVATED_SCHEMA = 28
PROVISIONED_SCHEMAS = pytest.mark.parametrize("provisioned_schema", [PREPARED_SCHEMA, ACTIVATED_SCHEMA])
HISTORICAL_SQL = Path(__file__).parent / "fixtures" / "schema26_roles"
OWNED_TABLES = tuple(table for table in application_roles.OWNED_TABLES
                    if table not in application_roles.GEOCODE_TABLES)


def _historical_table_rights(role, table):
    # Frozen schema-26 rights from source 25b6f9d, before bootstrap purge and
    # account update grants were narrowed by later migrations.
    if role == RUNTIME_ROLE:
        if table in OWNED_TABLES:
            return {"INSERT", "UPDATE", "DELETE"} | ({"SELECT"} if table != "ingest_credentials" else set())
        if table in application_roles.REFERENCE_TABLES:
            return {"SELECT"}
    if role == CONTROL_ROLE:
        if table == "accounts":
            return {"SELECT", "UPDATE"}
        if table == "oidc_identities":
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table in ("instance_state", "schema_migrations"):
            return {"SELECT"}
    if role == BOOTSTRAP_ROLE:
        rights = set()
        if table in application_roles.BOOTSTRAP_INSERT_TABLES:
            rights.add("INSERT")
        if table in application_roles.BOOTSTRAP_LOCK_TABLES or table == "instance_state":
            rights.update(("SELECT", "UPDATE"))
        if table == "reference_mileage_rates":
            rights.add("SELECT")
        return rights
    return set()


async def _provision(pool, monkeypatch, schema):
    # Reproduce schema-26/28 functions and grants, not today's provisioning.
    owned_tables = OWNED_TABLES
    with monkeypatch.context() as patch:
        control_tables = tuple(table for table in application_roles.CONTROL_TABLES
                               if table not in ("invitations", "oidc_attempts", "oidc_action_proofs",
                                                "account_security_audit"))
        future_functions = (application_roles.INVITATION_FUNCTIONS + application_roles.EMAIL_CHALLENGE_FUNCTIONS
                            + application_roles.PASSWORD_RESET_FUNCTIONS
                            + application_roles.OIDC_ATTEMPT_FUNCTIONS
                            + application_roles.OIDC_METHOD_FUNCTIONS
                            + application_roles.ACCOUNT_LIFECYCLE_FUNCTIONS
                            + application_roles.IMPORT_ADMISSION_FUNCTIONS
                            + application_roles.STORAGE_FUNCTIONS
                            + application_roles.GEOCODE_FUNCTIONS)
        patch.setattr(application_roles, "OWNED_TABLES", owned_tables)
        patch.setattr(application_roles, "PROTECTED_TABLES", ())
        patch.setattr(application_roles, "CONTROL_TABLES", control_tables)
        patch.setattr(application_roles, "TABLES", owned_tables + control_tables + application_roles.REFERENCE_TABLES)
        patch.setattr(application_roles, "FUNCTIONS", {
            key: value for key, value in application_roles.FUNCTIONS.items()
            if key not in future_functions
        })
        patch.setattr(application_roles, "FUNCTION_FILES", application_roles.FUNCTION_FILES[:3])
        patch.setattr(application_roles, "SQL_DIR", HISTORICAL_SQL)
        patch.setattr(application_roles, "_table_rights", _historical_table_rights)
        patch.setattr(application_roles, "INVITATION_FUNCTIONS", ())
        patch.setattr(application_roles, "EMAIL_CHALLENGE_FUNCTIONS", ())
        patch.setattr(application_roles, "PASSWORD_RESET_FUNCTIONS", ())
        patch.setattr(application_roles, "OIDC_ATTEMPT_FUNCTIONS", ())
        patch.setattr(application_roles, "OIDC_METHOD_FUNCTIONS", ())
        patch.setattr(application_roles, "ACCOUNT_LIFECYCLE_FUNCTIONS", ())
        if schema < ACTIVATED_SCHEMA:
            patch.setattr(application_roles, "CONTRACT_VERSION", "ownership-prepared-v1")
        await prepare_application_roles(TEST_DB)
    # The current provisioner adds column UPDATE grants that did not exist in
    # schema 26/28; remove them before replaying historical forward migrations.
    async with pool.connection() as conn:
        await conn.execute(
            "REVOKE UPDATE (updated_at,avatar_bytes,avatar_mime,avatar_updated_at) "
            "ON public.accounts FROM odograph_control")
    if schema >= ACTIVATED_SCHEMA:
        return
    # Schema 26 had every account policy prepared but not yet enforced.
    async with pool.connection() as conn:
        for table in owned_tables:
            ident = sql.Identifier(table)
            await conn.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(ident))
            await conn.execute(sql.SQL("ALTER TABLE {} DISABLE ROW LEVEL SECURITY").format(ident))


async def _prepare_before_039(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(application_roles, "OWNED_TABLES", OWNED_TABLES)
        patch.setattr(application_roles, "PROTECTED_TABLES", ("email_challenges",))
        patch.setattr(application_roles, "TABLES", tuple(
            table for table in application_roles.TABLES
            if table not in application_roles.STORAGE_TABLES + application_roles.GEOCODE_TABLES))
        patch.setattr(application_roles, "FUNCTIONS", {
            key: value for key, value in application_roles.FUNCTIONS.items()
            if key not in (application_roles.IMPORT_ADMISSION_FUNCTIONS + application_roles.STORAGE_FUNCTIONS
                           + application_roles.GEOCODE_FUNCTIONS)
        })
        await prepare_application_roles(TEST_DB)


async def _prepare_schema_40(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(application_roles, "OWNED_TABLES", OWNED_TABLES)
        patch.setattr(application_roles, "PROTECTED_TABLES", ("email_challenges",) + application_roles.STORAGE_TABLES)
        patch.setattr(application_roles, "TABLES", tuple(
            table for table in application_roles.TABLES if table not in application_roles.GEOCODE_TABLES))
        patch.setattr(application_roles, "FUNCTIONS", {
            key: value for key, value in application_roles.FUNCTIONS.items()
            if key not in application_roles.GEOCODE_FUNCTIONS
        })
        patch.setattr(application_roles, "FUNCTION_SPECS", tuple(
            replace(spec, source="040_storage_accounting.sql")
            if spec.signature == "public.storage_expected_usage()" else spec
            for spec in application_roles.FUNCTION_SPECS
        ))
        await prepare_application_roles(TEST_DB)


def _migration_dir(tmp_path, name, *, through=None, extra=None):
    target = tmp_path / name
    target.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if through is None or int(path.name.split("_", 1)[0]) <= through:
            shutil.copy(path, target / path.name)
    for filename, text in (extra or {}).items():
        (target / filename).write_text(text)
    return target


async def _upgrade_after_provisioning(monkeypatch, schema, provisioned_dir, upgrade_dir, after_upgrade):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await drop_and_recreate_schema(pool)
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", provisioned_dir)
        await run_migrations(pool)
        await _provision(pool, monkeypatch, schema)
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", upgrade_dir)
        await run_migrations(pool)
        await after_upgrade(pool)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
        await pool.close()


@PROVISIONED_SCHEMAS
def test_every_later_migration_keeps_the_upgraded_contract(monkeypatch, tmp_path, provisioned_schema):
    async def start_again(pool):
        await prepare_application_roles(TEST_DB)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class "
                "WHERE relnamespace='public'::regnamespace AND relname=ANY(%s)", (list(OWNED_TABLES),))
            assert await cur.fetchone() == (True,)

    provisioned = _migration_dir(tmp_path, "provisioned", through=provisioned_schema)
    asyncio.run(_upgrade_after_provisioning(
        monkeypatch, provisioned_schema, provisioned, MIGRATIONS_DIR, start_again))


def test_snap_attempt_upgrade_preserves_existing_trip_results(monkeypatch, tmp_path):
    async def scenario():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await drop_and_recreate_schema(pool)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(tmp_path, "before_038", through=37))
            await run_migrations(pool)
            await _prepare_before_039(monkeypatch)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO accounts(id,email,password_hash,is_admin) "
                    "VALUES(41,'upgrade@example.invalid','unused',true)")
                await conn.execute(
                    "INSERT INTO tracking_devices(account_id,label) VALUES(41,'phone')")
                device = (await (await conn.execute(
                    "SELECT id FROM tracking_devices WHERE account_id=41"
                )).fetchone())[0]
                for status in ("pending", "ok"):
                    await conn.execute(
                        "INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,"
                        "distance_m,snap_status,snapped_at) "
                        "VALUES(41,%s,'phone','detected','2026-07-01T10:00:00Z',"
                        "'2026-07-01T10:15:00Z',1000,%s,"
                        "CASE WHEN %s='ok' THEN '2026-07-01T11:00:00Z'::timestamptz ELSE NULL END)",
                        (device, status, status),
                    )
                before = await (await conn.execute(
                    "SELECT id,snap_status::text,snapped_at,created_at FROM trips ORDER BY id"
                )).fetchall()

            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(tmp_path, "through_038", through=38))
            await run_migrations(pool)
            await _prepare_before_039(monkeypatch)
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT max(version) FROM schema_migrations"
                )).fetchone()) == (38,)
                after = await (await conn.execute(
                    "SELECT id,snap_status::text,snapped_at,created_at,snap_attempted_at "
                    "FROM trips ORDER BY id"
                )).fetchall()
            assert [row[:4] for row in after] == before
            assert all(row[4] is None for row in after)
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await pool.close()
    asyncio.run(scenario())


def test_prepared_contract_without_activation_refuses_start_and_restore(monkeypatch, tmp_path):
    """A prepared-contract database, such as a restored schema-27 archive,
    names the contract mismatch instead of failing generically."""
    async def start_again(pool):
        with pytest.raises(RoleSetupError, match="security contract mismatch: security contract version$"):
            await prepare_application_roles(TEST_DB)
        with pytest.raises(RoleSetupError, match="security contract mismatch: security contract version$"):
            await finalize_application_restore(TEST_DB)

    provisioned = _migration_dir(tmp_path, "provisioned", through=PREPARED_SCHEMA)
    upgraded = _migration_dir(tmp_path, "upgraded", through=ACTIVATED_SCHEMA - 1)
    asyncio.run(_upgrade_after_provisioning(
        monkeypatch, PREPARED_SCHEMA, provisioned, upgraded, start_again))


@PROVISIONED_SCHEMAS
def test_table_added_without_its_contract_refuses_upgraded_start(monkeypatch, tmp_path, provisioned_schema):
    async def start_again(pool):
        # The probe table alone breaks the contract, and prepare_application_roles
        # now surfaces that specific cause instead of a generic failure.
        with pytest.raises(RoleSetupError, match="contract_probe"):
            await prepare_application_roles(TEST_DB)
        async with pool.connection() as conn:
            await conn.execute("DROP TABLE contract_probe")
            await validate_application_contract(conn, await _load_state(conn))

    provisioned = _migration_dir(tmp_path, "provisioned", through=provisioned_schema)
    upgraded = _migration_dir(
        tmp_path, "upgraded", extra={"999_contract_probe.sql": "CREATE TABLE contract_probe(id int);"})
    asyncio.run(_upgrade_after_provisioning(
        monkeypatch, provisioned_schema, provisioned, upgraded, start_again))


@pytest.mark.parametrize("omission,cause", [
    ("grant", "function privilege: odograph_control public.resend_member_invitation"),
    ("replacement", "function definition: public.bootstrap_first_account"),
])
def test_historical_upgrade_needs_forward_function_contract(monkeypatch, tmp_path, omission, cause):
    async def start_again(pool):
        with pytest.raises(RoleSetupError, match=cause):
            await prepare_application_roles(TEST_DB)

    if omission == "grant":
        filename = "034_admin_invitations.sql"
        original = (MIGRATIONS_DIR / filename).read_text()
        statement = "GRANT EXECUTE ON FUNCTION public.resend_member_invitation(bigint,bigint,bigint,text) TO odograph_control;"
        assert original.count(statement) == 1
        changed = original.replace(statement, "")
    else:
        filename = "037_account_deletion.sql"
        original = (MIGRATIONS_DIR / filename).read_text()
        start = original.index("CREATE OR REPLACE FUNCTION public.bootstrap_first_account(")
        end = original.index("REVOKE ALL ON FUNCTION public.bootstrap_first_account", start)
        changed = original[:start] + original[end:]
    provisioned = _migration_dir(tmp_path, "provisioned", through=PREPARED_SCHEMA)
    upgraded = _migration_dir(tmp_path, "upgraded", extra={filename: changed})
    asyncio.run(_upgrade_after_provisioning(monkeypatch, PREPARED_SCHEMA, provisioned, upgraded, start_again))


def test_failed_029_rolls_back_objects_and_grants(monkeypatch, tmp_path):
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await drop_and_recreate_schema(pool)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(tmp_path, "before_029", through=28))
            await run_migrations(pool)
            await _provision(pool, monkeypatch, ACTIVATED_SCHEMA)
            async with pool.connection() as conn:
                before_roles = await (await conn.execute(
                    "SELECT rolname,rolsuper,rolbypassrls,rolcanlogin FROM pg_roles "
                    "WHERE rolname LIKE 'odograph_%' ORDER BY rolname")).fetchall()
            failing_sql = (MIGRATIONS_DIR / "029_invitations.sql").read_text()
            failing_sql += "\nDO $$ BEGIN RAISE EXCEPTION 'forced 029 failure'; END $$;\n"
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(
                tmp_path, "failed_029", extra={"029_invitations.sql": failing_sql}))
            with pytest.raises(errors.RaiseException, match="forced 029 failure"):
                await run_migrations(pool)
            async with pool.connection() as conn:
                assert await (await conn.execute("SELECT max(version) FROM schema_migrations")).fetchone() == (28,)
                assert await (await conn.execute("SELECT to_regclass('public.invitations')")).fetchone() == (None,)
                assert await (await conn.execute(
                    "SELECT to_regprocedure('public.issue_member_invitation(bigint,text,text)'), "
                    "to_regprocedure('public.redeem_member_invitation(text,text,text)')")).fetchone() == (None, None)
                after_roles = await (await conn.execute(
                    "SELECT rolname,rolsuper,rolbypassrls,rolcanlogin FROM pg_roles "
                    "WHERE rolname LIKE 'odograph_%' ORDER BY rolname")).fetchall()
                assert after_roles == before_roles
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await pool.close()
    asyncio.run(run())


def test_032_converts_only_empty_passwords_and_rejects_new_empty_hashes(monkeypatch, tmp_path):
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await drop_and_recreate_schema(pool)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(tmp_path, "before_032", through=31))
            await run_migrations(pool)
            async with pool.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin) VALUES"
                    "('empty@example.invalid','',false),('valid@example.invalid','valid-hash',false)")
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await run_migrations(pool)
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT email,password_hash FROM accounts ORDER BY email")).fetchall()
                assert rows == [("empty@example.invalid", None),
                                ("valid@example.invalid", "valid-hash")]
                with pytest.raises(errors.CheckViolation):
                    await conn.execute(
                        "INSERT INTO accounts(email,password_hash,is_admin) "
                        "VALUES('new@example.invalid','',false)")
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await pool.close()
    asyncio.run(run())


def test_storage_upgrade_keeps_contract_without_reprovisioning(monkeypatch, tmp_path):
    """Upgrade an already managed installation and validate its migration grants."""
    async def scenario():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await drop_and_recreate_schema(pool)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(tmp_path, "before_storage", through=39))
            await run_migrations(pool)
            with monkeypatch.context() as patch:
                patch.setattr(application_roles, "OWNED_TABLES", OWNED_TABLES)
                patch.setattr(application_roles, "PROTECTED_TABLES", ("email_challenges",))
                patch.setattr(application_roles, "TABLES", tuple(
                    table for table in application_roles.TABLES
                    if table not in application_roles.STORAGE_TABLES + application_roles.GEOCODE_TABLES))
                patch.setattr(application_roles, "FUNCTIONS", {
                    key: value for key, value in application_roles.FUNCTIONS.items()
                    if key not in application_roles.STORAGE_FUNCTIONS + application_roles.GEOCODE_FUNCTIONS
                })
                await prepare_application_roles(TEST_DB)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO accounts(id,email,password_hash,is_admin,avatar_bytes,avatar_mime,avatar_updated_at) "
                    "VALUES(41,'upgrade-storage@example.invalid','unused',true,'avatar'::bytea,'image/png',now())")
                await conn.execute(
                    "INSERT INTO raw_messages(account_id,payload) VALUES(41,'{\"kind\": \"legacy\"}')")
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await run_migrations(pool)

            async def forbidden_reprovision(conn):
                raise AssertionError("upgrade must validate rather than provision")

            monkeypatch.setattr(application_roles, "_provision", forbidden_reprovision)
            await prepare_application_roles(TEST_DB)
            async with pool.connection() as conn:
                assert await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone() == (True,)
                assert await (await conn.execute(
                    "SELECT charge_version,actual_bytes,raw_bytes,reserved_bytes FROM account_usage WHERE account_id=41"
                )).fetchone() == (1, 128 + 6 + 9 + 128 + len('{"kind": "legacy"}') + 138,
                                  128 + len('{"kind": "legacy"}'), 0)
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await pool.close()
    asyncio.run(scenario())


def test_geocode_upgrade_keeps_contract_without_reprovisioning(monkeypatch, tmp_path):
    async def scenario():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await drop_and_recreate_schema(pool)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migration_dir(tmp_path, "before_geocode", through=40))
            await run_migrations(pool)
            await _prepare_schema_40(monkeypatch)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO accounts(id,email,password_hash,is_admin) "
                    "VALUES(41,'upgrade-geocode@example.invalid','unused',true)")
                await conn.execute(
                    "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,start_geom,end_geom) "
                    "VALUES(41,'manual','manual','2026-07-01','2026-07-01',100,"
                    "ST_SetSRID(ST_MakePoint(1,2),4326)::geography,"
                    "ST_SetSRID(ST_MakePoint(3,4),4326)::geography)")
                before = (await (await conn.execute(
                    "SELECT actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes "
                    "FROM account_usage WHERE account_id=41")).fetchone())
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await run_migrations(pool)

            async def forbidden_reprovision(conn):
                raise AssertionError("upgrade must validate rather than provision")

            monkeypatch.setattr(application_roles, "_provision", forbidden_reprovision)
            await prepare_application_roles(TEST_DB)
            async with application_roles.application_role_pools(TEST_DB) as roles:
                from app.account_context import AccountPool, AccountPrincipal
                bound = AccountPool(roles.runtime, AccountPrincipal(41, True, 1))
                async with bound.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT cursor_trip_id,generation,round_generation,scanned_generation FROM geocode_discovery"
                    )).fetchone() == (0, 1, 1, 0)
                    page = await (await conn.execute("SELECT * FROM public.geocode_discover_page(41)")).fetchone()
                    assert page[:2] == (1, 2)
                    assert await (await conn.execute(
                        "SELECT rounded_lat,rounded_lon FROM geocode_retry ORDER BY rounded_lat"
                    )).fetchall() == [(2, 1), (4, 3)]
            async with pool.connection() as conn:
                assert await (await conn.execute(
                    "SELECT actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes "
                    "FROM account_usage WHERE account_id=41"
                )).fetchone() == (before[0] + 393, *before[1:])
                assert await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone() == (True,)
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await pool.close()
    asyncio.run(scenario())
