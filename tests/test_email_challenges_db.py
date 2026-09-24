from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from psycopg import errors

from app.accounts import create_admin
from app.application_roles import (
    _load_state, application_role_pools, prepare_application_roles,
    validate_application_contract,
)
from app.db import make_pool
from app.email_challenges import (
    PURPOSE_CHANGE, PURPOSE_CURRENT, consume_email_challenge,
    is_current_email_verified, issue_email_challenge, revoke_email_challenge,
)
from app.role_setup import RoleSetupError
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        async with owner.connection() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
        await full_schema_reset(owner)
        await prepare_application_roles(TEST_DB)
        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                account = await create_admin(conn, "old@example.invalid", "test-hash")
            await callback(owner, pools, account)
    finally:
        await owner.close()


def test_verify_current_is_single_use_and_stale_proof_fails():
    async def check(owner, pools, account):
        account_id = account["id"]
        async with pools.control.connection() as conn:
            assert not await is_current_email_verified(conn, account_id)
            token = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
            assert token
            assert await issue_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, "new@example.invalid") is None
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, token) is None
            verified = await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, token)
            assert verified["email"] == "old@example.invalid"
            assert verified["auth_version"] == 1
            assert "avatar_mime" in verified
            assert await is_current_email_verified(conn, account_id)
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, token) is None

        async with owner.connection() as conn:
            await conn.execute("UPDATE email_challenges SET created_at = now() - interval '2 minutes' WHERE account_id=%s", (account_id,))
        async with pools.control.connection() as conn:
            stale = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
            assert stale
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET auth_version = auth_version + 1 WHERE id=%s", (account_id,))
            await conn.execute("UPDATE accounts SET email_verified_at = NULL WHERE id=%s", (account_id,))
        async with pools.control.connection() as conn:
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, stale) is None
            assert not await is_current_email_verified(conn, account_id)
    asyncio.run(_scenario(check))


def test_change_revokes_old_verification_and_old_sessions():
    async def check(owner, pools, account):
        account_id = account["id"]
        async with pools.control.connection() as conn:
            current = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, current)
        async with owner.connection() as conn:
            await conn.execute("UPDATE email_challenges SET created_at = now() - interval '2 minutes' WHERE account_id=%s", (account_id,))
        async with pools.control.connection() as conn:
            token = await issue_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, " NEW@example.invalid ")
            assert token
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, token) is None
            changed = await consume_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, token)
            assert changed["email"] == "new@example.invalid"
            assert changed["auth_version"] == 2
            assert await is_current_email_verified(conn, account_id)
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, token) is None
            assert await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "new@example.invalid") is None
        async with owner.connection() as conn:
            row = await (await conn.execute("SELECT email,email_verified_at,auth_version FROM accounts WHERE id=%s", (account_id,))).fetchone()
            assert row[0] == "new@example.invalid" and row[1] is not None and row[2] == 2
    asyncio.run(_scenario(check))


def test_attempt_limit_expiry_revocation_and_rollback():
    async def check(owner, pools, account):
        account_id = account["id"]
        async with pools.control.connection() as conn:
            token = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
            for _ in range(5):
                assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "A" * 43) is None
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, token) is None
        async with owner.connection() as conn:
            await conn.execute("UPDATE email_challenges SET created_at = now() - interval '2 minutes' WHERE account_id=%s", (account_id,))
        async with pools.control.connection() as conn:
            replacement = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
            assert replacement
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, token) is None
            await revoke_email_challenge(conn, account_id, PURPOSE_CURRENT, replacement)
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, replacement) is None
        async with owner.connection() as conn:
            await conn.execute("UPDATE email_challenges SET created_at = now() - interval '2 minutes' WHERE account_id=%s", (account_id,))
        async with pools.control.connection() as conn:
            expiring = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE email_challenges SET created_at=now()-interval '2 minutes', "
                "expires_at=now()-interval '1 minute' WHERE token_digest=%s",
                (hashlib.sha256(expiring.encode()).hexdigest(),),
            )
        async with pools.control.connection() as conn:
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, expiring) is None
        async with owner.connection() as conn:
            await conn.execute("UPDATE email_challenges SET created_at = now() - interval '2 minutes' WHERE account_id=%s", (account_id,))
        async with pools.control.connection() as conn:
            fourth = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
            assert fourth
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, fourth)
                    raise RuntimeError("rollback")
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, fourth)
    asyncio.run(_scenario(check))


