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

import pytest

from psycopg import errors, sql

import app.db as db_module
from app import application_roles
from app.application_roles import (
    OWNED_TABLES, _load_state, finalize_application_restore, prepare_application_roles,
    validate_application_contract,
)
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.role_setup import RoleSetupError
from conftest import drop_and_recreate_schema, full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

PREPARED_SCHEMA = 26
ACTIVATED_SCHEMA = 28
PROVISIONED_SCHEMAS = pytest.mark.parametrize("provisioned_schema", [PREPARED_SCHEMA, ACTIVATED_SCHEMA])


async def _provision(pool, monkeypatch, schema):
    # Reproduce the role contract that existed before invitations and email
    # challenges were added.
    owned_tables = OWNED_TABLES
    with monkeypatch.context() as patch:
        control_tables = tuple(table for table in application_roles.CONTROL_TABLES
                               if table not in ("invitations", "oidc_attempts", "oidc_action_proofs"))
        future_functions = (application_roles.INVITATION_FUNCTIONS + application_roles.EMAIL_CHALLENGE_FUNCTIONS
                            + application_roles.PASSWORD_RESET_FUNCTIONS
                            + application_roles.OIDC_ATTEMPT_FUNCTIONS
                            + application_roles.OIDC_METHOD_FUNCTIONS)
        patch.setattr(application_roles, "OWNED_TABLES", owned_tables)
        patch.setattr(application_roles, "PROTECTED_TABLES", ())
        patch.setattr(application_roles, "CONTROL_TABLES", control_tables)
        patch.setattr(application_roles, "TABLES", owned_tables + control_tables + application_roles.REFERENCE_TABLES)
        patch.setattr(application_roles, "FUNCTIONS", {
            key: value for key, value in application_roles.FUNCTIONS.items()
            if key not in future_functions
        })
        patch.setattr(application_roles, "FUNCTION_FILES", application_roles.FUNCTION_FILES[:3])
        patch.setattr(application_roles, "INVITATION_FUNCTIONS", ())
        patch.setattr(application_roles, "EMAIL_CHALLENGE_FUNCTIONS", ())
        patch.setattr(application_roles, "PASSWORD_RESET_FUNCTIONS", ())
        patch.setattr(application_roles, "OIDC_ATTEMPT_FUNCTIONS", ())
        patch.setattr(application_roles, "OIDC_METHOD_FUNCTIONS", ())
        if schema < ACTIVATED_SCHEMA:
            patch.setattr(application_roles, "CONTRACT_VERSION", "ownership-prepared-v1")
        await prepare_application_roles(TEST_DB)
    if schema >= ACTIVATED_SCHEMA:
        return
    # Schema 26 had every account policy prepared but not yet enforced.
    async with pool.connection() as conn:
        for table in owned_tables:
            ident = sql.Identifier(table)
            await conn.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(ident))
            await conn.execute(sql.SQL("ALTER TABLE {} DISABLE ROW LEVEL SECURITY").format(ident))


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
        await full_schema_reset(pool)
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
            await full_schema_reset(pool)
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
            await full_schema_reset(pool)
            await pool.close()
    asyncio.run(run())
