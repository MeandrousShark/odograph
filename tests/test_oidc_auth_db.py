from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import RedirectResponse

import app.auth as auth_module
from app.auth import AuthRedirect, make_router, require_user, require_legacy_establishment
from tests.auth_db_fixtures import auth_config, bind_auth_test_roles, seed_auth_account
from app.db import make_pool
from app.ingest import FailedAuthLimiter
from app.local_auth import hash_password
from app.main import make_templates
from app.oidc_identities import IdentityLinkRejectedError
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)
TZ = ZoneInfo("UTC")


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route missing")


class _OAuthClient:
    def __init__(self, userinfo=None):
        self.userinfo = userinfo or {}
        self.authorize_kwargs = None
        self.token_calls = 0

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        self.authorize_kwargs = kwargs
        return RedirectResponse("https://idp.example/authorize", status_code=303)

    async def authorize_access_token(self, request):
        self.token_calls += 1
        return {"userinfo": self.userinfo}


def _request(
    pool,
    oauth_client,
    *,
    session=None,
    query_params=None,
    allowed_email="",
    initial_signup=False,
):
    control_pool = getattr(pool, "control_pool", pool)
    runtime_pool = getattr(pool, "runtime_pool", pool)
    cfg = auth_config(TEST_DB,
        dev_no_auth=False,
        initial_admin_signup=initial_signup,
        allowed_email=allowed_email,
        oidc_client_id="test-client", oidc_client_secret="test-secret",
        oidc_issuer="https://idp.example/",
    )
    return SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                pool=control_pool, control_pool=control_pool, runtime_pool=runtime_pool,
                make_detector_runner=lambda bound: SimpleNamespace(pool=bound),
                config=cfg,
                oauth=SimpleNamespace(pocketid=oauth_client),
                templates=make_templates(
                    cfg
                ),
                login_limiter=FailedAuthLimiter(5, 900.0),
            )
        ),
        session=session if session is not None else {},
        query_params=query_params if query_params is not None else {},
        client=SimpleNamespace(host="203.0.113.9"),
        url_for=lambda name: "https://app.example/auth/callback",
    )


async def _create_account(pool, password="local password"):
    async with pool.connection() as conn:
        await seed_auth_account(conn, email="local@example.com", password_hash=hash_password(password))
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT * FROM accounts WHERE id=1")
        return await cur.fetchone()


async def _identity(pool):
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT * FROM oidc_identities")
        return await cur.fetchone()


async def _account(pool):
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT * FROM accounts")
        return await cur.fetchone()


async def _link_and_exact_login_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        roles = await bind_auth_test_roles(pool)
        async with roles.control.connection() as conn:
            assert (await (await conn.execute("SELECT session_user")).fetchone())[0] == "odograph_control"
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT * FROM trips")
        account = await _create_account(pool)
        oauth_client = _OAuthClient(
            {
                "sub": "stable-subject",
                "email": "different-provider@example.net",
                "name": "Provider User",
            }
        )
        request = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 1, "csrf": "csrf"},
        )
        user = {
            "id": 1,
            "name": "local",
            "email": "local@example.com",
            "is_admin": True,
            "legacy_oidc": False,
        }
        await require_user(request)
        response = await _endpoint("/settings/account/oidc/link", "POST")(
            request,
            current_password="local password",
            csrf_token="csrf",
            user=user,
        )
        assert response.status_code == 303
        state = oauth_client.authorize_kwargs["state"]
        assert state.startswith("link.")
        assert oauth_client.authorize_kwargs["nonce"]

        old_link_session = _request(
            pool, oauth_client,
            session={"account_id": 1, "auth_version": 1, "csrf": "other"},
        )
        request.query_params = {"state": state, "code": "valid"}
        response = await _endpoint("/auth/callback", "GET")(request)
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/account"
        linked = await _identity(pool)
        assert linked["issuer"] == "https://idp.example"
        assert linked["subject"] == "stable-subject"
        assert linked["provider_email"] == "different-provider@example.net"
        assert (await _account(pool))["email"] == "local@example.com"
        assert (await _account(pool))["auth_version"] == 2
        assert request.session["auth_version"] == 2
        with pytest.raises(AuthRedirect):
            await require_user(old_link_session)
        account_page = await _endpoint("/settings/account", "GET")(
            request, user=user
        )
        assert b"different-provider@example.net" in account_page.body
        assert b"Provider User" in account_page.body
        assert b"stable-subject" not in account_page.body

        with pytest.raises(Exception) as replay:
            await _endpoint("/auth/callback", "GET")(request)
        assert replay.value.status_code == 401
        assert oauth_client.token_calls == 1
        assert (await _identity(pool))["subject"] == "stable-subject"

        oauth_client.userinfo = {
            "sub": "stable-subject",
            "email": "changed-provider@example.net",
            "name": "Changed Provider Name",
        }
        login_request = _request(pool, oauth_client, session={"csrf": "old"})
        start = await _endpoint("/login/oidc", "GET")(login_request)
        assert start.status_code == 303
        login_request.query_params = {
            "state": oauth_client.authorize_kwargs["state"],
            "code": "valid",
        }
        logged_in = await _endpoint("/auth/callback", "GET")(login_request)
        assert logged_in.status_code == 303
        assert login_request.session["account_id"] == account["id"]
        assert login_request.session["auth_version"] == 2
        assert (await _account(pool))["email"] == "local@example.com"
        assert (await _identity(pool))["provider_email"] == "changed-provider@example.net"
    finally:
        await pool.close()