def test_restricted_roles_cannot_write_verification_state_directly():
    async def check(owner, pools, account):
        async with pools.control.connection() as conn:
            for statement in (
                "UPDATE accounts SET email_verified_at=now()",
                "UPDATE accounts SET email='stolen@example.invalid'",
                "UPDATE email_challenges SET consumed_at=now()",
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(statement)
        async with pools.runtime.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT token_digest FROM email_challenges")
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT public.consume_email_challenge(1,1,'verify_current','a')")
    asyncio.run(_scenario(check))


def test_daily_issue_limit_and_account_disablement():
    async def check(owner, pools, account):
        account_id = account["id"]
        tokens = []
        for _ in range(5):
            async with pools.control.connection() as conn:
                token = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")
                assert token
                tokens.append(token)
            async with owner.connection() as conn:
                await conn.execute(
                    "UPDATE email_challenges SET created_at=now()-interval '2 minutes' "
                    "WHERE account_id=%s", (account_id,),
                )
        async with pools.control.connection() as conn:
            assert await issue_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, "new@example.invalid") is None
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, tokens[0]) is None
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (account_id,))
        async with pools.control.connection() as conn:
            assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, tokens[-1]) is None
            assert await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid") is None
    asyncio.run(_scenario(check))


def test_change_collision_preserves_old_login_and_verification():
    async def check(owner, pools, account):
        account_id = account["id"]
        async with owner.connection() as conn:
            async with conn.transaction(force_rollback=True):
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin) "
                    "VALUES ('taken@example.invalid','test-hash',true)"
                )
                await conn.execute(
                    "UPDATE accounts SET email_verified_at=now() WHERE id=%s", (account_id,)
                )
                token = await issue_email_challenge(
                    conn, account_id, 1, PURPOSE_CHANGE, "taken@example.invalid",
                )
                assert token
                assert await consume_email_challenge(conn, account_id, 1, PURPOSE_CHANGE, token) is None
                row = await (await conn.execute(
                    "SELECT email,email_verified_at,auth_version FROM accounts WHERE id=%s",
                    (account_id,),
                )).fetchone()
                assert row[0] == "old@example.invalid" and row[1] is not None and row[2] == 1
        async with owner.connection() as conn:
            row = await (await conn.execute(
                "SELECT indexname FROM pg_indexes WHERE indexname='accounts_singleton_idx'"
            )).fetchone()
            assert row is not None
    asyncio.run(_scenario(check))


def test_concurrent_consume_has_one_winner():
    async def check(owner, pools, account):
        account_id = account["id"]
        async with pools.control.connection() as conn:
            token = await issue_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, "old@example.invalid")

        async def consume():
            async with pools.control.connection() as conn:
                return await consume_email_challenge(conn, account_id, 1, PURPOSE_CURRENT, token)

        results = await asyncio.gather(consume(), consume())
        assert sum(result is not None for result in results) == 1
    asyncio.run(_scenario(check))


def test_protected_table_requires_forced_rls_and_account_policy():
    async def check(owner, pools, account):
        async with owner.connection() as conn:
            state = await _load_state(conn)
            with pytest.raises(RoleSetupError, match="relation ownership or RLS flags: .*email_challenges"):
                async with conn.transaction(force_rollback=True):
                    await conn.execute("ALTER TABLE email_challenges NO FORCE ROW LEVEL SECURITY")
                    await validate_application_contract(conn, state)
            with pytest.raises(RoleSetupError, match="policy set: .*email_challenges"):
                async with conn.transaction(force_rollback=True):
                    await conn.execute("DROP POLICY account_isolation ON email_challenges")
                    await validate_application_contract(conn, state)
            await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))
