"""Maintenance commands surface the schema ceiling message without a database."""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

import pytest

import app.application_roles as application_roles
import app.diagnose as diagnose
import app.manage_account as manage_account
import app.reprocess_detector as reprocess_detector
from app.db import SchemaTooNewError, check_schema_ceiling
from app.role_setup import RoleSetupError

MESSAGE = str(SchemaTooNewError(45, 44))


async def _too_new(*args, **kwargs):
    raise SchemaTooNewError(45, 44)


@asynccontextmanager
async def _too_new_pools(*args, **kwargs):
    raise SchemaTooNewError(45, 44)
    yield  # pragma: no cover


def test_check_schema_ceiling_allows_equal_and_older_and_refuses_newer():
    check_schema_ceiling(44, 44)
    check_schema_ceiling(0, 44)
    with pytest.raises(SchemaTooNewError) as failure:
        check_schema_ceiling(45, 44)
    assert str(failure.value) == MESSAGE


def test_check_schema_ceiling_defaults_to_the_shipped_migrations():
    from app.db import MIGRATIONS_DIR, newest_migration_version
    newest = newest_migration_version(MIGRATIONS_DIR.glob("*.sql"))
    check_schema_ceiling(newest)
    with pytest.raises(SchemaTooNewError, match=f"newest migration {newest},"):
        check_schema_ceiling(newest + 1)


class _FakeConnection:
    def __init__(self):
        self.statements = []

    @classmethod
    async def connect(cls, *args, **kwargs):
        return cls._instance

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self.statements.append(statement)


def test_prepare_application_roles_passes_the_refusal_through_unwrapped(monkeypatch):
    conn = _FakeConnection()
    _FakeConnection._instance = conn

    async def installed(_conn):
        return 10_000

    monkeypatch.setattr(application_roles, "_SafeConnection", _FakeConnection)
    monkeypatch.setattr(application_roles, "_installed_schema_version", installed)

    with pytest.raises(SchemaTooNewError) as failure:
        asyncio.run(application_roles.prepare_application_roles("postgresql://unused"))
    assert not isinstance(failure.value, RoleSetupError)
    # Only the advisory lock ran: no contract check and no write.
    assert len(conn.statements) == 1 and "pg_advisory_xact_lock" in conn.statements[0]


@pytest.mark.parametrize("command,target", [
    ("verify", "application_role_pools"),
    ("finalize-restore", "finalize_application_restore"),
])
def test_application_roles_command_prints_the_message(monkeypatch, capsys, command, target):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(sys, "argv", ["app.application_roles", command])
    monkeypatch.setattr(application_roles, target,
                        _too_new_pools if target == "application_role_pools" else _too_new)
    with pytest.raises(SystemExit) as exit_info:
        application_roles.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == MESSAGE + "\n"


def test_application_roles_command_keeps_role_setup_errors(monkeypatch, capsys):
    async def failing(*args, **kwargs):
        raise RoleSetupError("application database setup failed")

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(sys, "argv", ["app.application_roles", "finalize-restore"])
    monkeypatch.setattr(application_roles, "finalize_application_restore", failing)
    with pytest.raises(SystemExit) as exit_info:
        application_roles.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "application database setup failed\n"


@pytest.mark.parametrize("argv,target", [
    (["create-admin"], "_create_admin"),
    (["list-accounts"], "_list_accounts"),
    (["reset-password", "7"], "_reset_password"),
])
def test_manage_account_prints_the_message(monkeypatch, capsys, argv, target):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(manage_account, target, _too_new)
    assert manage_account.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.err == f"error: {MESSAGE}\n"
    assert captured.out == ""


def test_manage_account_keeps_other_failures_generic(monkeypatch, capsys):
    async def failing(*args, **kwargs):
        raise RuntimeError("postgresql://secret@host")

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(manage_account, "_list_accounts", failing)
    assert manage_account.main(["list-accounts"]) == 1
    assert capsys.readouterr().err == "error: account operation failed\n"


def test_reprocess_detector_prints_the_message(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(reprocess_detector, "_reprocess", _too_new)
    assert reprocess_detector.main(["--account", "1"]) == 1
    assert capsys.readouterr().err == f"error: {MESSAGE}\n"


def test_reprocess_detector_keeps_other_failures_to_the_type(monkeypatch, capsys):
    async def failing(*args, **kwargs):
        raise RuntimeError("postgresql://secret@host")

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(reprocess_detector, "_reprocess", failing)
    assert reprocess_detector.main(["--account", "1"]) == 1
    assert capsys.readouterr().err == "error: detector reprocess failed (RuntimeError)\n"


@pytest.mark.parametrize("error,finding", [
    (SchemaTooNewError(45, 44), True),
    (RuntimeError("postgresql://secret@host"), False),
])
def test_diagnose_reports_a_newer_schema_as_a_finding(monkeypatch, capsys, error, finding):
    @asynccontextmanager
    async def pools(*args, **kwargs):
        raise error
        yield  # pragma: no cover

    async def no_connectivity(*args, **kwargs):
        return []

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(diagnose, "application_role_pools", pools)
    monkeypatch.setattr(diagnose, "run_connectivity_checks", no_connectivity)
    asyncio.run(diagnose._main())
    captured = capsys.readouterr()
    assert f"database: FAILED ({type(error).__name__})" in captured.out
    assert captured.err == (f"error: {MESSAGE}\n" if finding else "")
    assert "secret" not in captured.out + captured.err
