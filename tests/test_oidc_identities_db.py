from __future__ import annotations

import asyncio
import os

import pytest
from psycopg import errors
from psycopg.rows import dict_row

from app.accounts import create_admin
from app.db import make_pool
from app.oidc_identities import (
    IdentityLinkRejectedError,
    create_identity_link,
    establish_legacy_admin_identity,
    get_identity_for_account,
    normalize_issuer,
    resolve_identity_account,
    touch_identity_last_used,
    unlink_identity,
)
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)


async def _identity_count(conn) -> int:
    cur = await conn.execute("SELECT count(*) FROM oidc_identities")
    return (await cur.fetchone())[0]


async def _schema_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            account = await create_admin(conn, "admin@example.com", "hash")
            await conn.execute(
                "INSERT INTO oidc_identities (account_id, issuer, subject) "
                "VALUES (%s, %s, %s)",
                (account["id"], "https://id.example", "subject-1"),
            )
            with pytest.raises(errors.UniqueViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO oidc_identities (account_id, issuer, subject) "
                        "VALUES (%s, %s, %s)",
                        (account["id"], "https://id.example", "subject-1"),
                    )
            with pytest.raises(errors.CheckViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO oidc_identities (account_id, issuer, subject) "
                        "VALUES (%s, %s, %s)",
                        (account["id"], "https://id.example/", "subject-2"),
                    )
            with pytest.raises(errors.ForeignKeyViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO oidc_identities (account_id, issuer, subject) "
                        "VALUES (%s, %s, %s)",
                        (999, "https://other-id.example", "subject-3"),
                    )
    finally:
        await pool.close()


def test_oidc_identity_schema_enforces_normalized_exact_identity():
    asyncio.run(_schema_scenario())


async def _resolution_and_metadata_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            account = await create_admin(conn, "local@example.com", "hash")
            identity = await create_identity_link(
                conn,
                account["id"],
                "https://id.example/",
                "subject-1",
                provider_email="before@example.net",
                provider_display_name="Before",
            )
            assert identity["issuer"] == "https://id.example"
            assert await create_identity_link(
                conn,
                account["id"],
                "https://second-id.example",
                "subject-2",
            ) is not None
            assert normalize_issuer("https://id.example///") == "https://id.example"
            assert await get_identity_for_account(
                conn, account["id"], "https://id.example/"
            ) == identity

            resolved = await resolve_identity_account(
                conn, "https://id.example/", "subject-1"
            )
            assert resolved is not None
            assert resolved["id"] == account["id"]
            assert resolved["email"] == "local@example.com"
            assert (
                await resolve_identity_account(
                    conn, "https://id.example", "different-subject"
                )
                is None
            )

            updated = await touch_identity_last_used(
                conn,
                "https://id.example",
                "subject-1",
                provider_email="changed@example.net",
                provider_display_name="Changed",
            )
            assert updated is not None
            assert updated["provider_email"] == "changed@example.net"
            assert updated["provider_display_name"] == "Changed"
            assert updated["last_used_at"] >= updated["linked_at"]
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("SELECT email FROM accounts WHERE id = %s", (account["id"],))
            assert (await cur.fetchone())["email"] == "local@example.com"
            await conn.execute(
                "UPDATE accounts SET is_enabled = false WHERE id = %s", (account["id"],)
            )
            assert (
                await resolve_identity_account(
                    conn, "https://id.example", "subject-1"
                )
                is None
            )
    finally:
        await pool.close()


def test_identity_resolution_is_exact_and_provider_email_is_only_metadata():
    asyncio.run(_resolution_and_metadata_scenario())


async def _duplicate_and_owner_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            owner = await create_admin(conn, "owner@example.com", "hash")
            await create_identity_link(
                conn, owner["id"], "https://id.example", "subject-1"
            )
            assert (
                await create_identity_link(
                    conn, owner["id"], "https://id.example", "subject-1"
                )
                is None
            )

            await conn.execute("DROP INDEX accounts_singleton_idx")
            # A privileged synthetic second identity isolates link ownership;
            # first-account bootstrap correctly remains closed even without its index.
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("INSERT INTO accounts(email,password_hash) VALUES('other@example.com','hash') RETURNING id")
            other = await cur.fetchone()
            assert (
                await create_identity_link(
                    conn, other["id"], "https://id.example", "subject-1"
                )
                is None
            )
            await conn.execute("DELETE FROM accounts WHERE id = %s", (other["id"],))
            await conn.execute("CREATE UNIQUE INDEX accounts_singleton_idx ON accounts ((true))")
    finally:
        await pool.close()


