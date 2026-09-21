"""Tests for POST-only /logout (Batch B item 4): a bare GET no longer logs
anyone out (CSRF-able), and the CSRF-protected POST behaves like the old
GET did. No DB needed -- logout only touches the session; the follow-up
GET /login below uses a fake pool/oauth (test_auth_route_ordering.py's
pattern) purely to exercise routing, not real persistence.

Session state is seeded/inspected through two test-only routes bolted onto
the bare app below, the same "smallest ASGI app that exercises real
dependency injection" approach tests/test_dashboard_db.py uses for its
unauthenticated-redirect case, since `require_csrf` (a FastAPI dependency)
only actually runs when dispatched through the router.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse

from app.auth import make_router


class _FakeOAuthClient:
    async def authorize_redirect(self, request, redirect_uri):
        return RedirectResponse("https://idp.example.com/authorize", status_code=303)


class _FakeOAuth:
    pocketid = _FakeOAuthClient()


class _FakeCursor:
    async def execute(self, *args, **kwargs):
        self.query = args[0]
        return self

    async def fetchone(self):
        return (None,) if "current_setting" in self.query else (False,)


class _FakeConn:
    async def execute(self, *args, **kwargs):
        return await _FakeCursor().execute(*args, **kwargs)

    def cursor(self, row_factory=None):
        return _FakeCursor()


class _FakeConnCtx:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    def connection(self):
        return _FakeConnCtx()


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )
    # This OIDC-only upgrade configuration has no account row. It is the state
    # in which GET /login used to auto-redirect to the provider instead of
    # rendering a page (the logout bug).
    app.state.config = SimpleNamespace(
        dev_no_auth=False,
        initial_admin_signup=False,
        allowed_email="",
        oidc_configured=True,
    )
    app.state.oauth = _FakeOAuth()
    app.state.control_pool = _FakePool()
    app.state.templates = SimpleNamespace(
        TemplateResponse=lambda request, name, context, status_code=200: (
            PlainTextResponse(f"rendered:{name}", status_code=status_code)
        )
    )

    @app.post("/test/seed-session")
    async def seed(request: Request):
        request.session["user"] = {"sub": "u1"}
        request.session["csrf"] = "test-csrf-token"
        return Response(status_code=204)

    @app.get("/test/session")
    async def read_session(request: Request):
        return {"has_user": "user" in request.session}

    app.include_router(make_router())
    return app


async def _client():
    transport = httpx.ASGITransport(app=_bare_app())
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    )


def test_get_logout_is_no_longer_allowed():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.get("/logout")
            assert response.status_code == 405

            still_logged_in = await client.get("/test/session")
            assert still_logged_in.json() == {"has_user": True}

    asyncio.run(run())


def test_post_logout_without_csrf_token_is_rejected():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.post("/logout")
            assert response.status_code == 403

            still_logged_in = await client.get("/test/session")
            assert still_logged_in.json() == {"has_user": True}

    asyncio.run(run())


def test_post_logout_with_valid_csrf_clears_session_and_redirects():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.post(
                "/logout", headers={"X-CSRF-Token": "test-csrf-token"}
            )
            assert response.status_code == 204
            # The query marker is read only by base.html's inline script
            # (never the server) so a real sign-out still clears the shared
            # cross-tab account marker and other signed-in tabs reload.
            assert response.headers["HX-Redirect"] == "/login?signed_out=1"

            logged_out = await client.get("/test/session")
            assert logged_out.json() == {"has_user": False}

    asyncio.run(run())


def test_post_logout_then_following_hx_redirect_renders_login_not_oidc():
    # Regression test for the reported defect: logging out looked like a
    # no-op because the IdP's own SSO session silently re-authenticated the
    # browser the moment it landed on /login. GET /login must render the
    # login page here rather than issue an OIDC redirect.
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            logout_response = await client.post(
                "/logout", headers={"X-CSRF-Token": "test-csrf-token"}
            )
            assert logout_response.status_code == 204
            redirect_target = logout_response.headers["HX-Redirect"]

            login_response = await client.get(redirect_target)
            assert login_response.status_code == 200
            assert login_response.text == "rendered:login.html"

    asyncio.run(run())
