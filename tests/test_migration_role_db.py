"""Migrations refuse a role that forced row-level security would filter."""
from __future__ import annotations

import asyncio
import os
import traceback

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from app.db import MigrationRoleError, check_migration_role, make_pool, run_migrations
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

PLAIN = "odograph_test_plain_migrator"
BYPASS = "odograph_test_bypass_migrator"
PASSWORD = "disposable-migrator-password"


async def _with_roles(callback):
    admin = make_pool(TEST_DB)
    await admin.open(wait=True)
    try:
        await reset_account_db(admin)
        async with admin.connection() as conn:
            for role, attributes in ((PLAIN, "NOBYPASSRLS"), (BYPASS, "BYPASSRLS")):
                await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
                await conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER {} PASSWORD {}").format(
                    sql.Identifier(role), sql.SQL(attributes), sql.Literal(PASSWORD)))
                await conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(conn.info.dbname), sql.Identifier(role)))
                await conn.execute(sql.SQL("GRANT SELECT ON schema_migrations TO {}").format(
                    sql.Identifier(role)))
        await callback(admin)
    finally:
        async with admin.connection() as conn:
            for role in (PLAIN, BYPASS):
                await conn.execute(sql.SQL("REVOKE ALL ON schema_migrations FROM {}").format(sql.Identifier(role)))
                await conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                    sql.Identifier(conn.info.dbname), sql.Identifier(role)))
                await conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        await admin.close()


def _url(role):
    return make_conninfo(TEST_DB, user=role, password=PASSWORD)


def test_forced_rls_hides_owned_rows_from_the_plain_owning_role():
    async def check(admin):
        async with admin.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 1
            async with conn.transaction(force_rollback=True):
                await conn.execute("SET LOCAL ROLE odograph_migrate")
                assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 0
                with pytest.raises(MigrationRoleError):
                    await check_migration_role(conn)
    asyncio.run(_with_roles(check))


def test_run_migrations_refuses_a_plain_role_before_touching_the_schema():
    async def check(admin):
        async with admin.connection() as conn:
            before = await (await conn.execute("SELECT max(version),count(*) FROM schema_migrations")).fetchone()
        pool = make_pool(_url(PLAIN))
        await pool.open(wait=True)
        try:
            with pytest.raises(MigrationRoleError) as failure:
                await run_migrations(pool)
        finally:
            await pool.close()
        rendered = "".join(traceback.format_exception(failure.value))
        assert "superuser" in str(failure.value) and "BYPASSRLS" in str(failure.value)
        for secret in (PASSWORD, PLAIN, "127.0.0.1", "postgresql://"):
            assert secret not in rendered
        async with admin.connection() as conn:
            after = await (await conn.execute("SELECT max(version),count(*) FROM schema_migrations")).fetchone()
        assert after == before
    asyncio.run(_with_roles(check))


def test_bypassrls_and_superuser_roles_may_migrate():
    async def check(admin):
        async with await AsyncConnection.connect(_url(BYPASS)) as conn:
            await check_migration_role(conn)
        async with admin.connection() as conn:
            await check_migration_role(conn)
    asyncio.run(_with_roles(check))


def test_startup_refuses_a_plain_migration_role(monkeypatch):
    from app.config import Config
    from app.main import create_app
    monkeypatch.setenv("DATABASE_URL", _url(PLAIN))
    monkeypatch.setenv("SESSION_SECRET", "disposable-session-secret")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    for name in ("OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL", "SMTP_HOST",
                 "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    async def check(admin):
        app = create_app(Config.from_env())
        with pytest.raises(MigrationRoleError):
            async with app.router.lifespan_context(app):
                pass
    asyncio.run(_with_roles(check))
