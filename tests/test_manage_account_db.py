from __future__ import annotations

import asyncio
import io
import os

import pytest

from app.db import make_pool, run_migrations
from app.local_auth import verify_password
from app.manage_account import main

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)


async def _reset_schema():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)
    finally:
        await pool.close()


async def _account_row():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT email, password_hash, auth_version FROM accounts"
            )
            return await cur.fetchone()
    finally:
        await pool.close()


def test_operator_command_creates_then_resets_admin_and_revokes_sessions(
    monkeypatch, capsys
):
    asyncio.run(_reset_schema())
    monkeypatch.setenv("DATABASE_URL", TEST_DB)

    first_password = "first strong password"
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(f"admin@example.com\n{first_password}\n{first_password}\n"),
    )
    assert main(["create-admin"]) == 0
    created = asyncio.run(_account_row())
    assert created[0] == "admin@example.com"
    assert verify_password(first_password, created[1])
    assert created[2] == 1

    second_password = "second strong password"
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"{second_password}\n{second_password}\n")
    )
    assert main(["reset-password"]) == 0
    reset = asyncio.run(_account_row())
    assert verify_password(second_password, reset[1])
    assert reset[2] == 2

    output = capsys.readouterr()
    combined = output.out + output.err
    assert first_password not in combined
    assert second_password not in combined
    assert reset[1] not in combined
    assert TEST_DB not in combined


def test_operator_command_refuses_ambiguous_operations(monkeypatch):
    asyncio.run(_reset_schema())
    monkeypatch.setenv("DATABASE_URL", TEST_DB)

    monkeypatch.setattr(
        "sys.stdin", io.StringIO("password one\npassword one\n")
    )
    assert main(["reset-password"]) == 1

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("admin@example.com\npassword one\npassword one\n"),
    )
    assert main(["create-admin"]) == 0

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("other@example.com\npassword two\npassword two\n"),
    )
    assert main(["create-admin"]) == 1


def test_operator_command_has_no_password_argument(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["reset-password", "--password", "visible-secret"])
    assert exc_info.value.code != 0
    output = capsys.readouterr()
    assert "visible-secret" not in output.out + output.err


def test_operator_command_redacts_unexpected_database_failures(monkeypatch, capsys):
    database_url = "postgresql://user:database-secret@example.invalid/database"
    monkeypatch.setenv("DATABASE_URL", database_url)

    async def fail(_database_url):
        raise RuntimeError(database_url)

    monkeypatch.setattr("app.manage_account._reset_password", fail)
    assert main(["reset-password"]) == 1
    output = capsys.readouterr()
    assert database_url not in output.out + output.err
    assert "database-secret" not in output.out + output.err