def test_password_reauthenticated_link_and_exact_identity_login_share_account():
    asyncio.run(_link_and_exact_login_scenario())


async def _link_and_unlink_failures_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await bind_auth_test_roles(pool)
        await _create_account(pool)
        oauth_client = _OAuthClient({"sub": "subject-1", "email": "local@example.com"})
        user = {
            "id": 1,
            "name": "local",
            "email": "local@example.com",
            "is_admin": True,
            "legacy_oidc": False,
        }

        wrong = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 1, "csrf": "csrf"},
        )
        await require_user(wrong)
        response = await _endpoint("/settings/account/oidc/link", "POST")(
            wrong,
            current_password="wrong password",
            csrf_token="csrf",
            user=user,
        )
        assert response.status_code == 401
        assert oauth_client.authorize_kwargs is None
        assert await _identity(pool) is None

        link_request = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 1, "csrf": "csrf"},
        )
        await require_user(link_request)
        await _endpoint("/settings/account/oidc/link", "POST")(
            link_request,
            current_password="local password",
            csrf_token="csrf",
            user=user,
        )
        link_request.query_params = {"state": oauth_client.authorize_kwargs["state"]}
        await _endpoint("/auth/callback", "GET")(link_request)

        duplicate = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 2, "csrf": "csrf"},
        )
        await require_user(duplicate)
        duplicate_response = await _endpoint("/settings/account/oidc/link", "POST")(
            duplicate,
            current_password="local password",
            csrf_token="csrf",
            user=user,
        )
        assert duplicate_response.status_code == 409
        assert (await _identity(pool))["subject"] == "subject-1"

        no_confirm = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 2, "csrf": "csrf"},
        )
        await require_user(no_confirm)
        response = await _endpoint("/settings/account/oidc/unlink", "POST")(
            no_confirm,
            current_password="local password",
            csrf_token="csrf",
            confirm_unlink=None,
            user=user,
        )
        assert response.status_code == 400
        assert await _identity(pool) is not None

        wrong_password = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 2, "csrf": "csrf"},
        )
        await require_user(wrong_password)
        response = await _endpoint("/settings/account/oidc/unlink", "POST")(
            wrong_password,
            current_password="wrong password",
            csrf_token="csrf",
            confirm_unlink="yes",
            user=user,
        )
        assert response.status_code == 401
        assert (await _account(pool))["auth_version"] == 2

        old_session = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 2, "csrf": "other"},
        )
        unlink_request = _request(
            pool,
            oauth_client,
            session={"account_id": 1, "auth_version": 2, "csrf": "csrf"},
        )
        await require_user(unlink_request)
        response = await _endpoint("/settings/account/oidc/unlink", "POST")(
            unlink_request,
            current_password="local password",
            csrf_token="csrf",
            confirm_unlink="yes",
            user=user,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/account"
        assert unlink_request.session["account_id"] == 1
        assert unlink_request.session["auth_version"] == 3
        assert await _identity(pool) is None
        assert (await _account(pool))["auth_version"] == 3
        with pytest.raises(AuthRedirect):
            await require_user(old_session)

        local_request = _request(pool, oauth_client, session={"csrf": "fresh"})
        local_response = await _endpoint("/login/local", "POST")(
            local_request,
            email="local@example.com",
            password="local password",
            csrf_token="fresh",
        )
        assert local_response.status_code == 303
        assert local_request.session["auth_version"] == 3
    finally:
        await pool.close()


def test_link_failures_and_confirmed_unlink_fail_closed_and_revoke_sessions():
    asyncio.run(_link_and_unlink_failures_scenario())


async def _legacy_establishment_and_email_fallback_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await bind_auth_test_roles(pool)
        oauth_client = _OAuthClient()
        legacy_session = {
            "legacy_oidc": {
                "issuer": "https://idp.example",
                "subject": "legacy-subject",
                "email": "provider@example.net",
                "name": "Legacy User",
            },
            "csrf": "csrf",
        }
        request = _request(pool, oauth_client, session=legacy_session)
        legacy_user = await require_legacy_establishment(request)
        page = await _endpoint("/account/establish", "GET")(
            request, user=legacy_user
        )
        assert page.status_code == 200
        assert b'value="provider@example.net"' in page.body

        response = await _endpoint("/account/establish", "POST")(
            request,
            email="different-local@example.com",
            password="new local password",
            password_confirm="new local password",
            display_timezone="UTC",
            csrf_token="csrf",
            user=legacy_user,
        )
        assert response.status_code == 303
        assert request.session["account_id"] == 1
        account = await _account(pool)
        linked = await _identity(pool)
        assert account["email"] == "different-local@example.com"
        assert linked["subject"] == "legacy-subject"
        assert linked["provider_email"] == "provider@example.net"

        oauth_client.userinfo = {
            "sub": "unlinked-subject",
            "email": "different-local@example.com",
        }
        unlinked = _request(
            pool,
            oauth_client,
            query_params={"state": "login.unlinked", "code": "valid"},
        )
        with pytest.raises(Exception) as rejected:
            await _endpoint("/auth/callback", "GET")(unlinked)
        assert rejected.value.status_code == 401
        assert "account_id" not in unlinked.session
        assert (await _identity(pool))["subject"] == "legacy-subject"
    finally:
        await pool.close()


def test_legacy_establishment_links_atomically_without_email_based_authentication():
    asyncio.run(_legacy_establishment_and_email_fallback_scenario())


async def _failed_legacy_establishment_scenario(monkeypatch):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await bind_auth_test_roles(pool)
        oauth_client = _OAuthClient()
        request = _request(
            pool,
            oauth_client,
            session={
                "legacy_oidc": {
                    "issuer": "https://idp.example",
                    "subject": "legacy-subject",
                    "email": "provider@example.net",
                    "name": "Legacy User",
                },
                "csrf": "csrf",
            },
        )
        legacy_user = await require_legacy_establishment(request)

        async def reject(*args, **kwargs):
            raise IdentityLinkRejectedError()

        monkeypatch.setattr(auth_module, "establish_legacy_admin_identity", reject)
        response = await _endpoint("/account/establish", "POST")(
            request,
            email="local@example.com",
            password="new local password",
            password_confirm="new local password",
            display_timezone="UTC",
            csrf_token="csrf",
            user=legacy_user,
        )
        assert response.status_code == 409
        assert request.session["legacy_oidc"]["subject"] == "legacy-subject"
        assert (await require_legacy_establishment(request))["legacy_oidc"] is True
        assert await _account(pool) is None
        assert await _identity(pool) is None
    finally:
        await pool.close()


def test_failed_legacy_establishment_keeps_legacy_access(monkeypatch):
    asyncio.run(_failed_legacy_establishment_scenario(monkeypatch))


async def _no_provider_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await bind_auth_test_roles(pool)
        await _create_account(pool)
        request = _request(
            pool,
            _OAuthClient(),
            session={"account_id": 1, "auth_version": 1, "csrf": "csrf"},
        )
        request.app.state.oauth = None
        request.app.state.config.oidc_issuer = ""
        user = {
            "id": 1,
            "name": "local",
            "email": "local@example.com",
            "is_admin": True,
            "legacy_oidc": False,
        }
        user = await require_user(request)
        page = await _endpoint("/settings/account", "GET")(request, user=user)
        assert page.status_code == 200
        assert b"No sign-in provider is configured" in page.body
        assert b"/settings/account/oidc/link" not in page.body

        with pytest.raises(Exception) as link_error:
            await _endpoint("/settings/account/oidc/link", "POST")(
                request,
                current_password="local password",
                csrf_token="csrf",
                user=user,
            )
        assert link_error.value.status_code == 404

        changed = await _endpoint("/settings/account/password", "POST")(
            request,
            current_password="local password",
            password="changed password",
            password_confirm="changed password",
            csrf_token="csrf",
            user=user,
        )
        assert changed.status_code == 200
    finally:
        await pool.close()


def test_account_security_and_local_password_work_without_oidc_provider():
    asyncio.run(_no_provider_scenario())
