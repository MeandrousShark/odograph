"""Non-ASCII CSRF token handling for check_form_csrf (app/auth.py, the plain
<form> POST check used by login, signup, and Account Settings) and require_csrf
(the X-CSRF-Token header check used by /logout and other htmx POSTs):
hmac.compare_digest raises TypeError on non-ASCII `str` operands, which
turned a merely-wrong CSRF token into a 500 instead of a 403. No DB needed --
both checks run before either handler touches the database, same reasoning
as tests/test_auth_logout.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse

from app.auth import make_router


class _FakeCursor:
    async def execute(self, *args, **kwargs):
        return self

    async def fetchone(self):
        return None  # no account row, unreached because check_form_csrf raises first


class _FakeConn:
    def cursor(self, row_factory=None):
        return _FakeCursor()

    async def execute(self, *args, **kwargs):
        return None


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
    app.state.config = SimpleNamespace(
        dev_no_auth=False, initial_admin_signup=False, allowed_email=""
    )
    app.state.oauth = None
    app.state.pool = _FakePool()
    app.state.templates = SimpleNamespace(
        TemplateResponse=lambda request, name, context, status_code=200: (
            PlainTextResponse(f"rendered:{name}", status_code=status_code)
        )
    )

    @app.post("/test/seed-session")
    async def seed(request: Request):
        request.session["csrf"] = "ascii-session-csrf"
        request.session["user"] = {"sub": "u1"}
        return Response(status_code=204)

    app.include_router(make_router())
    return app


async def _client():
    transport = httpx.ASGITransport(app=_bare_app())
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    )


def test_login_local_rejects_non_ascii_csrf_token_with_403_not_500():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.post(
                "/login/local",
                data={
                    "email": "admin@example.com",
                    "password": "whatever",
                    "csrf_token": "tökén-mismatch",
                },
            )
            assert response.status_code == 403

    asyncio.run(run())


def test_logout_rejects_non_ascii_csrf_header_with_403_not_500():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            # httpx's own header validation rejects a non-ASCII str value
            # outright, so the raw UTF-8 bytes are passed directly -- what a
            # non-httpx client (or a hand-crafted request) can actually send
            # on the wire.
            response = await client.post(
                "/logout", headers={"X-CSRF-Token": "tökén-mismatch".encode("utf-8")}
            )
            assert response.status_code == 403

    asyncio.run(run())
