"""Real-auth password reset across two accounts and several sessions."""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx
import pytest

import app.main as main_module
from app.account_context import AccountPool, AccountPrincipal
from app.config import Config
from app.db import make_pool
from app.mailer import Mailer
from app.oidc_identities import create_identity_link
from app.tracking import create_device
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

A_EMAIL = "reset-owner@example.invalid"
B_EMAIL = "reset-member@example.invalid"
OLD_PASSWORD = "original password"
NEW_PASSWORD = "replacement password"


def _csrf(page: httpx.Response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def _without_nonce(html: str) -> str:
    return re.sub(r'nonce="[^"]+"', 'nonce=""', html)


async def _sign_in(client, email, password):
    page = await client.get("/login")
    return await client.post("/login/local", data={
        "email": email, "password": password, "csrf_token": _csrf(page)})


async def _signed_in(client) -> bool:
    response = await client.get("/settings/account")
    return response.status_code == 200


def test_reset_ends_only_the_target_accounts_sessions(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "disposable-session-secret")
    monkeypatch.setenv("INITIAL_ADMIN_SIGNUP", "1")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("EMAIL_FROM", "odograph@example.invalid")
    monkeypatch.setenv("APP_URL", "https://odograph.example.invalid")
    for name in ("EMAIL_TO", "OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL",
                 "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    delivered = []

    def capture(mailer, message):
        delivered.append(message)

    def captured_mailer(*args, **kwargs):
        return Mailer(*args, **kwargs, transport=capture)

    monkeypatch.setattr(main_module, "Mailer", captured_mailer)

    async def run():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            async with owner.connection() as conn:
                await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
            await full_schema_reset(owner)
            app = main_module.create_app(Config.from_env())
            async with app.router.lifespan_context(app):
                def client():
                    return httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="https://test")

                async with client() as a1, client() as a2, client() as b, client() as anonymous:
                    page = await a1.get("/signup")
                    assert (await a1.post("/signup", data={
                        "email": A_EMAIL, "password": OLD_PASSWORD, "password_confirm": OLD_PASSWORD,
                        "csrf_token": _csrf(page), "display_timezone": "UTC"})).status_code == 303
                    async with owner.connection() as conn:
                        a_id = (await (await conn.execute(
                            "SELECT id FROM accounts WHERE email=%s", (A_EMAIL,))).fetchone())[0]
                        # Test-only second account with the same password.
                        b_hash = (await (await conn.execute(
                            "SELECT password_hash FROM accounts WHERE id=%s", (a_id,))).fetchone())[0]
                        await conn.execute("DROP INDEX accounts_singleton_idx")
                        await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                        b_id = (await (await conn.execute(
                            "INSERT INTO accounts(email,password_hash,is_admin) VALUES (%s,%s,false) "
                            "RETURNING id", (B_EMAIL, b_hash))).fetchone())[0]
                        # The owned defaults account_bootstrap.sql gives an account.
                        await conn.execute("INSERT INTO account_settings(account_id) VALUES (%s)", (b_id,))
                        await conn.execute(
                            "INSERT INTO vehicles(account_id,name,is_default) VALUES (%s,'My Car',true)", (b_id,))
                        await conn.execute(
                            "INSERT INTO mileage_rates(account_id,year,rate_per_mi,rate_h2_per_mi,h2_start_month) "
                            "SELECT %s,year,rate_per_mi,rate_h2_per_mi,h2_start_month "
                            "FROM reference_mileage_rates", (b_id,))
                        await conn.execute("UPDATE accounts SET email_verified_at=now()")
                    async with app.state.control_pool.connection() as conn:
                        await create_identity_link(conn, a_id, "https://idp.example.invalid", "a-subject")
                    bound = AccountPool(app.state.runtime_pool, AccountPrincipal(a_id, True, 1))
                    async with bound.connection() as conn:
                        await create_device(conn, "Phone")

                    assert (await _sign_in(a2, A_EMAIL, OLD_PASSWORD)).status_code == 303
                    assert (await _sign_in(b, B_EMAIL, OLD_PASSWORD)).status_code == 303
                    assert all([await _signed_in(a1), await _signed_in(a2), await _signed_in(b)])

                    page = await anonymous.get("/forgot-password")
                    unknown = await anonymous.post("/forgot-password", data={
                        "email": "nobody@example.invalid", "csrf_token": _csrf(page)})
                    # A spoofed host never reaches the emailed link.
                    known = await anonymous.post("/forgot-password", data={
                        "email": A_EMAIL.upper(), "csrf_token": _csrf(page)},
                        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
                    assert unknown.status_code == known.status_code == 200
                    # Only the per-response script nonce may differ.
                    assert _without_nonce(unknown.text) == _without_nonce(known.text)
                    for _ in range(200):
                        if delivered:
                            break
                        await asyncio.sleep(0.02)
                    assert len(delivered) == 1
                    message = delivered[0]
                    assert message["To"] == A_EMAIL
                    body = message.get_content()
                    assert "evil.example" not in body
                    token = re.search(
                        r"https://odograph\.example\.invalid/reset-password#token=([A-Za-z0-9_-]{43})\n",
                        body).group(1)

                    page = await a1.get("/reset-password")
                    assert page.headers["cache-control"] == "no-store, private"
                    assert page.headers["referrer-policy"] == "no-referrer"
                    reset = await a1.post("/reset-password", data={
                        "token": token, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD,
                        "csrf_token": _csrf(page)})
                    assert reset.status_code == 303
                    assert reset.headers["location"] == "/login?signed_out=1"
                    login = await a1.get("/login?signed_out=1")
                    assert "Password reset." in login.text

                    assert not await _signed_in(a1)
                    assert not await _signed_in(a2)
                    assert await _signed_in(b)

                    page = await a2.get("/reset-password")
                    replay = await a2.post("/reset-password", data={
                        "token": token, "password": "third password", "password_confirm": "third password",
                        "csrf_token": _csrf(page)})
                    assert replay.status_code == 400
                    assert (await _sign_in(a2, A_EMAIL, OLD_PASSWORD)).status_code == 401
                    assert (await _sign_in(a2, A_EMAIL, NEW_PASSWORD)).status_code == 303
                    assert await _signed_in(a2)

            async with owner.connection() as conn:
                cur = await conn.execute(
                    "SELECT a.auth_version, a.email_verified_at IS NOT NULL, "
                    "(SELECT count(*) FROM oidc_identities WHERE account_id=a.id), "
                    "(SELECT count(*) FROM tracking_devices WHERE account_id=a.id) "
                    "FROM accounts a ORDER BY id")
                assert await cur.fetchall() == [(2, True, 1, 1), (1, True, 0, 0)]
            return token
        finally:
            await full_schema_reset(owner)
            await owner.close()

    with caplog.at_level(logging.DEBUG):
        token = asyncio.run(run())
    assert token not in caplog.text
