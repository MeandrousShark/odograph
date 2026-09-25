from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import errors

from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.db import make_pool
from app.invitations import InvitationUnavailable, issue_invitation, redeem_oidc_invitation_by_digest
from app.oidc_attempts import (
    consume_action_proof, consume_oidc_attempt, finish_oidc_reauth, start_oidc_attempt,
)
from app.oidc_identities import create_identity_link
from app.local_auth import hash_password, verify_password
from app.password_reset import host_reset_password
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
                admin = await create_admin(conn, "admin@example.invalid", "valid-hash")
            await callback(owner, pools, admin)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


async def _allow_member_accounts(owner):
    async with owner.connection() as conn:
        await conn.execute("DROP INDEX accounts_singleton_idx")
        await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")


def test_oidc_invitation_is_atomic_and_restricted():
    async def check(owner, pools, admin):
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, dict(admin, is_admin=True), "member@example.invalid")
            assert await start_oidc_attempt(
                conn, action="invite", state="state-1", nonce="nonce-1",
                browser_nonce="browser-1", invite_token=token, target="UTC",
            )
            pending = await consume_oidc_attempt(
                conn, action="invite", state="state-1", nonce="nonce-1",
                browser_nonce="browser-1",
            )
            assert pending["invitation_digest"] is not None
            assert token not in repr(pending)
            assert await consume_oidc_attempt(
                conn, action="invite", state="state-1", nonce="nonce-1",
                browser_nonce="browser-1",
            ) is None
            with pytest.raises(InvitationUnavailable):
                await redeem_oidc_invitation_by_digest(
                    conn, pending["invitation_digest"], "https://id.example", "subject-1",
                )
        async with owner.connection() as conn:
            assert (await (await conn.execute(
                "SELECT consumed_at FROM invitations")).fetchone()) == (None,)
        await _allow_member_accounts(owner)
        async with pools.control.connection() as conn:
            member_id = await redeem_oidc_invitation_by_digest(
                conn, pending["invitation_digest"], "https://id.example/", "subject-1",
                provider_email="other@example.invalid", display_timezone="UTC",
            )
            assert member_id != admin["id"]
            with pytest.raises(InvitationUnavailable):
                await redeem_oidc_invitation_by_digest(
                    conn, pending["invitation_digest"], "https://id.example", "subject-2",
                )
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute("INSERT INTO accounts(email,is_admin) VALUES ('bad@example.invalid',false)")
        async with owner.connection() as conn:
            row = await (await conn.execute(
                "SELECT a.email,a.password_hash,a.is_admin,a.email_verified_at,i.issuer,i.subject,"
                "i.provider_email,i.provider_display_name FROM accounts a "
                "JOIN oidc_identities i ON i.account_id=a.id "
                "WHERE a.id=%s", (member_id,))).fetchone()
            assert row == ("member@example.invalid", None, False, None,
                           "https://id.example", "subject-1", "other@example.invalid", None)
            assert await (await conn.execute(
                "SELECT display_tz FROM account_settings WHERE account_id=%s", (member_id,)
            )).fetchone() == ("UTC",)
            assert await (await conn.execute(
                "SELECT name,is_default FROM vehicles WHERE account_id=%s", (member_id,)
            )).fetchall() == [("My Car", True)]
            assert await (await conn.execute(
                "SELECT a_kind,b_kind,category FROM tag_rules WHERE account_id=%s ORDER BY a_kind",
                (member_id,),
            )).fetchall() == [("home", "work", "personal"), ("work", "work", "business")]
            assert await (await conn.execute(
                "SELECT year,rate_per_mi,rate_h2_per_mi,h2_start_month FROM mileage_rates "
                "WHERE account_id=%s ORDER BY year", (member_id,),
            )).fetchall() == await (await conn.execute(
                "SELECT year,rate_per_mi,rate_h2_per_mi,h2_start_month "
                "FROM reference_mileage_rates ORDER BY year"
            )).fetchall()
            with pytest.raises(errors.CheckViolation):
                await conn.execute("UPDATE accounts SET password_hash='' WHERE id=%s", (member_id,))
    asyncio.run(_scenario(check))


