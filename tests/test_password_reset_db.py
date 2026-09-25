"""Protected password reset through the restricted control role."""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

import pytest
from psycopg import errors

from app.accounts import create_admin, get_account, replace_password
from app.application_roles import (
    application_role_pools, finalize_application_restore, prepare_application_roles,
)
from app.db import make_pool
from app.email_challenges import (
    PURPOSE_CHANGE, PURPOSE_CURRENT, consume_email_challenge, issue_email_challenge,
)
from app.oidc_identities import create_identity_link
from app.password_reset import (
    INITIATOR_ADMIN, INITIATOR_PUBLIC, consume_password_reset, host_reset_password,
    issue_password_reset, password_reset_usable, revoke_password_reset,
)
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

A_EMAIL = "reset-a@example.invalid"
B_EMAIL = "reset-b@example.invalid"


def _token() -> str:
    return secrets.token_urlsafe(32)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


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
                a = await create_admin(conn, A_EMAIL, "hash-a")
            # Test-only second account; full_schema_reset restores the guards.
            async with owner.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                cur = await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin,email_verified_at) "
                    "VALUES (%s,'hash-b',false,now()) RETURNING id", (B_EMAIL,))
                b_id = (await cur.fetchone())[0]
                await conn.execute("UPDATE accounts SET email_verified_at=now() WHERE id=%s", (a["id"],))
            await callback(owner, pools, a["id"], b_id)
    finally:
        await full_schema_reset(owner)
        await owner.close()


async def _issue(pools, *, email=None, account_id=None, initiator=INITIATOR_PUBLIC):
    token = _token()
    async with pools.control.connection() as conn:
        address = await issue_password_reset(
            conn, token, initiator=initiator, email=email, account_id=account_id)
    return (token, address) if address is not None else (None, None)


async def _age_resets(owner, account_id, interval="11 minutes"):
    async with owner.connection() as conn:
        await conn.execute(
            f"UPDATE email_challenges SET created_at=created_at-interval '{interval}' "
            "WHERE account_id=%s", (account_id,))


async def _row(owner, account_id):
    async with owner.connection() as conn:
        cur = await conn.execute(
            "SELECT password_hash,auth_version,email,email_verified_at IS NOT NULL,is_enabled "
            "FROM accounts WHERE id=%s", (account_id,))
        return await cur.fetchone()


def test_eligibility_is_enabled_account_with_verified_current_email():
    async def check(owner, pools, a_id, b_id):
        assert await _issue(pools, email="nobody@example.invalid") == (None, None)
        token, address = await _issue(pools, email=" RESET-A@example.invalid ")
        assert token and address == A_EMAIL
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email_verified_at=NULL WHERE id=%s", (b_id,))
        assert await _issue(pools, email=B_EMAIL) == (None, None)
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email_verified_at=now(), is_enabled=false WHERE id=%s", (b_id,))
        assert await _issue(pools, email=B_EMAIL) == (None, None)
        # Only digests are stored, and the control role cannot read them.
        async with owner.connection() as conn:
            cur = await conn.execute("SELECT token_digest,target_email,initiator FROM email_challenges")
            assert await cur.fetchall() == [(_digest(token), A_EMAIL, "public")]
        async with pools.control.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute("SELECT token_digest FROM email_challenges")
    asyncio.run(_scenario(check))


def test_public_request_coalesces_without_cancelling_or_spending_budget():
    async def check(owner, pools, a_id, b_id):
        first, _ = await _issue(pools, email=A_EMAIL)
        for _ in range(8):
            assert await _issue(pools, email=A_EMAIL) == (None, None)
        async with pools.control.connection() as conn:
            assert await password_reset_usable(conn, first)
        async with owner.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM email_challenges WHERE account_id=%s", (a_id,))
            assert await cur.fetchone() == (1,)
        # Admin initiation may supersede within its own budget.
        admin, address = await _issue(pools, account_id=a_id, initiator=INITIATOR_ADMIN)
        assert admin and address == A_EMAIL
        async with pools.control.connection() as conn:
            assert not await password_reset_usable(conn, first)
            assert await password_reset_usable(conn, admin)
    asyncio.run(_scenario(check))


