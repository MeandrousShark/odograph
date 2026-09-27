"""Full create_app acceptance: first-admin bootstrap, invitation onboarding, recovery.

Everything here goes through real routes on the application built by
create_app, so the real restricted role pools, RecoveryQueue and Mailer are in
play. Only the SMTP socket is replaced, by a capture of what the real Mailer
would have sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from contextlib import AsyncExitStack

import httpx
import psycopg
import pytest

import app.main as main_module
from app.config import Config
from app.db import make_pool
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

ADMIN_EMAIL = "acceptance-admin@example.invalid"
VERIFIED_EMAIL = "acceptance-verified@example.invalid"
UNVERIFIED_EMAIL = "acceptance-unverified@example.invalid"
ADMIN_PASSWORD = "administrator password"
VERIFIED_PASSWORD = "verified member password"
UNVERIFIED_PASSWORD = "unverified member password"
RESET_PASSWORD = "replacement member password"
LINK_BASE = "https://odograph.example.invalid"


class _CaptureSMTP:
    """Stands in for smtplib.SMTP so the real Mailer runs end to end."""

    sent: list = []

    def __init__(self, host, port=0, timeout=None):
        assert host == "smtp.example.invalid"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def starttls(self, context=None):
        assert context is not None

    def login(self, *_args):
        raise AssertionError("no SMTP credentials are configured")

    def send_message(self, message):
        type(self).sent.append(message)


def _form_csrf(page: httpx.Response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


async def _wait_for(predicate, what: str) -> None:
    for _ in range(250):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _signed_in(client) -> bool:
    return (await client.get("/settings/account")).status_code == 200


async def _login(client, email: str, password: str) -> httpx.Response:
    page = await client.get("/login")
    return await client.post("/login/local", data={
        "email": email, "password": password, "csrf_token": _form_csrf(page)})


async def _redeem(client, token: str, password: str) -> httpx.Response:
    page = await client.get("/invite")
    return await client.post("/invite", data={
        "token": token, "password": password, "password_confirm": password,
        "display_timezone": "UTC", "csrf_token": _form_csrf(page)})


def _mail(subject: str, to: str | None = None) -> list:
    return [m for m in _CaptureSMTP.sent
            if m["Subject"] == subject and (to is None or m["To"] == to)]


def _shown_invitation_token(response: httpx.Response) -> str:
    return re.search(r'id="admin-invitation-token" value="([^"]+)"', response.text).group(1)


async def _account_id(owner, email: str) -> int:
    async with owner.connection() as conn:
        return (await (await conn.execute(
            "SELECT id FROM accounts WHERE email=%s", (email,))).fetchone())[0]


async def _durable_security_text_containing(owner, secret: str) -> int:
    """Rows in the durable security tables that hold `secret` in any column."""
    async with owner.connection() as conn:
        return (await (await conn.execute(
            "SELECT count(*) FROM ("
            " SELECT row_to_json(t)::text AS j FROM account_security_audit t"
            " UNION ALL SELECT row_to_json(t)::text FROM email_challenges t"
            " UNION ALL SELECT row_to_json(t)::text FROM invitations t"
            " UNION ALL SELECT row_to_json(t)::text FROM accounts t) x "
            "WHERE position(%s in j) > 0", (secret,))).fetchone())[0]


async def _scenario(owner, secrets_seen: list[str], unverified_processed: asyncio.Event) -> None:
    app = main_module.create_app(Config.from_env())
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))

        async def new_client():
            return await stack.enter_async_context(httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://test"))

        # The application runs on the real restricted roles, neither of which
        # can bypass row security or read personal ledger rows.
        for pool, role in ((app.state.control_pool, "odograph_control"),
                           (app.state.runtime_pool, "odograph_runtime")):
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT session_user, r.rolsuper OR r.rolbypassrls "
                    "FROM pg_roles r WHERE r.rolname = session_user")).fetchone()) == (role, False)
        async with app.state.control_pool.connection() as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT * FROM trips")

        admin1, admin2, member1, member2, other, anonymous = [
            await new_client() for _ in range(6)]

        # First-admin bootstrap closes after the first account.
        page = await admin1.get("/signup")
        assert (await admin1.post("/signup", data={
            "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD, "csrf_token": _form_csrf(page),
            "display_timezone": "UTC"})).status_code == 303
        assert (await anonymous.get("/signup")).status_code == 404
        assert (await anonymous.post("/signup", data={
            "email": "late@example.invalid", "password": "late password 1",
            "password_confirm": "late password 1", "csrf_token": "x"})).status_code == 404
        admin_id = await _account_id(owner, ADMIN_EMAIL)
        async with owner.connection() as conn:
            for is_admin, error in ((True, psycopg.errors.UniqueViolation),
                                    (False, psycopg.errors.CheckViolation)):
                with pytest.raises(error):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO accounts(email,password_hash,is_admin) "
                            "VALUES ('second@example.invalid','x',%s)", (is_admin,))
        assert (await _login(admin2, ADMIN_EMAIL, ADMIN_PASSWORD)).status_code == 303

        # The singleton refuses invitations until the separate activation gate.
        admin_csrf = _form_csrf(await admin1.get("/settings/account"))
        gated = await admin1.post("/admin/invitations", data={
            "csrf_token": admin_csrf, "email": VERIFIED_EMAIL, "send_email": "1"})
        assert gated.status_code == 409
        assert not _CaptureSMTP.sent

        # Test-only stand-in for the activation gate, as in the other
        # acceptance tests: remove the singleton guards on this disposable DB.
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")

        # One invitation is emailed, the other is handed over manually.
        emailed = await admin1.post("/admin/invitations", data={
            "csrf_token": admin_csrf, "email": VERIFIED_EMAIL, "send_email": "1"})
        assert emailed.status_code == 200
        assert "accepted the invitation email" in emailed.text
        (invite_mail,) = _mail("Invitation to Odograph")
        assert invite_mail["To"] == VERIFIED_EMAIL
        shown_token = _shown_invitation_token(emailed)
        mailed_token = re.search(rf"{LINK_BASE}/invite#token=([A-Za-z0-9_-]+)\n",
                                 invite_mail.get_content()).group(1)
        assert mailed_token == shown_token
        manual = await admin1.post("/admin/invitations", data={
            "csrf_token": admin_csrf, "email": UNVERIFIED_EMAIL})
        assert manual.status_code == 200
        assert len(_mail("Invitation to Odograph")) == 1
        manual_token = _shown_invitation_token(manual)
        secrets_seen += [mailed_token, manual_token]

        assert (await _redeem(member1, mailed_token, VERIFIED_PASSWORD)).status_code == 303
        assert (await _redeem(other, manual_token, UNVERIFIED_PASSWORD)).status_code == 303
        assert (await _redeem(anonymous, mailed_token, "another password")).status_code == 400
        assert (await _login(member2, VERIFIED_EMAIL, VERIFIED_PASSWORD)).status_code == 303
        member_id = await _account_id(owner, VERIFIED_EMAIL)
        unverified_id = await _account_id(owner, UNVERIFIED_EMAIL)

        # An invitation, emailed or not, does not verify the address.
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT email, email_verified_at IS NULL, is_admin FROM accounts "
                "WHERE id IN (%s,%s) ORDER BY id", (member_id, unverified_id))).fetchall() == [
                (VERIFIED_EMAIL, True, False), (UNVERIFIED_EMAIL, True, False)]
        listing = (await admin1.get("/admin/accounts")).text
        assert f"/admin/accounts/{member_id}/recovery" not in listing
        assert f"/admin/accounts/{unverified_id}/recovery" not in listing

        # The member proves the address with a delivered challenge.
        csrf = _form_csrf(await member1.get("/settings/account"))
        requested = await member1.post("/settings/account/email/verify/request", data={
            "current_password": VERIFIED_PASSWORD, "csrf_token": csrf})
        assert requested.status_code == 200
        (verify_mail,) = _mail("Confirm your Odograph email address")
        assert verify_mail["To"] == VERIFIED_EMAIL
        verify_token = re.search(r"enter this code manually: ([A-Za-z0-9_-]{43})",
                                 verify_mail.get_content()).group(1)
        secrets_seen.append(verify_token)
        confirmed = await member1.post("/settings/account/email/confirm", data={
            "purpose": "verify_current", "token": verify_token, "csrf_token": csrf})
        assert confirmed.status_code == 200
        assert "Email address confirmed." in confirmed.text
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT email_verified_at IS NOT NULL FROM accounts WHERE id=%s",
                (member_id,))).fetchone() == (True,)
            versions_before = dict(await (await conn.execute(
                "SELECT id, auth_version FROM accounts")).fetchall())
        listing = (await admin1.get("/admin/accounts")).text
        assert f"/admin/accounts/{member_id}/recovery" in listing
        assert f"/admin/accounts/{unverified_id}/recovery" not in listing

        sessions = {"admin1": admin1, "admin2": admin2, "member1": member1,
                    "member2": member2, "other": other}
        assert {name: await _signed_in(c) for name, c in sessions.items()} == {
            name: True for name in sessions}

        # A member cannot start recovery for anyone.
        member_csrf = _form_csrf(await other.get("/settings/account"))
        denied = await other.post(f"/admin/accounts/{member_id}/recovery",
                                  data={"csrf_token": member_csrf})
        assert denied.status_code == 403

        # Unverified target: no queue entry, mail or proof, by admin or public request.
        mails_before = len(_CaptureSMTP.sent)
        unverified = await admin1.post(f"/admin/accounts/{unverified_id}/recovery",
                                       data={"csrf_token": admin_csrf})
        assert unverified.status_code == 200
        assert "no verified login address" in unverified.text
        page = await anonymous.get("/forgot-password")
        assert (await anonymous.post("/forgot-password", data={
            "email": UNVERIFIED_EMAIL, "csrf_token": _form_csrf(page)})).status_code == 200
        await asyncio.wait_for(unverified_processed.wait(), timeout=5)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM email_challenges WHERE account_id=%s "
                "AND purpose='reset_password'", (unverified_id,),
            )).fetchone() == (0,)

        # Verified target: one message, to the stored verified address only.
        queued = await admin1.post(f"/admin/accounts/{member_id}/recovery",
                                   data={"csrf_token": admin_csrf})
        assert queued.status_code == 200
        assert "Password recovery was queued" in queued.text
        await _wait_for(lambda: _mail("Reset your Odograph password"), "the reset message")
        await app.state.security_mail.drain()
        (reset_mail,) = _mail("Reset your Odograph password")
        assert len(_CaptureSMTP.sent) == mails_before + 1
        assert reset_mail["To"] == VERIFIED_EMAIL
        body = reset_mail.get_content()
        token = re.search(rf"{LINK_BASE}/reset-password#token=([A-Za-z0-9_-]{{43}})\n", body).group(1)
        secrets_seen.append(token)

        # The token reaches only the mailbox, never the admin or durable state.
        admin_views = [queued.text, (await admin1.get("/admin/accounts")).text,
                       (await admin2.get("/admin/accounts")).text]
        assert all(token not in view for view in admin_views)
        assert await _durable_security_text_containing(owner, token) == 0
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT account_id, initiator, target_email, consumed_at IS NULL, "
                "revoked_at IS NULL FROM email_challenges WHERE token_digest=%s",
                (hashlib.sha256(token.encode()).hexdigest(),))).fetchall() == [
                (member_id, "admin", VERIFIED_EMAIL, True, True)]
            assert await (await conn.execute(
                "SELECT count(*) FROM email_challenges WHERE account_id IN (%s,%s) "
                "AND purpose='reset_password'", (unverified_id, admin_id))).fetchone() == (0,)
        assert not _mail("Reset your Odograph password", UNVERIFIED_EMAIL)

        # Issuing a reset changes nothing until the proof is used.
        assert all([await _signed_in(c) for c in sessions.values()])

        # Redeeming from a browser with no session ends only the target's sessions.
        page = await anonymous.get("/reset-password")
        reset = await anonymous.post("/reset-password", data={
            "token": token, "password": RESET_PASSWORD,
            "password_confirm": RESET_PASSWORD, "csrf_token": _form_csrf(page)})
        assert reset.status_code == 303
        assert reset.headers["location"] == "/login?signed_out=1"
        assert not await _signed_in(member1)
        assert not await _signed_in(member2)
        assert await _signed_in(other)
        assert await _signed_in(admin1)
        assert await _signed_in(admin2)
        async with owner.connection() as conn:
            versions_after = dict(await (await conn.execute(
                "SELECT id, auth_version FROM accounts")).fetchall())
        assert versions_after[member_id] == versions_before[member_id] + 1
        assert {k: v for k, v in versions_after.items() if k != member_id} == {
            k: v for k, v in versions_before.items() if k != member_id}

        # The proof is single-use, and only the new password works.
        page = await anonymous.get("/reset-password")
        replay = await anonymous.post("/reset-password", data={
            "token": token, "password": "third member password",
            "password_confirm": "third member password", "csrf_token": _form_csrf(page)})
        assert replay.status_code == 400
        assert (await _login(member1, VERIFIED_EMAIL, VERIFIED_PASSWORD)).status_code == 401
        assert (await _login(member1, VERIFIED_EMAIL, "third member password")).status_code == 401
        assert (await _login(member1, VERIFIED_EMAIL, RESET_PASSWORD)).status_code == 303
        assert await _signed_in(member1)
        assert await _signed_in(other)

        # Recovery never mailed the unverified address, before or after redemption.
        assert not _mail("Reset your Odograph password", UNVERIFIED_EMAIL)
        assert len(_mail("Reset your Odograph password")) == 1
        secrets_seen += [ADMIN_PASSWORD, VERIFIED_PASSWORD, UNVERIFIED_PASSWORD, RESET_PASSWORD]


def test_full_app_bootstrap_onboarding_and_admin_recovery_keep_proofs_private(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "disposable-session-secret")
    monkeypatch.setenv("INITIAL_ADMIN_SIGNUP", "1")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_SECURITY", "starttls")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SMTP_TLS_INSECURE", "0")
    monkeypatch.setenv("EMAIL_FROM", "odograph@example.invalid")
    monkeypatch.setenv("APP_URL", LINK_BASE)
    for name in ("EMAIL_TO", "OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL",
                 "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("smtplib.SMTP", _CaptureSMTP)
    monkeypatch.setattr(_CaptureSMTP, "sent", [])
    secrets_seen: list[str] = []

    async def run():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            async with owner.connection() as conn:
                await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
            await full_schema_reset(owner)
            unverified_processed = asyncio.Event()
            from app import password_reset

            original_issue = password_reset.issue_password_reset

            async def observe_unverified_request(conn, token, **kwargs):
                address = await original_issue(conn, token, **kwargs)
                if kwargs.get("email") == UNVERIFIED_EMAIL:
                    unverified_processed.set()
                return address

            monkeypatch.setattr(password_reset, "issue_password_reset", observe_unverified_request)
            await _scenario(owner, secrets_seen, unverified_processed)
        finally:
            await full_schema_reset(owner)
            await owner.close()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert len(secrets_seen) == 8
    for secret in secrets_seen:
        assert secret not in caplog.text