def test_oidc_invitation_expiry_and_revocation_are_rechecked():
    async def check(owner, pools, admin):
        async with pools.control.connection() as conn:
            expired = await issue_invitation(conn, dict(admin, is_admin=True), "expired@example.invalid")
            revoked = await issue_invitation(conn, dict(admin, is_admin=True), "revoked@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET created_at=pg_catalog.clock_timestamp()-interval '2 hours', "
                "expires_at=pg_catalog.clock_timestamp()-interval '1 minute' "
                "WHERE token_digest=%s", (hashlib.sha256(expired.encode()).hexdigest(),),
            )
            await conn.execute(
                "UPDATE invitations SET revoked_at=pg_catalog.clock_timestamp() "
                "WHERE token_digest=%s", (hashlib.sha256(revoked.encode()).hexdigest(),),
            )
        async with pools.control.connection() as conn:
            for token in (expired, revoked):
                with pytest.raises(InvitationUnavailable):
                    await redeem_oidc_invitation_by_digest(
                        conn, hashlib.sha256(token.encode()).hexdigest(),
                        "https://id.example", "unused-subject",
                    )
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM accounts"
            )).fetchone() == (1,)
            assert await (await conn.execute(
                "SELECT count(*) FROM invitations WHERE consumed_at IS NOT NULL"
            )).fetchone() == (0,)
    asyncio.run(_scenario(check))


def test_oidc_invitation_concurrent_redemption_creates_one_account():
    async def check(owner, pools, admin):
        await _allow_member_accounts(owner)
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, dict(admin, is_admin=True), "race@example.invalid")
        digest = hashlib.sha256(token.encode()).hexdigest()

        async def redeem(subject):
            async with pools.control.connection() as conn:
                try:
                    return await redeem_oidc_invitation_by_digest(
                        conn, digest, "https://id.example", subject,
                    )
                except InvitationUnavailable:
                    return None

        results = await asyncio.gather(redeem("race-subject-a"), redeem("race-subject-b"))
        assert sum(result is not None for result in results) == 1
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM accounts WHERE email='race@example.invalid'"
            )).fetchone() == (1,)
            assert await (await conn.execute(
                "SELECT consumed_at IS NOT NULL FROM invitations WHERE token_digest=%s", (digest,)
            )).fetchone() == (True,)
            assert await (await conn.execute(
                "SELECT count(*) FROM oidc_identities WHERE subject IN ('race-subject-a','race-subject-b')"
            )).fetchone() == (1,)
    asyncio.run(_scenario(check))


def test_oidc_invitation_rolls_back_all_provisioning_after_failure():
    async def check(owner, pools, admin):
        await _allow_member_accounts(owner)
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, dict(admin, is_admin=True), "rollback@example.invalid")
        digest = hashlib.sha256(token.encode()).hexdigest()
        async with owner.connection() as conn:
            await conn.execute(
                "CREATE FUNCTION public.fail_oidc_vehicle_for_test() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced OIDC invitation failure'; "
                "END $$"
            )
            await conn.execute(
                "CREATE TRIGGER fail_oidc_vehicle_for_test BEFORE INSERT ON public.vehicles "
                "FOR EACH ROW EXECUTE FUNCTION public.fail_oidc_vehicle_for_test()"
            )
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                await redeem_oidc_invitation_by_digest(
                    conn, digest, "https://id.example", "rollback-subject",
                )
        async with owner.connection() as conn:
            for table, where, params in (
                ("accounts", "email=%s", ("rollback@example.invalid",)),
                ("oidc_identities", "subject=%s", ("rollback-subject",)),
                ("account_settings", "account_id IN (SELECT id FROM accounts WHERE email=%s)",
                 ("rollback@example.invalid",)),
                ("vehicles", "account_id IN (SELECT id FROM accounts WHERE email=%s)",
                 ("rollback@example.invalid",)),
                ("tag_rules", "account_id IN (SELECT id FROM accounts WHERE email=%s)",
                 ("rollback@example.invalid",)),
                ("mileage_rates", "account_id IN (SELECT id FROM accounts WHERE email=%s)",
                 ("rollback@example.invalid",)),
            ):
                assert await (await conn.execute(
                    f"SELECT count(*) FROM {table} WHERE {where}", params,
                )).fetchone() == (0,)
            assert await (await conn.execute(
                "SELECT consumed_at FROM invitations WHERE token_digest=%s", (digest,),
            )).fetchone() == (None,)
            await conn.execute("DROP TRIGGER fail_oidc_vehicle_for_test ON public.vehicles")
            await conn.execute("DROP FUNCTION public.fail_oidc_vehicle_for_test()")
        async with pools.control.connection() as conn:
            member_id = await redeem_oidc_invitation_by_digest(
                conn, digest, "https://id.example", "rollback-subject",
            )
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT email FROM accounts WHERE id=%s", (member_id,),
            )).fetchone() == ("rollback@example.invalid",)
    asyncio.run(_scenario(check))


