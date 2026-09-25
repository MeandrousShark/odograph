from __future__ import annotations

import asyncio
import io
import os
from contextlib import asynccontextmanager

import pytest
from psycopg import errors

import app.manage_account as manage_account
from app.db import make_pool
from app.local_auth import verify_password
from app.manage_account import main
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)


async def _reset_schema():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
    finally:
        await pool.close()


async def _account_rows():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, email, password_hash, auth_version, is_enabled, "
                "email_verified_at IS NOT NULL FROM accounts ORDER BY id"
            )
            return {row[0]: row[1:] for row in await cur.fetchall()}
    finally:
        await pool.close()


async def _owner(sql, params=()):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute(sql, params)
    finally:
        await pool.close()


def _create_admin(monkeypatch, password="first strong password"):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"admin@example.com\n{password}\n{password}\n"),
    )
    assert main(["create-admin"]) == 0
    (account_id,) = asyncio.run(_account_rows())
    return account_id


def test_operator_command_creates_then_resets_admin_and_revokes_sessions(
    monkeypatch, capsys
):
    asyncio.run(_reset_schema())
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    original_role_pools = manage_account.application_role_pools

    @asynccontextmanager
    async def verify_restricted_path(database_url):
        async with original_role_pools(database_url) as pools:
            async with pools.control.connection() as conn:
                assert (await (await conn.execute("SELECT session_user")).fetchone())[0] == "odograph_control"
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute("SELECT * FROM trips")
            yield pools

    monkeypatch.setattr(manage_account, "application_role_pools", verify_restricted_path)

    first_password = "first strong password"
    account_id = _create_admin(monkeypatch, first_password)
    created = asyncio.run(_account_rows())[account_id]
    assert created[0] == "admin@example.com"
    assert verify_password(first_password, created[1])
    assert created[2] == 1

    # No SMTP and an unverified address: host recovery still restores login.
    second_password = "second strong password"
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"yes\n{second_password}\n{second_password}\n")
    )
    assert main(["reset-password", str(account_id)]) == 0
    reset = asyncio.run(_account_rows())[account_id]
    assert verify_password(second_password, reset[1])
    assert not verify_password(first_password, reset[1])
    assert reset[2] == 2
    assert reset[4] is False

    output = capsys.readouterr()
    combined = output.out + output.err
    assert "admin@example.com" in output.out
    assert first_password not in combined
    assert second_password not in combined
    assert reset[1] not in combined
    assert TEST_DB not in combined


def _two_accounts(monkeypatch):
    """Test-only second account with a lower ID, so it sorts first."""
    asyncio.run(_reset_schema())
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    asyncio.run(_owner("SELECT setval(pg_get_serial_sequence('accounts','id'), 5, false)"))
    target = _create_admin(monkeypatch)
    asyncio.run(_owner("DROP INDEX accounts_singleton_idx"))
    asyncio.run(_owner("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check"))
    asyncio.run(_owner(
        "INSERT INTO accounts (id,email,password_hash,is_admin,email_verified_at) "
        "VALUES (2,'member@example.com','member-hash',false,now())"))
    return target, 2


def test_reset_changes_only_the_explicit_target(monkeypatch, capsys):
    target, other = _two_accounts(monkeypatch)
    try:
        before = asyncio.run(_account_rows())
        monkeypatch.setattr("sys.stdin", io.StringIO("yes\nreplacement pw\nreplacement pw\n"))
        assert main(["reset-password", str(target)]) == 0
        after = asyncio.run(_account_rows())
        assert after[other] == before[other]
        assert verify_password("replacement pw", after[target][1])
        assert after[target][2] == before[target][2] + 1

        capsys.readouterr()
        assert main(["list-accounts"]) == 0
        output = capsys.readouterr().out
        assert f"{other}\tmember@example.com\tmember\tenabled\tverified" in output
        assert f"{target}\tadmin@example.com\tadmin\tenabled\tunverified" in output
        assert "member-hash" not in output and after[target][1] not in output
    finally:
        asyncio.run(_reset_schema())


def test_reset_refuses_missing_nonexistent_disabled_and_declined_targets(monkeypatch, capsys):
    target, other = _two_accounts(monkeypatch)
    try:
        before = asyncio.run(_account_rows())
        with pytest.raises(SystemExit) as exc_info:
            main(["reset-password"])
        assert exc_info.value.code == 2
        for argument in ("0", "-1", "abc", "1e3", "99999999999999999999"):
            with pytest.raises(SystemExit):
                main(["reset-password", argument])

        monkeypatch.setattr("sys.stdin", io.StringIO("yes\nsome password\nsome password\n"))
        assert main(["reset-password", "999"]) == 1

        monkeypatch.setattr("sys.stdin", io.StringIO("no\nsome password\nsome password\n"))
        assert main(["reset-password", str(target)]) == 1

        asyncio.run(_owner("UPDATE accounts SET is_enabled=false WHERE id=%s", (other,)))
        monkeypatch.setattr("sys.stdin", io.StringIO("yes\nsome password\nsome password\n"))
        assert main(["reset-password", str(other)]) == 1
        after = asyncio.run(_account_rows())
        assert after[target] == before[target]
        assert after[other][:3] == before[other][:3] and after[other][3] is False
        assert "some password" not in "".join(capsys.readouterr())
    finally:
        asyncio.run(_reset_schema())


def test_operator_command_refuses_ambiguous_operations(monkeypatch):
    asyncio.run(_reset_schema())
    monkeypatch.setenv("DATABASE_URL", TEST_DB)

    monkeypatch.setattr(
        "sys.stdin", io.StringIO("yes\npassword one\npassword one\n")
    )
    assert main(["reset-password", "1"]) == 1

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
        main(["reset-password", "1", "--password", "visible-secret"])
    assert exc_info.value.code != 0
    output = capsys.readouterr()
    assert "visible-secret" not in output.out + output.err


def test_operator_command_redacts_unexpected_database_failures(monkeypatch, capsys):
    database_url = "postgresql://user:database-secret@example.invalid/database"
    monkeypatch.setenv("DATABASE_URL", database_url)

    async def fail(_database_url, _account_id):
        raise RuntimeError(database_url)

    monkeypatch.setattr("app.manage_account._reset_password", fail)
    assert main(["reset-password", "1"]) == 1
    output = capsys.readouterr()
    assert database_url not in output.out + output.err
    assert "database-secret" not in output.out + output.err
