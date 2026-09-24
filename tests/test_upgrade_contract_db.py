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

from psycopg import sql

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
    if schema >= ACTIVATED_SCHEMA:
        await prepare_application_roles(TEST_DB)
        return
    # Reproduce what the prepared release provisioned: its contract version,
    # with every account policy present but row-level security disabled.
    activated = application_roles.CONTRACT_VERSION
    application_roles.CONTRACT_VERSION = "ownership-prepared-v1"
    try:
        await prepare_application_roles(TEST_DB)
    finally:
        application_roles.CONTRACT_VERSION = activated
    async with pool.connection() as conn:
        for table in OWNED_TABLES:
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
