from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

import app.auth as auth
from app.ingest import FailedAuthLimiter
from app.main import SecurityHeadersMiddleware, _csp_nonce_context


class _FakeConnection:
    @asynccontextmanager
    async def transaction(self):
        yield self


def _app(monkeypatch, *, redeem=None, account=None, max_failures=3):
    conn = _FakeConnection()
    calls = []

    @asynccontextmanager
    async def control_connection(pool):
        assert pool is conn
        yield conn

    async def redeem_invitation(current, token, password, *, display_timezone):
        assert current is conn
        calls.append((token, password, display_timezone))
        if redeem is not None:
            raise redeem
        return 2

    async def get_account(current, account_id):
        assert current is conn and account_id == 2
        return account or {"id": 2, "is_enabled": True, "auth_version": 1}

    monkeypatch.setattr(auth, "control_connection", control_connection)
    monkeypatch.setattr(auth, "redeem_invitation", redeem_invitation)
    monkeypatch.setattr(auth, "get_account", get_account)
    app = FastAPI()
    app.state.config = SimpleNamespace(dev_no_auth=False, display_tz="UTC")
    app.state.control_pool = conn
    app.state.login_limiter = FailedAuthLimiter(max_failures, 900)
    app.state.templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "app" / "templates"),
        context_processors=[_csp_nonce_context],
    )
    app.add_middleware(SessionMiddleware, secret_key="test-secret", https_only=False)
    app.add_middleware(SecurityHeadersMiddleware, tile_host="https://tiles.example", hsts_max_age=0)
    app.include_router(auth.make_router())

    @app.get("/session")
    async def session(request: Request):
        return JSONResponse(dict(request.session))

    return app, calls


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _form(csrf, **changes):
    fields = {"csrf_token": csrf, "token": "copied-bearer", "password": "good password",
              "password_confirm": "good password", "display_timezone": "UTC"}
    fields.update(changes)
    return fields


def test_invite_page_and_failures_hide_token_and_set_private_headers(monkeypatch):
    async def check():
        app, calls = _app(monkeypatch, redeem=auth.InvitationUnavailable())
        async with await _client(app) as client:
            page = await client.get("/invite#token=copied-bearer")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store, private"
            assert page.headers["referrer-policy"] == "no-referrer"
            assert "copied-bearer" not in page.text
            assert 'name="token"' in page.text
            assert 'replaceState(null, "", "/invite")' in page.text
            csrf = (await client.get("/session")).json()["csrf"]
            failed = await client.post("/invite", data=_form(csrf))
            assert failed.status_code == 400
            assert failed.headers["cache-control"] == "no-store, private"
            assert failed.headers["referrer-policy"] == "no-referrer"
            assert auth.GENERIC_INVITE_ERROR in failed.text
            assert "copied-bearer" not in failed.text
            assert calls == [("copied-bearer", "good password", "UTC")]
    asyncio.run(check())


def test_invite_validates_before_redemption_and_caps_body(monkeypatch):
    async def check():
        app, calls = _app(monkeypatch, max_failures=10)
        async with await _client(app) as client:
            await client.get("/invite")
            csrf = (await client.get("/session")).json()["csrf"]
            bad_csrf = await client.post("/invite", data=_form("wrong"))
            assert bad_csrf.status_code == 403
            bad_token = await client.post("/invite", data=_form(csrf, token=""))
            assert bad_token.status_code == 400
            bad_password = await client.post("/invite", data=_form(csrf, password="short", password_confirm="short"))
            assert bad_password.status_code == 400
            bad_timezone = await client.post("/invite", data=_form(csrf, display_timezone="Not/AZone"))
            assert bad_timezone.status_code == 400
            oversized = await client.post("/invite", content=b"x" * 8193,
                                          headers={"content-type": "application/x-www-form-urlencoded"})
            assert oversized.status_code == 413
            for response in (bad_csrf, bad_token, bad_password, bad_timezone, oversized):
                assert response.headers["cache-control"] == "no-store, private"
                assert response.headers["referrer-policy"] == "no-referrer"
            assert calls == []
    asyncio.run(check())