def test_oidc_attempt_restart_expiry_and_account_switch():
    async def check(owner, pools, admin):
        await _allow_member_accounts(owner)
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, dict(admin, is_admin=True), "switch@example.invalid")
            member_id = await redeem_oidc_invitation_by_digest(
                conn, hashlib.sha256(token.encode()).hexdigest(),
                "https://id.example", "switch-subject",
            )
        browser_nonce = "attempt-browser"
        first = dict(
            action="link", browser_nonce=browser_nonce,
            account_id=admin["id"], auth_version=admin["auth_version"], target="",
        )
        async with pools.control.connection() as conn:
            assert await start_oidc_attempt(conn, state="old-state", nonce="old-nonce", **first)
            assert await start_oidc_attempt(conn, state="new-state", nonce="new-nonce", **first)
            assert await consume_oidc_attempt(
                conn, action="link", state="old-state", nonce="old-nonce",
                browser_nonce=browser_nonce, account_id=admin["id"],
                auth_version=admin["auth_version"],
            ) is None
            assert await consume_oidc_attempt(
                conn, action="link", state="new-state", nonce="new-nonce",
                browser_nonce=browser_nonce, account_id=member_id, auth_version=1,
            ) is None
            current = await consume_oidc_attempt(
                conn, action="link", state="new-state", nonce="new-nonce",
                browser_nonce=browser_nonce, account_id=admin["id"],
                auth_version=admin["auth_version"],
            )
            assert current is not None
            assert current["account_id"] == admin["id"]

            assert await start_oidc_attempt(conn, state="expired-state", nonce="expired-nonce", **first)
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE oidc_attempts SET created_at=pg_catalog.clock_timestamp()-interval '2 minutes', "
                "expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE state_digest=%s", (hashlib.sha256(b"expired-state").hexdigest(),),
            )
        async with pools.control.connection() as conn:
            assert await consume_oidc_attempt(
                conn, action="link", state="expired-state", nonce="expired-nonce",
                browser_nonce=browser_nonce, account_id=admin["id"],
                auth_version=admin["auth_version"],
            ) is None
    asyncio.run(_scenario(check))


