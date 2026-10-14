"""Startup and maintenance commands refuse a database migrated by a newer release.

Set TEST_DATABASE_URL only to a throwaway Postgres/PostGIS instance. Each case
removes the schema_migrations rows and objects it adds, even on failure.
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import app.application_roles as application_roles
import app.main as main_module
import app.manage_account as manage_account
import app.reprocess_detector as reprocess_detector
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


REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGE = str(SchemaTooNewError(NEWEST + 1, NEWEST))


async def _snapshot(conn):
    """Everything a refused maintenance command could have changed."""
    cur = await conn.execute(
        "SELECT (SELECT array_agg(version ORDER BY version) FROM schema_migrations),"
        "(SELECT md5(string_agg(rolname || COALESCE(rolpassword, ''), ',' ORDER BY rolname)) "
        " FROM pg_authid WHERE rolname LIKE 'odograph%%'),"
        "(SELECT md5(row(m.*)::text) FROM odograph_service.managed_role_state m),"
        "(SELECT md5(COALESCE(string_agg(row(a.*)::text, ',' ORDER BY a.id), '')) FROM accounts a),"
        "(SELECT md5(COALESCE(string_agg(row(t.*)::text, ',' ORDER BY t.id), '')) FROM trips t)")
    return await cur.fetchone()


def _refused_cli(run):
    """Mark the database newer than this image, run a command, prove no change."""
    async def action(pool):
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (NEWEST + 1,))
            before = await _snapshot(conn)
        await asyncio.to_thread(run)
        async with pool.connection() as conn:
            assert await _snapshot(conn) == before
    _scenario(action)


def _command_env():
    env = {**os.environ, "DATABASE_URL": TEST_DB, "SESSION_SECRET": "test-session-secret"}
    for key in ("OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL", "NTFY_TOPIC", "SMTP_HOST"):
        env.pop(key, None)
    return env


def test_role_verify_command_refuses_a_newer_schema():
    def run():
        result = subprocess.run(
            [sys.executable, "-m", "app.application_roles", "verify"],
            cwd=REPO_ROOT, env=_command_env(), capture_output=True, text=True, timeout=60)
        assert result.returncode == 1
        assert result.stderr == MESSAGE + "\n"
    _refused_cli(run)


def test_finalize_restore_refuses_a_newer_schema_before_role_work(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setattr(sys, "argv", ["app.application_roles", "finalize-restore"])

    def run():
        with pytest.raises(SystemExit) as exit_info:
            application_roles.main()
        assert exit_info.value.code == 1
        assert capsys.readouterr().err == MESSAGE + "\n"
    _refused_cli(run)


def test_prepare_application_roles_refuses_a_newer_schema_with_a_new_table():
    async def action(pool):
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (NEWEST + 1,))
            await conn.execute(f"CREATE TABLE public.{MARKER_TABLE} (id int)")
        with pytest.raises(SchemaTooNewError):
            await application_roles.prepare_application_roles(TEST_DB)
        with pytest.raises(SchemaTooNewError):
            await application_roles.finalize_application_restore(TEST_DB)
    _scenario(action)


@pytest.mark.parametrize("argv,stdin", [
    (["list-accounts"], ""),
    (["create-admin"], "admin@example.com\nfirst strong password\nfirst strong password\n"),
    (["reset-password", "1"], "another strong password\nanother strong password\n"),
])
def test_manage_account_refuses_a_newer_schema(monkeypatch, capsys, argv, stdin):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    def run():
        assert manage_account.main(argv) == 1
        captured = capsys.readouterr()
        assert captured.err.endswith(f"error: {MESSAGE}\n")
        assert "ID\tLOGIN EMAIL" not in captured.out
    _refused_cli(run)


def test_reprocess_detector_refuses_a_newer_schema(monkeypatch, capsys):
    for key, value in _command_env().items():
        monkeypatch.setenv(key, value)
    for key in ("OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL", "NTFY_TOPIC", "SMTP_HOST"):
        monkeypatch.delenv(key, raising=False)

    def run():
        assert reprocess_detector.main(["--account", "1"]) == 1
        assert capsys.readouterr().err == f"error: {MESSAGE}\n"
    _refused_cli(run)


def test_diagnose_reports_a_newer_schema_as_a_finding():
    def run():
        result = subprocess.run(
            [sys.executable, "-m", "app.diagnose"],
            cwd=REPO_ROOT, env=_command_env(), capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert "database: FAILED (SchemaTooNewError)" in result.stdout
        assert result.stderr.endswith(f"error: {MESSAGE}\n")
    _refused_cli(run)
