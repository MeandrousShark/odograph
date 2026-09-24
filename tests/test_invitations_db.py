from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from psycopg import errors

from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.auth import _account_user
from app.db import make_pool
from app.invitations import InvitationUnavailable, issue_invitation, redeem_invitation
from app.local_auth import verify_password
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
                admin = await create_admin(conn, "admin@example.invalid", "existing-hash")
            admin_user = _account_user(admin)
            await callback(owner, pools, admin_user)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


async def _wait_for_lock(owner, pid, task):
    for _ in range(150):
        async with owner.connection() as observer:
            row = await (await observer.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,))).fetchone()
        if row == ("Lock",):
            return
        assert not task.done(), "invitation operation bypassed the expected lock"
        await asyncio.sleep(0.01)
    pytest.fail("invitation operation did not wait for the expected lock")


def test_issue_requires_enabled_admin_normalizes_email_and_revokes_prior_token():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, {**admin_user, "is_admin": False}, "guest@example.invalid")
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, {**admin_user, "is_enabled": False}, "guest@example.invalid")
        async with pools.control.connection() as conn:
            first = await issue_invitation(conn, admin_user, " Guest@Example.Invalid ")
        async with pools.control.connection() as conn:
            second = await issue_invitation(conn, admin_user, "guest@example.invalid")
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await issue_invitation(conn, admin_user, "admin@example.invalid")
        async with owner.connection() as conn:
            rows = await (await conn.execute(
                "SELECT token_digest,email,issued_by,expires_at-created_at,revoked_at IS NOT NULL "
                "FROM invitations ORDER BY created_at, token_digest")).fetchall()
            assert len(rows) == 2
            assert {row[0] for row in rows} == {hashlib.sha256(token.encode()).hexdigest() for token in (first, second)}
            assert all(row[1:4] == ("guest@example.invalid", admin_id, rows[0][3]) for row in rows)
            assert all(row[3].total_seconds() == 48 * 3600 for row in rows)
            assert sorted(row[4] for row in rows) == [False, True]
            assert first not in str(rows) and second not in str(rows)
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (admin_id,))
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await issue_invitation(conn, admin_user, "next@example.invalid")
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await redeem_invitation(conn, second, "good-password")
    asyncio.run(_scenario(check))


def test_normal_singleton_refuses_member_and_rolls_back_token_use():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "guest@example.invalid")
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await redeem_invitation(conn, token, "good-password")
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute(
                "SELECT consumed_at IS NULL FROM invitations WHERE token_digest=%s",
                (hashlib.sha256(token.encode()).hexdigest(),))).fetchone() == (True,)
    asyncio.run(_scenario(check))


