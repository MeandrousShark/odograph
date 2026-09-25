"""Restricted-role routes from OIDC invite through fresh method proof."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from authlib.integrations.base_client import OAuthError
from fastapi import HTTPException
from starlette.responses import RedirectResponse

import app.auth as auth
from app.accounts import create_admin, get_account
from app.application_roles import application_role_pools, prepare_application_roles
from app.db import make_pool
from app.ingest import FailedAuthLimiter
from app.invitations import issue_invitation
from app.local_auth import verify_password
from app.main import make_templates
from app.oidc_identities import create_identity_link
from tests.auth_db_fixtures import auth_config
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


def _endpoint(path: str, method: str):
    for route in auth.make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


class _Provider:
    def __init__(self):
        self.userinfo = {}
        self.cancel = False
        self.redirect_kwargs = None
        self.token_calls = 0

    async def authorize_redirect(self, _request, _redirect_uri, **kwargs):
        self.redirect_kwargs = kwargs
        return RedirectResponse("https://idp.example/authorize", status_code=303)

    async def authorize_access_token(self, _request):
        self.token_calls += 1
        if self.cancel:
            raise OAuthError(error="access_denied")
        return {"userinfo": self.userinfo}


def _request(pools, provider):
    config = auth_config(
        TEST_DB, dev_no_auth=False, initial_admin_signup=False,
        oidc_client_id="test-client", oidc_client_secret="test-secret",
        oidc_issuer="https://idp.example/",
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            config=config, control_pool=pools.control, runtime_pool=pools.runtime,
            oauth=SimpleNamespace(pocketid=provider), templates=make_templates(config),
            login_limiter=FailedAuthLimiter(20, 900),
            make_detector_runner=lambda pool: SimpleNamespace(pool=pool),
        )),
        state=SimpleNamespace(), session={"csrf": "csrf"}, query_params={},
        client=SimpleNamespace(host="203.0.113.9"),
        url_for=lambda _name: "https://app.example/auth/callback",
    )


async def _run_route_scenario(monkeypatch):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        async with owner.connection() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
        await full_schema_reset(owner)
        await prepare_application_roles(TEST_DB)
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                admin = await create_admin(conn, "admin@example.invalid", "existing-hash")
                await create_identity_link(conn, admin["id"], "https://idp.example", "claimed-subject")
                token = await issue_invitation(
                    conn, dict(admin, is_admin=True), "invitee@example.invalid",
                )
            provider = _Provider()
            request = _request(pools, provider)

            async def invite_form(_request, fields):
                assert fields == {"token", "display_timezone", "csrf_token"}
                return {"token": token, "display_timezone": "UTC", "csrf_token": "csrf"}

            monkeypatch.setattr(auth, "_email_form", invite_form)

            async def begin_invite():
                request.session["csrf"] = "csrf"
                response = await _endpoint("/invite/oidc", "POST")(request)
                assert response.status_code == 303
                assert provider.redirect_kwargs["state"].startswith("invite.")
                assert token not in repr(request.session)
                assert token not in response.headers["location"]
                request.query_params = {"state": provider.redirect_kwargs["state"], "code": "valid"}

            async def invite_unconsumed():
                async with owner.connection() as conn:
                    row = await (await conn.execute(
                        "SELECT consumed_at FROM invitations WHERE email='invitee@example.invalid'"
                    )).fetchone()
                    assert row == (None,)
                    count = await (await conn.execute(
                        "SELECT count(*) FROM accounts WHERE email='invitee@example.invalid'"
                    )).fetchone()
                    assert count == (0,)

            await begin_invite()
            provider.cancel = True
            with pytest.raises(HTTPException) as canceled:
                await _endpoint("/auth/callback", "GET")(request)
            assert canceled.value.status_code == 401
            await invite_unconsumed()

            await begin_invite()
            provider.cancel = False
            provider.userinfo = {"sub": "claimed-subject", "email": "invitee@example.invalid"}
            with pytest.raises(HTTPException) as collision:
                await _endpoint("/auth/callback", "GET")(request)
            assert collision.value.status_code == 401
            await invite_unconsumed()

            await begin_invite()
            provider.userinfo = {"sub": "fresh-subject", "email": "other@example.invalid"}
            onboarded = await _endpoint("/auth/callback", "GET")(request)
            assert onboarded.status_code == 303
            member_id = request.session["account_id"]
            async with pools.control.connection() as conn:
                member = await get_account(conn, member_id)
                assert member["email"] == "invitee@example.invalid"
                assert member["password_hash"] is None
            async with owner.connection() as conn:
                row = await (await conn.execute(
                    "SELECT i.subject,a.email_verified_at FROM oidc_identities i "
                    "JOIN accounts a ON a.id=i.account_id WHERE i.account_id=%s", (member_id,)
                )).fetchone()
                assert row == ("fresh-subject", None)

            user = await auth.require_user(request)
            response = await _endpoint("/settings/account/oidc/reauth", "POST")(
                request, action="add_password", target="", target_confirm="",
                csrf_token=request.session["csrf"], user=user,
            )
            assert response.status_code == 303
            assert provider.redirect_kwargs["max_age"] == 0
            assert provider.redirect_kwargs["prompt"] == "login"
            request.query_params = {"state": provider.redirect_kwargs["state"], "code": "valid"}
            provider.userinfo = {"sub": "fresh-subject", "auth_time": time.time()}
            checked = await _endpoint("/auth/callback", "GET")(request)
            assert checked.status_code == 303
            assert request.session["oidc_action_proof_nonce"]

            user = await auth.require_user(request)
            account_page = await _endpoint("/settings/account", "GET")(
                request, user=user,
            )
            assert auth.OIDC_REAUTH_NOTICE.encode() in account_page.body
            changed = await _endpoint("/settings/account/password", "POST")(
                request, current_password="", password="new local password",
                password_confirm="new local password", csrf_token=request.session["csrf"],
                user=user,
            )
            assert changed.status_code == 200
            assert auth.PASSWORD_SAVED_NOTICE.encode() in changed.body
            assert request.session["auth_version"] == 2
            assert "oidc_action_proof_nonce" not in request.session
            async with pools.control.connection() as conn:
                account = await get_account(conn, member_id)
                assert verify_password("new local password", account["password_hash"])
                assert account["auth_version"] == 2
            async with owner.connection() as conn:
                assert await (await conn.execute(
                    "SELECT count(*) FROM oidc_action_proofs WHERE account_id=%s", (member_id,)
                )).fetchone() == (0,)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


def test_restricted_oidc_invite_cancel_collision_and_fresh_add_password(monkeypatch):
    asyncio.run(_run_route_scenario(monkeypatch))
