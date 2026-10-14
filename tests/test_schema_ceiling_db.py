"""Startup refuses a database migrated by a newer release than this image.

Set TEST_DATABASE_URL only to a throwaway Postgres/PostGIS instance. Each case
removes the schema_migrations rows and objects it adds, even on failure.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import traceback

import pytest

import app.main as main_module
from app.db import MIGRATIONS_DIR, SchemaTooNewError, make_pool, newest_migration_version, run_migrations
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

NEWEST = newest_migration_version(sorted(MIGRATIONS_DIR.glob("*.sql")))
MARKER_TABLE = "schema_ceiling_marker"


async def _state(conn):
    cur = await conn.execute("SELECT array_agg(version ORDER BY version) FROM schema_migrations")
    versions = (await cur.fetchone())[0]
    cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{MARKER_TABLE}",))
    return versions, (await cur.fetchone())[0]


async def _cleanup(conn):
    await conn.execute("DELETE FROM schema_migrations WHERE version > %s", (NEWEST,))
    await conn.execute(f"DROP TABLE IF EXISTS public.{MARKER_TABLE}")


def _scenario(action):
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await reset_db(pool)
            try:
                await action(pool)
            finally:
                async with pool.connection() as conn:
                    await _cleanup(conn)
        finally:
            await pool.close()
    asyncio.run(run())


def _migrations_with_extra(tmp_path, version):
    for path in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(path, tmp_path / path.name)
    (tmp_path / f"{version:03d}_ceiling_marker.sql").write_text(
        f"CREATE TABLE public.{MARKER_TABLE} (id int);")
    return tmp_path


def test_equal_schema_migrates_as_before():
    async def action(pool):
        async with pool.connection() as conn:
            before = await _state(conn)
        await run_migrations(pool)
        async with pool.connection() as conn:
            after = await _state(conn)
        assert before == after
        assert after[0][-1] == NEWEST
    _scenario(action)


def test_older_schema_applies_pending_migrations(tmp_path, monkeypatch):
    import app.db as db_module
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migrations_with_extra(tmp_path, NEWEST + 1))

    async def action(pool):
        await run_migrations(pool)
        async with pool.connection() as conn:
            versions, marker = await _state(conn)
        assert versions[-1] == NEWEST + 1
        assert marker
    _scenario(action)


def test_newer_schema_refuses_and_changes_nothing():
    async def action(pool):
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (NEWEST + 1,))
            before = await _state(conn)
        with pytest.raises(SchemaTooNewError) as failure:
            await run_migrations(pool)
        message = str(failure.value)
        assert f"database schema version {NEWEST + 1} is newer" in message
        assert f"newest migration {NEWEST}," in message
        assert "docs/upgrading.md, Rollback" in message
        rendered = "".join(traceback.format_exception(failure.value))
        for secret in ("postgresql://", "password"):
            assert secret not in rendered
        async with pool.connection() as conn:
            assert await _state(conn) == before
    _scenario(action)


def test_newer_schema_refuses_before_applying_a_pending_migration(tmp_path, monkeypatch):
    import app.db as db_module
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", _migrations_with_extra(tmp_path, NEWEST + 1))

    async def action(pool):
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (NEWEST + 2,))
            before = await _state(conn)
        with pytest.raises(SchemaTooNewError) as failure:
            await run_migrations(pool)
        assert (failure.value.stored, failure.value.newest) == (NEWEST + 2, NEWEST + 1)
        async with pool.connection() as conn:
            after = await _state(conn)
        assert after == before
        assert NEWEST + 1 not in after[0] and not after[1]
    _scenario(action)


def test_startup_refuses_a_newer_schema_before_the_role_contract(monkeypatch):
    from app.config import Config
    from app.main import create_app
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "disposable-session-secret")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    for name in ("OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL", "SMTP_HOST",
                 "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    def role_pools_reached(*args, **kwargs):
        raise AssertionError("startup reached the role contract")

    async def action(pool):
        # A newer release's new table would fail the role contract with an
        # unrelated relation-set error if startup reached it.
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (NEWEST + 1,))
            await conn.execute(f"CREATE TABLE public.{MARKER_TABLE} (id int)")
        app = create_app(Config.from_env())
        monkeypatch.setattr(main_module, "application_role_pools", role_pools_reached)
        with pytest.raises(SchemaTooNewError):
            async with app.router.lifespan_context(app):
                pass
    _scenario(action)