def test_duplicate_links_and_identities_owned_by_another_account_fail_closed():
    asyncio.run(_duplicate_and_owner_scenario())


async def _stale_link_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            account = await create_admin(conn, "admin@example.com", "hash")
            assert (
                await create_identity_link(
                    conn,
                    account["id"],
                    "https://id.example",
                    "subject-1",
                    expected_auth_version=2,
                )
                is None
            )
            assert await _identity_count(conn) == 0
            assert await create_identity_link(
                conn,
                account["id"],
                "https://id.example",
                "subject-1",
                expected_auth_version=1,
            ) is not None
    finally:
        await pool.close()


def test_stale_account_session_cannot_create_an_oidc_link():
    asyncio.run(_stale_link_scenario())


async def _unlink_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            account = await create_admin(conn, "admin@example.com", "hash")
            await create_identity_link(
                conn, account["id"], "https://id.example", "subject-1"
            )
            assert (
                await unlink_identity(
                    conn,
                    account["id"],
                    "https://id.example",
                    "subject-1",
                    expected_auth_version=2,
                )
                is None
            )
            assert await _identity_count(conn) == 1
            assert (
                await unlink_identity(
                    conn,
                    account["id"],
                    "https://id.example",
                    "other-subject",
                    expected_auth_version=1,
                )
                is None
            )
            cur = await conn.execute(
                "SELECT auth_version FROM accounts WHERE id = %s", (account["id"],)
            )
            assert (await cur.fetchone())[0] == 1

            unlinked = await unlink_identity(
                conn,
                account["id"],
                "https://id.example/",
                "subject-1",
                expected_auth_version=1,
            )
            assert unlinked is not None
            assert unlinked["auth_version"] == 2
            assert await _identity_count(conn) == 0
    finally:
        await pool.close()


def test_unlink_targets_the_exact_identity_and_revokes_sessions():
    asyncio.run(_unlink_scenario())


async def _legacy_establishment_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            owner = await create_admin(conn, "owner@example.com", "hash")
            await create_identity_link(
                conn, owner["id"], "https://id.example", "subject-1"
            )
            with pytest.raises(errors.UniqueViolation):
                await establish_legacy_admin_identity(
                    conn,
                    email="new@example.com",
                    password_hash="new-hash",
                    issuer="https://id.example/",
                    subject="subject-1",
                    provider_email="current@example.net",
                )
            cur = await conn.execute("SELECT email FROM accounts ORDER BY id")
            assert [row[0] for row in await cur.fetchall()] == ["owner@example.com"]
            assert await _identity_count(conn) == 1
            await conn.execute("CREATE UNIQUE INDEX accounts_singleton_idx ON accounts ((true))")
    finally:
        await pool.close()


def test_legacy_establishment_cannot_bypass_completed_bootstrap_by_dropping_singleton_index():
    asyncio.run(_legacy_establishment_scenario())


def test_failed_identity_link_rolls_back_first_account_and_all_owned_defaults(monkeypatch):
    import app.oidc_identities as identities

    async def rejected_link(*args, **kwargs):
        return None
    monkeypatch.setattr(identities, "create_identity_link", rejected_link)

    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await reset_db(pool)
            async with pool.connection() as conn:
                with pytest.raises(IdentityLinkRejectedError):
                    await establish_legacy_admin_identity(
                        conn, email="admin@example.com", password_hash="test-hash",
                        issuer="https://id.example", subject="subject-1",
                    )
                for table in ("accounts", "account_settings", "vehicles", "tag_rules", "mileage_rates", "oidc_identities"):
                    assert (await (await conn.execute(f"SELECT count(*) FROM {table}")).fetchone())[0] == 0
                assert (await (await conn.execute("SELECT first_account_id,bootstrap_completed_at FROM instance_state WHERE id=1")).fetchone()) == (None,None)
        finally:
            await pool.close()
    asyncio.run(run())