def test_public_admin_and_email_budgets_are_separate_and_persistent():
    async def check(owner, pools, a_id, b_id):
        for _ in range(5):
            token, _ = await _issue(pools, email=A_EMAIL)
            assert token
            assert await _issue(pools, email=A_EMAIL) == (None, None)  # coalesced
            await _age_resets(owner, a_id)
        assert await _issue(pools, email=A_EMAIL) == (None, None)  # public budget spent
        for _ in range(5):
            token, _ = await _issue(pools, account_id=a_id, initiator=INITIATOR_ADMIN)
            assert token
            assert await _issue(pools, account_id=a_id, initiator=INITIATOR_ADMIN) == (None, None)
            await _age_resets(owner, a_id, "2 minutes")
        assert await _issue(pools, account_id=a_id, initiator=INITIATOR_ADMIN) == (None, None)
        async with pools.control.connection() as conn:
            assert await issue_email_challenge(conn, a_id, 1, PURPOSE_CURRENT, A_EMAIL)
        # Budgets are rows, so a new process sees the same counts.
        async with application_role_pools(TEST_DB) as restarted:
            async with restarted.control.connection() as conn:
                assert await issue_password_reset(
                    conn, _token(), initiator=INITIATOR_PUBLIC, email=A_EMAIL) is None
        # Another account's budget is untouched.
        assert (await _issue(pools, email=B_EMAIL))[0]
        # A day later the public budget is available again.
        await _age_resets(owner, a_id, "24 hours")
        assert (await _issue(pools, email=A_EMAIL))[0]
    asyncio.run(_scenario(check))


def test_email_challenges_do_not_spend_reset_budget():
    async def check(owner, pools, a_id, b_id):
        async with pools.control.connection() as conn:
            assert await issue_email_challenge(conn, a_id, 1, PURPOSE_CURRENT, A_EMAIL)
        assert (await _issue(pools, email=A_EMAIL))[0]
        async with pools.control.connection() as conn:
            assert await issue_email_challenge(conn, a_id, 1, PURPOSE_CHANGE, "new-a@example.invalid") is None
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE email_challenges SET created_at=created_at-interval '2 minutes' "
                "WHERE account_id=%s AND purpose='verify_current'", (a_id,))
        # The reset issued seconds ago does not count against 2B's allowance.
        async with pools.control.connection() as conn:
            assert await issue_email_challenge(conn, a_id, 1, PURPOSE_CHANGE, "new-a@example.invalid")
    asyncio.run(_scenario(check))


def test_consume_replaces_password_ends_sessions_and_revokes_challenges():
    async def check(owner, pools, a_id, b_id):
        async with pools.control.connection() as conn:
            email_token = await issue_email_challenge(conn, a_id, 1, PURPOSE_CHANGE, "new-a@example.invalid")
            await create_identity_link(conn, a_id, "https://idp.example.invalid", "subject-a")
        token, _ = await _issue(pools, email=A_EMAIL)
        b_before = await _row(owner, b_id)
        async with pools.control.connection() as conn:
            assert await consume_password_reset(conn, token, "") is None
            assert await consume_password_reset(conn, token, "new-hash") == a_id
            assert await consume_password_reset(conn, token, "again-hash") is None
            assert not await password_reset_usable(conn, token)
            assert await consume_email_challenge(conn, a_id, 1, PURPOSE_CHANGE, email_token) is None
            assert await consume_email_challenge(conn, a_id, 2, PURPOSE_CHANGE, email_token) is None
        assert await _row(owner, a_id) == ("new-hash", 2, A_EMAIL, True, True)
        assert await _row(owner, b_id) == b_before
        async with owner.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM email_challenges WHERE account_id=%s "
                "AND consumed_at IS NULL AND revoked_at IS NULL", (a_id,))
            assert await cur.fetchone() == (0,)
            cur = await conn.execute("SELECT subject FROM oidc_identities WHERE account_id=%s", (a_id,))
            assert await cur.fetchall() == [("subject-a",)]
    asyncio.run(_scenario(check))


