"""Route-registration tests for the new /login/local, /login/oidc, and
/setup routes: they must resolve to their own handlers (not get swallowed
by another route or dependency), and /setup must genuinely 404 -- not just
render an error page -- when ADMIN_TOKEN is unset. Real `Route.matches()`
resolution, no DB needed, same pattern as tests/test_ui_route_ordering.py.
"""
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


def test_login_local_and_oidc_and_setup_resolve_to_their_own_handlers():
    assert _first_matching_route("GET", "/login").path == "/login"
    assert _first_matching_route("POST", "/login/local").path == "/login/local"
    assert _first_matching_route("GET", "/login/oidc").path == "/login/oidc"
    assert _first_matching_route("GET", "/setup").path == "/setup"
    assert _first_matching_route("POST", "/setup").path == "/setup"


def test_existing_routes_still_resolve_after_new_routes_are_added():
    assert _first_matching_route("GET", "/auth/callback").path == "/auth/callback"
    assert _first_matching_route("POST", "/logout").path == "/logout"


def test_login_local_and_setup_have_no_require_csrf_dependency():
    # These forms are plain (no-JS) POSTs and can't set the X-CSRF-Token
    # header require_csrf checks -- their CSRF defense is the hidden
    # csrf_token form field, checked by hand inside the handler instead.
    routes = {
        route.path: route for route in make_router().routes
        if route.path in {"/login/local", "/setup"}
    }
    for route in routes.values():
        names = {dep.call.__name__ for dep in route.dependant.dependencies}
        assert "require_csrf" not in names


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def execute(self, *args, **kwargs):
        return self

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self, row_factory=None):
        return _FakeCursor(self._row)

    async def execute(self, *args, **kwargs):
        return None


class _FakeConnCtx:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _FakeConn(self._row)

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    def __init__(self, row=None):
        self._row = row

    def connection(self):
        return _FakeConnCtx(self._row)


class _FakeOAuthClient:
    async def authorize_redirect(self, request, redirect_uri):
        from starlette.responses import RedirectResponse
        return RedirectResponse("https://idp.example.com/authorize", status_code=303)


class _FakeOAuth:
    pocketid = _FakeOAuthClient()


def _bare_app(*, admin_token: str, oidc_configured: bool, local_admin_row=None):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )
    app.state.config = SimpleNamespace(
        dev_no_auth=False, admin_token=admin_token, allowed_email="",
        oidc_configured=oidc_configured,
    )
    app.state.pool = _FakePool(local_admin_row)
    app.state.oauth = _FakeOAuth() if oidc_configured else None
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


def test_setup_404s_when_admin_token_is_unset():
    app = _bare_app(admin_token="", oidc_configured=False)
    response = asyncio.run(_get(app, "/setup"))
    assert response.status_code == 404


def test_setup_is_reachable_when_admin_token_is_set():
    app = _bare_app(admin_token="setup-token", oidc_configured=False)
    response = asyncio.run(_get(app, "/setup"))
    assert response.status_code == 200


def test_login_renders_page_when_configured_and_no_local_admin_exists():
    # GET /login never auto-redirects to the provider, even in this
    # configuration (OIDC configured, no local admin row yet): auto-redirect
    # here is what let the IdP's own SSO session silently re-authenticate a
    # user right after they logged out.
    app = _bare_app(admin_token="", oidc_configured=True, local_admin_row=None)
    response = asyncio.run(_get(app, "/login"))
    assert response.status_code == 200
    assert response.text == "rendered:login.html"


def test_login_renders_page_when_local_admin_exists_even_with_oidc_configured():
    app = _bare_app(
        admin_token="", oidc_configured=True,
        local_admin_row={"id": 1, "email": "admin@example.com"},
    )
    response = asyncio.run(_get(app, "/login"))
    assert response.status_code == 200
    assert response.text == "rendered:login.html"


def test_auth_callback_404s_in_local_only_mode():
    app = _bare_app(admin_token="setup-token", oidc_configured=False)
    response = asyncio.run(_get(app, "/auth/callback"))
    assert response.status_code == 404
