from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Match

from app.auth import make_router


def _first_matching_route(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path}
    for route in make_router().routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_account_auth_routes_resolve_and_setup_is_removed():
    expected = {
        ("GET", "/login"),
        ("POST", "/login/local"),
        ("GET", "/signup"),
        ("POST", "/signup"),
        ("GET", "/login/oidc"),
        ("GET", "/auth/callback"),
        ("GET", "/account/establish"),
        ("POST", "/account/establish"),
        ("GET", "/account/avatar"),
        ("GET", "/settings/account"),
        ("POST", "/settings/account/password"),
        ("POST", "/settings/account/oidc/link"),
        ("POST", "/settings/account/oidc/unlink"),
        ("POST", "/settings/account/avatar"),
        ("POST", "/settings/account/avatar/remove"),
        ("POST", "/logout"),
    }
    for method, path in expected:
        assert _first_matching_route(method, path).path == path
    assert _first_matching_route("GET", "/setup") is None
    assert _first_matching_route("POST", "/setup") is None


def test_plain_auth_forms_use_hidden_form_csrf_checks():
    routes = {
        route.path: route
        for route in make_router().routes
        if route.path in {
            "/login/local",
            "/signup",
            "/account/establish",
            "/settings/account/password",
            "/settings/account/oidc/link",
            "/settings/account/oidc/unlink",
        }
    }
    for route in routes.values():
        names = {dep.call.__name__ for dep in route.dependant.dependencies}
        assert "require_csrf" not in names


class _FakeCursor:
    def __init__(self, row, identity):
        self._row = row
        self._identity = identity
        self._query = ""

    async def execute(self, *args, **kwargs):
        self._query = args[0] if args else ""
        return self

    async def fetchone(self):
        if "FROM oidc_identities" in self._query:
            return self._identity
        return self._row


class _FakeConn:
    def __init__(self, row, identity):
        self._row = row
        self._identity = identity

    def cursor(self, row_factory=None):
        return _FakeCursor(self._row, self._identity)

    async def execute(self, *args, **kwargs):
        cursor = _FakeCursor((self._row is not None,), self._identity)
        return await cursor.execute(*args, **kwargs)


class _FakeConnCtx:
    def __init__(self, row, identity):
        self._row = row
        self._identity = identity

    async def __aenter__(self):
        return _FakeConn(self._row, self._identity)

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    def __init__(self, row=None, identity=None):
        self._row = row
        self._identity = identity

    def connection(self):
        return _FakeConnCtx(self._row, self._identity)


class _FakeOAuthClient:
    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        from starlette.responses import RedirectResponse

        return RedirectResponse("https://idp.example.com/authorize", status_code=303)


class _FakeOAuth:
    pocketid = _FakeOAuthClient()


def _bare_app(*, signup: bool, oidc: bool, account=None, linked=False):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )
    app.state.config = SimpleNamespace(
        dev_no_auth=False,
        initial_admin_signup=signup,
        allowed_email="",
        oidc_configured=oidc,
        oidc_issuer="https://idp.example.com",
    )
    app.state.pool = _FakePool(account, {"id": 1} if linked else None)
    app.state.oauth = _FakeOAuth() if oidc else None
    app.state.templates = SimpleNamespace(
        TemplateResponse=lambda request, name, context, status_code=200: (
            PlainTextResponse(f"rendered:{name}", status_code=status_code)
        )
    )
    app.include_router(make_router())
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        return await client.get(path)


def test_fresh_install_exposes_signup_but_not_legacy_oidc():
    app = _bare_app(signup=True, oidc=True)
    assert asyncio.run(_get(app, "/signup")).status_code == 200
    assert asyncio.run(_get(app, "/login/oidc")).status_code == 404
    assert asyncio.run(_get(app, "/auth/callback")).status_code == 404


def test_upgrade_default_closes_signup_and_preserves_legacy_oidc():
    app = _bare_app(signup=False, oidc=True)
    assert asyncio.run(_get(app, "/signup")).status_code == 404
    assert asyncio.run(_get(app, "/login/oidc")).status_code == 303


def test_existing_unlinked_account_closes_signup_and_oidc_login():
    account = {
        "id": 1,
        "email": "admin@example.com",
        "password_hash": "hash",
        "is_admin": True,
        "is_enabled": True,
        "auth_version": 1,
    }
    app = _bare_app(signup=True, oidc=True, account=account)
    assert asyncio.run(_get(app, "/signup")).status_code == 404
    assert asyncio.run(_get(app, "/login/oidc")).status_code == 404
    assert asyncio.run(_get(app, "/login")).status_code == 200


def test_existing_linked_account_enables_oidc_login():
    account = {
        "id": 1,
        "email": "admin@example.com",
        "password_hash": "hash",
        "is_admin": True,
        "is_enabled": True,
        "auth_version": 1,
    }
    app = _bare_app(signup=False, oidc=True, account=account, linked=True)
    assert asyncio.run(_get(app, "/login/oidc")).status_code == 303


def test_admin_token_cannot_restore_setup_route():
    app = _bare_app(signup=False, oidc=False)
    app.state.config.admin_token = "obsolete"
    assert asyncio.run(_get(app, "/setup")).status_code == 404
