"""Migrations after 026 must keep an upgraded installation's role contract.

Startup applies pending migrations, then prepare_application_roles. Only a
new installation or an explicit restore provisions grants and policies; an
upgrade only validates them. A fresh-database test therefore cannot catch a
later migration that adds a table without its own ownership, grants and
policies, because provisioning covers it there. These tests replay the
upgrade order instead: provision at schema 26, apply every later migration,
then prepare again without provisioning.
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest

import app.db as db_module
from app.application_roles import prepare_application_roles
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.role_setup import RoleSetupError
from conftest import drop_and_recreate_schema, full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

PROVISIONED_SCHEMA = 26


def _migration_dir(tmp_path, name, *, through=None, extra=None):
    target = tmp_path / name
    target.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if through is None or int(path.name.split("_", 1)[0]) <= through:
            shutil.copy(path, target / path.name)
    for filename, text in (extra or {}).items():
        (target / filename).write_text(text)
    return target


async def _upgrade_after_provisioning(monkeypatch, provisioned_dir, upgrade_dir):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await drop_and_recreate_schema(pool)
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", provisioned_dir)
        await run_migrations(pool)
        await prepare_application_roles(TEST_DB)
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", upgrade_dir)
        await run_migrations(pool)
        await prepare_application_roles(TEST_DB)
    finally:
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
        await full_schema_reset(pool)
        await pool.close()


def test_every_later_migration_keeps_the_upgraded_contract(monkeypatch, tmp_path):
    provisioned = _migration_dir(tmp_path, "provisioned", through=PROVISIONED_SCHEMA)
    asyncio.run(_upgrade_after_provisioning(monkeypatch, provisioned, MIGRATIONS_DIR))


def test_table_added_without_its_contract_refuses_upgraded_start(monkeypatch, tmp_path):
    provisioned = _migration_dir(tmp_path, "provisioned", through=PROVISIONED_SCHEMA)
    upgraded = _migration_dir(
        tmp_path, "upgraded", extra={"999_contract_probe.sql": "CREATE TABLE contract_probe(id int);"})
    with pytest.raises(RoleSetupError):
        asyncio.run(_upgrade_after_provisioning(monkeypatch, provisioned, upgraded))