def test_invite_success_clears_prior_session_and_limiter_bounds_attempts(monkeypatch):
    async def check():
        app, calls = _app(monkeypatch, redeem=auth.InvitationUnavailable(), max_failures=1)
        async with await _client(app) as client:
            await client.get("/invite")
            csrf = (await client.get("/session")).json()["csrf"]
            failed = await client.post("/invite", data=_form(csrf))
            blocked = await client.post("/invite", data=_form(csrf))
            assert (failed.status_code, blocked.status_code) == (400, 429)
            assert len(calls) == 1

        app, calls = _app(monkeypatch)
        @app.get("/old-session")
        async def old_session(request: Request):
            request.session.update(account_id=1, auth_version=9, selection="old", csrf="old-csrf")
            return JSONResponse({"ok": True})

        async with await _client(app) as client:
            await client.get("/old-session")
            response = await client.post("/invite", data=_form("old-csrf"))
            assert response.status_code == 303
            assert response.headers["location"] == "/"
            assert response.headers["cache-control"] == "no-store, private"
            session = (await client.get("/session")).json()
            assert session["account_id"] == 2
            assert session["auth_version"] == 1
            assert session["csrf"] != "old-csrf"
            assert "selection" not in session
            assert calls == [("copied-bearer", "good password", "UTC")]
    asyncio.run(check())


def test_parallel_invalid_invites_bound_password_work(monkeypatch):
    async def check():
        app, _ = _app(monkeypatch, max_failures=2)
        limiter = app.state.login_limiter
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def slow_redeem(conn, token, password, *, display_timezone):
            calls.append(token)
            if len(calls) == 2:
                entered.set()
            await release.wait()
            raise auth.InvitationUnavailable()

        monkeypatch.setattr(auth, "redeem_invitation", slow_redeem)
        async with await _client(app) as setup:
            await setup.get("/invite")
            csrf = (await setup.get("/session")).json()["csrf"]
            cookie = setup.cookies.get("session")
        async with AsyncExitStack() as stack:
            clients = []
            for _ in range(3):
                client = await stack.enter_async_context(await _client(app))
                client.cookies.set("session", cookie)
                clients.append(client)
            first = asyncio.create_task(clients[0].post("/invite", data=_form(csrf, token="bad-one")))
            second = asyncio.create_task(clients[1].post("/invite", data=_form(csrf, token="bad-two")))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert len(limiter._auth_tasks) == 2
                excess = await clients[2].post("/invite", data=_form(csrf, token="bad-three"))
                assert excess.status_code == 429
                assert len(limiter._auth_tasks) == 2
                assert calls == ["bad-one", "bad-two"]
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(first, second), 5)
                assert [response.status_code for response in responses] == [400, 400]
                assert limiter.blocked("127.0.0.1")
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await asyncio.gather(*limiter._auth_tasks, return_exceptions=True)
    asyncio.run(check())


def test_cancelled_bounded_work_keeps_its_slot_and_records_failure():
    async def check():
        limiter = FailedAuthLimiter(1, 900, max_concurrent_auth=1)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            entered.set()
            await release.wait()
            limiter.record_failure("client")
            raise auth.InvitationUnavailable()

        request = asyncio.create_task(limiter.run_bounded(operation))
        try:
            await entered.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert len(limiter._auth_tasks) == 1
            with pytest.raises(auth._AuthSaturated):
                await limiter.run_bounded(operation)
            release.set()
            await asyncio.gather(*limiter._auth_tasks, return_exceptions=True)
            assert limiter.blocked("client")
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            await asyncio.gather(*limiter._auth_tasks, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_invite_is_closed_in_dev_no_auth(monkeypatch, method):
    async def check():
        app, calls = _app(monkeypatch)
        app.state.config.dev_no_auth = True
        async with await _client(app) as client:
            response = await client.request(method, "/invite")
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store, private"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert calls == []
    asyncio.run(check())