def test_malformed_expired_superseded_stale_and_rolled_back_proofs_fail():
    async def check(owner, pools, a_id, b_id):
        async with pools.control.connection() as conn:
            assert await consume_password_reset(conn, "short", "hash") is None
            assert await consume_password_reset(conn, "A" * 43, "hash") is None
        old, _ = await _issue(pools, account_id=a_id, initiator=INITIATOR_ADMIN)
        await _age_resets(owner, a_id, "2 minutes")
        new, _ = await _issue(pools, account_id=a_id, initiator=INITIATOR_ADMIN)
        async with pools.control.connection() as conn:
            assert await consume_password_reset(conn, old, "hash") is None
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE email_challenges SET created_at=now()-interval '31 minutes', "
                "expires_at=now()-interval '1 minute' WHERE token_digest=%s", (_digest(new),))
        async with pools.control.connection() as conn:
            assert await consume_password_reset(conn, new, "hash") is None

        rolled, _ = await _issue(pools, email=A_EMAIL)
        async with pools.control.connection() as conn:
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    assert await consume_password_reset(conn, rolled, "rolled-hash") == a_id
                    raise RuntimeError("rollback")
        assert (await _row(owner, a_id))[:2] == ("hash-a", 1)

        # A password change after issuance makes the reset stale.
        async with pools.control.connection() as conn:
            assert await replace_password(conn, a_id, "changed-hash", expected_auth_version=1)
            assert await consume_password_reset(conn, rolled, "stale-hash") is None
        await _age_resets(owner, a_id, "2 minutes")

        # So does a login-email change, and disablement.
        after_change, _ = await _issue(pools, email=A_EMAIL)
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email='moved-a@example.invalid' WHERE id=%s", (a_id,))
        async with pools.control.connection() as conn:
            assert await consume_password_reset(conn, after_change, "hash") is None
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email=%s WHERE id=%s", (A_EMAIL, a_id))
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (a_id,))
        async with pools.control.connection() as conn:
            assert await consume_password_reset(conn, after_change, "hash") is None
        assert (await _row(owner, a_id))[:2] == ("changed-hash", 2)
    asyncio.run(_scenario(check))


def test_delivery_failure_revocation_is_exact():
    async def check(owner, pools, a_id, b_id):
        a_token, _ = await _issue(pools, email=A_EMAIL)
        b_token, _ = await _issue(pools, email=B_EMAIL)
        async with pools.control.connection() as conn:
            await revoke_password_reset(conn, a_token)
            assert not await password_reset_usable(conn, a_token)
            assert await password_reset_usable(conn, b_token)
        # A revoked reset no longer blocks a new public request.
        await _age_resets(owner, a_id, "2 minutes")
        assert (await _issue(pools, email=A_EMAIL))[0]
    asyncio.run(_scenario(check))


async def _hold_account_lock(owner, account_id, entered: asyncio.Event, release: asyncio.Event):
    async with owner.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM accounts WHERE id=%s FOR UPDATE", (account_id,))
            entered.set()
            await release.wait()


async def _race(owner, account_id, *contenders, before_release=None):
    """Hold the account row so every contender queues on it, then release."""
    entered, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_account_lock(owner, account_id, entered, release))
    await entered.wait()
    tasks = [asyncio.create_task(contender()) for contender in contenders]
    await asyncio.sleep(0.3)
    assert not any(task.done() for task in tasks)
    if before_release is not None:
        await before_release()
    release.set()
    await holder
    return await asyncio.gather(*tasks, return_exceptions=True)


def test_competing_proofs_from_one_version_have_one_winner():
    async def check(owner, pools, a_id, b_id):
        token, _ = await _issue(pools, email=A_EMAIL)

        async def reset(password_hash):
            async with pools.control.connection() as conn:
                return await consume_password_reset(conn, token, password_hash)

        results = await _race(owner, a_id, lambda: reset("one"), lambda: reset("two"))
        assert sorted(result is None for result in results) == [False, True]

        await _age_resets(owner, a_id, "2 minutes")
        token, _ = await _issue(pools, email=A_EMAIL)
        async with pools.control.connection() as conn:
            email_token = await issue_email_challenge(conn, a_id, 2, PURPOSE_CHANGE, "new-a@example.invalid")

        async def change_password():
            async with pools.control.connection() as conn:
                return await replace_password(conn, a_id, "changed", expected_auth_version=2)

        async def change_email():
            async with pools.control.connection() as conn:
                return await consume_email_challenge(conn, a_id, 2, PURPOSE_CHANGE, email_token)

        results = await _race(owner, a_id, lambda: reset("three"), change_password, change_email)
        assert sum(result is not None for result in results) == 1
        assert (await _row(owner, a_id))[1] == 3
    asyncio.run(_scenario(check))