def test_member_defaults_atomicity_replay_expiry_and_restricted_roles():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "guest@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "ALTER TABLE vehicles ADD CONSTRAINT invitation_failure_probe CHECK (account_id=%s)".replace(
                    "%s", str(admin_id)))
        try:
            async with pools.control.connection() as conn:
                with pytest.raises(InvitationUnavailable):
                    async with conn.transaction():
                        await redeem_invitation(conn, token, "good-password")
        finally:
            async with owner.connection() as conn:
                await conn.execute("ALTER TABLE vehicles DROP CONSTRAINT invitation_failure_probe")
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
        async with pools.control.connection() as conn:
            member_id = await redeem_invitation(conn, token, "good-password", display_timezone="Europe/London")
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await redeem_invitation(conn, token, "good-password")
        async with owner.connection() as conn:
            account = await (await conn.execute(
                "SELECT email,password_hash,is_admin FROM accounts WHERE id=%s", (member_id,))).fetchone()
            assert account[0] == "guest@example.invalid" and not account[2]
            assert verify_password("good-password", account[1])
            assert await (await conn.execute("SELECT display_tz FROM account_settings WHERE account_id=%s", (member_id,))).fetchone() == ("Europe/London",)
            assert await (await conn.execute("SELECT name,is_default FROM vehicles WHERE account_id=%s", (member_id,))).fetchall() == [("My Car", True)]
            assert await (await conn.execute("SELECT a_kind,b_kind,category FROM tag_rules WHERE account_id=%s ORDER BY a_kind", (member_id,))).fetchall() == [("home", "work", "personal"), ("work", "work", "business")]
            assert await (await conn.execute("SELECT count(*) FROM mileage_rates WHERE account_id=%s", (member_id,))).fetchone() == await (await conn.execute("SELECT count(*) FROM reference_mileage_rates")).fetchone()
            assert await (await conn.execute("SELECT consumed_at IS NOT NULL FROM invitations")).fetchone() == (True,)
        async with pools.runtime.connection() as conn:
            for statement in (
                "SELECT public.issue_member_invitation(1,'x@example.invalid','a')",
                "SELECT public.redeem_member_invitation('a','b','UTC')",
                "INSERT INTO accounts(email,password_hash) VALUES('x@example.invalid','hash')",
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(statement)
    asyncio.run(_scenario(check))


def test_expired_revoked_and_concurrent_redemption():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            expired = await issue_invitation(conn, admin_user, "expired@example.invalid")
            revoked = await issue_invitation(conn, admin_user, "revoked@example.invalid")
            live = await issue_invitation(conn, admin_user, "live@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET created_at=now()-interval '49 hours', "
                "expires_at=now()-interval '1 hour' WHERE email='expired@example.invalid'")
            await conn.execute("UPDATE invitations SET revoked_at=now() WHERE email='revoked@example.invalid'")
        for token in (expired, revoked):
            async with pools.control.connection() as conn:
                with pytest.raises(InvitationUnavailable):
                    await redeem_invitation(conn, token, "good-password")
        async def attempt():
            async with pools.control.connection() as conn:
                try:
                    return await redeem_invitation(conn, live, "good-password")
                except InvitationUnavailable:
                    return None
        results = await asyncio.gather(attempt(), attempt())
        assert sum(result is not None for result in results) == 1
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (2,)
    asyncio.run(_scenario(check))


def test_issuer_disable_serializes_with_issue_and_redeem():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "member@example.invalid")

        async def disabled_before(operation):
            async with pools.control.connection() as control:
                pid = (await (await control.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with owner.connection() as blocker:
                    async with blocker.transaction():
                        await blocker.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (admin_id,))
                        task = asyncio.create_task(operation(control))
                        await _wait_for_lock(owner, pid, task)
                    with pytest.raises(InvitationUnavailable):
                        await task

        await disabled_before(lambda conn: issue_invitation(conn, admin_user, "other@example.invalid"))
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET is_enabled=true WHERE id=%s", (admin_id,))
        await disabled_before(lambda conn: redeem_invitation(conn, token, "good-password"))
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
    asyncio.run(_scenario(check))


def test_issue_and_redeem_serialize_per_email_in_both_orders():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")

        async with pools.control.connection() as conn:
            old_token = await issue_invitation(conn, admin_user, "race@example.invalid")

        async with pools.control.connection() as issuer:
            async with pools.control.connection() as redeemer:
                pid = (await (await redeemer.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with issuer.transaction():
                    new_token = await issue_invitation(issuer, admin_user, "race@example.invalid")
                    task = asyncio.create_task(redeem_invitation(redeemer, old_token, "good-password"))
                    await _wait_for_lock(owner, pid, task)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL,consumed_at IS NULL FROM invitations "
                "WHERE token_digest=%s", (hashlib.sha256(old_token.encode()).hexdigest(),))).fetchone() == (True, True)

        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "winner@example.invalid")
        async with pools.control.connection() as redeemer:
            async with pools.control.connection() as issuer:
                pid = (await (await issuer.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with redeemer.transaction():
                    member_id = await redeem_invitation(redeemer, token, "good-password")
                    task = asyncio.create_task(issue_invitation(issuer, admin_user, "winner@example.invalid"))
                    await _wait_for_lock(owner, pid, task)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT email FROM accounts WHERE id=%s", (member_id,))).fetchone() == ("winner@example.invalid",)
            assert await (await conn.execute(
                "SELECT revoked_at,consumed_at IS NOT NULL FROM invitations "
                "WHERE token_digest=%s", (hashlib.sha256(token.encode()).hexdigest(),))).fetchone() == (None, True)
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (2,)
            assert new_token != old_token
    asyncio.run(_scenario(check))


def test_expiry_while_waiting_for_email_lock_refuses_redemption():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "soon@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET expires_at=pg_catalog.clock_timestamp()+interval '1 second' "
                "WHERE email='soon@example.invalid'")
        async with owner.connection() as blocker:
            async with pools.control.connection() as redeemer:
                pid = (await (await redeemer.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with blocker.transaction():
                    await blocker.execute(
                        "SELECT pg_advisory_xact_lock(901410,hashtext('soon@example.invalid'))")
                    task = asyncio.create_task(redeem_invitation(redeemer, token, "good-password"))
                    await _wait_for_lock(owner, pid, task)
                    await asyncio.sleep(1.1)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
    asyncio.run(_scenario(check))