def test_oidc_attempts_and_reauth_proofs_are_one_use():
    async def check(owner, pools, admin):
        async with pools.control.connection() as conn:
            identity = await create_identity_link(conn, admin["id"], "https://id.example", "admin-subject")
            assert identity is not None
            kwargs = dict(action="reauth", state="state-2", nonce="nonce-2",
                          browser_nonce="browser-2", account_id=admin["id"],
                          auth_version=admin["auth_version"], proof_action="verify_current",
                          target=admin["email"])
            assert await start_oidc_attempt(conn, **kwargs)
            assert not await finish_oidc_reauth(
                conn, state="state-2", nonce="wrong", browser_nonce="browser-2",
                account_id=admin["id"], auth_version=admin["auth_version"],
                issuer="https://id.example", subject="admin-subject",
                auth_time=datetime.now(timezone.utc),
            )
            assert not await finish_oidc_reauth(
                conn, state="state-2", nonce="nonce-2", browser_nonce="browser-2",
                account_id=admin["id"], auth_version=admin["auth_version"],
                issuer="https://id.example", subject="wrong-subject",
                auth_time=datetime.now(timezone.utc),
            )
            assert await start_oidc_attempt(conn, **kwargs)
            assert not await finish_oidc_reauth(
                conn, state="state-2", nonce="nonce-2", browser_nonce="browser-2",
                account_id=admin["id"], auth_version=admin["auth_version"],
                issuer="https://id.example", subject="admin-subject",
                auth_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            assert await start_oidc_attempt(conn, **kwargs)
            assert await finish_oidc_reauth(
                conn, state="state-2", nonce="nonce-2", browser_nonce="browser-2",
                account_id=admin["id"], auth_version=admin["auth_version"],
                issuer="https://id.example", subject="admin-subject",
                auth_time=datetime.now(timezone.utc),
            )
            assert not await consume_action_proof(
                conn, account_id=admin["id"], auth_version=admin["auth_version"],
                action="verify_current", target="different@example.invalid",
                browser_nonce="browser-2",
            )
            assert await consume_action_proof(
                conn, account_id=admin["id"], auth_version=admin["auth_version"],
                action="verify_current", target=admin["email"], browser_nonce="browser-2",
            )
            assert not await consume_action_proof(
                conn, account_id=admin["id"], auth_version=admin["auth_version"],
                action="verify_current", target=admin["email"], browser_nonce="browser-2",
            )
            assert await start_oidc_attempt(conn, **kwargs)
            await conn.commit()
            async with owner.connection() as raw:
                await raw.execute("UPDATE accounts SET auth_version=auth_version+1 WHERE id=%s", (admin["id"],))
            assert not await finish_oidc_reauth(
                conn, state="state-2", nonce="nonce-2", browser_nonce="browser-2",
                account_id=admin["id"], auth_version=admin["auth_version"],
                issuer="https://id.example", subject="admin-subject",
                auth_time=datetime.now(timezone.utc),
            )
    asyncio.run(_scenario(check))


def test_oidc_invitation_rechecks_issuer_and_exact_identity():
    async def check(owner, pools, admin):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            assert await create_identity_link(conn, admin["id"], "https://id.example", "used-subject")
            token = await issue_invitation(conn, dict(admin, is_admin=True), "member@example.invalid")
            assert await start_oidc_attempt(
                conn, action="invite", state="state-3", nonce="nonce-3",
                browser_nonce="browser-3", invite_token=token, target="UTC",
            )
            pending = await consume_oidc_attempt(
                conn, action="invite", state="state-3", nonce="nonce-3",
                browser_nonce="browser-3",
            )
            with pytest.raises(InvitationUnavailable):
                await redeem_oidc_invitation_by_digest(
                    conn, pending["invitation_digest"], "https://id.example", "used-subject",
                )
        async with owner.connection() as conn:
            assert (await (await conn.execute(
                "SELECT count(*) FROM accounts WHERE email='member@example.invalid'"
            )).fetchone()) == (0,)
            assert (await (await conn.execute(
                "SELECT consumed_at FROM invitations WHERE email='member@example.invalid'"
            )).fetchone()) == (None,)
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (admin["id"],))
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                await redeem_oidc_invitation_by_digest(
                    conn, pending["invitation_digest"], "https://id.example", "new-subject",
                )
        async with owner.connection() as conn:
            assert (await (await conn.execute(
                "SELECT consumed_at FROM invitations WHERE email='member@example.invalid'"
            )).fetchone()) == (None,)
    asyncio.run(_scenario(check))


def test_host_recovery_establishes_password_without_removing_oidc_identity():
    async def check(owner, pools, admin):
        await _allow_member_accounts(owner)
        async with pools.control.connection() as conn:
            token = await issue_invitation(
                conn, dict(admin, is_admin=True), "recovery@example.invalid",
            )
            member_id = await redeem_oidc_invitation_by_digest(
                conn, hashlib.sha256(token.encode()).hexdigest(),
                "https://id.example", "recovery-subject",
            )
            password_hash = hash_password("recovered password")
            recovered = await host_reset_password(conn, member_id, password_hash)
            assert recovered is not None
            assert recovered["auth_version"] == 2
        async with owner.connection() as conn:
            account = await (await conn.execute(
                "SELECT password_hash, auth_version, email_verified_at "
                "FROM accounts WHERE id=%s", (member_id,),
            )).fetchone()
            identity = await (await conn.execute(
                "SELECT issuer, subject FROM oidc_identities WHERE account_id=%s",
                (member_id,),
            )).fetchone()
        assert verify_password("recovered password", account[0])
        assert account[1:] == (2, None)
        assert identity == ("https://id.example", "recovery-subject")
    asyncio.run(_scenario(check))