def test_reset_cannot_follow_committed_disablement_or_expiry_during_lock_wait():
    async def check(owner, pools, a_id, b_id):
        token, _ = await _issue(pools, email=A_EMAIL)

        async def disable():
            async with pools.control.connection() as conn:
                await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (a_id,))

        async def reset():
            async with pools.control.connection() as conn:
                return await consume_password_reset(conn, token, "after-disable")

        # Disablement queues first, so it commits before the reset rechecks.
        entered, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(_hold_account_lock(owner, a_id, entered, release))
        await entered.wait()
        disabling = asyncio.create_task(disable())
        await asyncio.sleep(0.2)
        resetting = asyncio.create_task(reset())
        await asyncio.sleep(0.2)
        release.set()
        await holder
        await disabling
        assert await resetting is None
        assert (await _row(owner, a_id))[0] == "hash-a"

        token, _ = await _issue(pools, email=B_EMAIL)
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE email_challenges SET expires_at=clock_timestamp()+interval '400 milliseconds' "
                "WHERE token_digest=%s", (_digest(token),))

        async def reset_b():
            async with pools.control.connection() as conn:
                return await consume_password_reset(conn, token, "late")

        async def wait_past_expiry():
            await asyncio.sleep(0.5)

        assert await _race(owner, b_id, reset_b, before_release=wait_past_expiry) == [None]
        assert (await _row(owner, b_id))[0] == "hash-b"
    asyncio.run(_scenario(check))


def test_host_reset_targets_exactly_one_enabled_account():
    async def check(owner, pools, a_id, b_id):
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email_verified_at=NULL WHERE id=%s", (b_id,))
        pending, _ = await _issue(pools, email=A_EMAIL)
        a_before = await _row(owner, a_id)
        async with pools.control.connection() as conn:
            assert await host_reset_password(conn, 999999, "hash") is None
            assert await host_reset_password(conn, b_id, "") is None
            updated = await host_reset_password(conn, b_id, "host-hash")
            assert updated["id"] == b_id and updated["auth_version"] == 2
        assert await _row(owner, b_id) == ("host-hash", 2, B_EMAIL, False, True)
        assert await _row(owner, a_id) == a_before
        async with pools.control.connection() as conn:
            assert await password_reset_usable(conn, pending)
            assert await host_reset_password(conn, a_id, "host-a")
            assert not await password_reset_usable(conn, pending)
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (b_id,))
        async with pools.control.connection() as conn:
            assert await host_reset_password(conn, b_id, "disabled-hash") is None
        assert await _row(owner, b_id) == ("host-hash", 2, B_EMAIL, False, False)
    asyncio.run(_scenario(check))


def test_restore_finalization_ends_sessions_and_revokes_lifecycle_tokens():
    async def check(owner, pools, a_id, b_id):
        reset_token, _ = await _issue(pools, email=A_EMAIL)
        async with pools.control.connection() as conn:
            await issue_email_challenge(conn, b_id, 1, PURPOSE_CURRENT, B_EMAIL)
            await conn.execute(
                "SELECT public.issue_member_invitation(%s,%s,%s)",
                (a_id, "invitee@example.invalid", _digest(_token())))
            before = {row["id"]: row["auth_version"] for row in
                      [await get_account(conn, a_id), await get_account(conn, b_id)]}
        await finalize_application_restore(TEST_DB)
        async with owner.connection() as conn:
            cur = await conn.execute("SELECT id,auth_version FROM accounts ORDER BY id")
            after = dict(await cur.fetchall())
            for table in ("email_challenges", "invitations"):
                cur = await conn.execute(
                    f"SELECT count(*) FROM {table} WHERE consumed_at IS NULL AND revoked_at IS NULL")
                assert await cur.fetchone() == (0,)
        # Every version issued after the backup, not just the restored one,
        # is left behind.
        for account_id, version in before.items():
            assert after[account_id] >= version + 1_000_000_000
        async with application_role_pools(TEST_DB) as restored:
            async with restored.control.connection() as conn:
                assert not await password_reset_usable(conn, reset_token)
    asyncio.run(_scenario(check))
