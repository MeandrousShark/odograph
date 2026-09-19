"""Proves a spoofed X-Forwarded-For cannot reset, evade, or poison the
per-IP ledger either FailedAuthLimiter keys on -- including the ledger
/auth/callback shares with the other authentication checks.

Wraps the real /ingest router and the real login/auth router in
uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware -- the same
middleware Uvicorn installs under --proxy-headers -- at its safe default
(trusted_hosts="127.0.0.1"). Only a request whose immediate TCP peer is
that trusted address gets its X-Forwarded-For header honored at all, so an
attacker connecting directly can put anything in the header and it never
changes what client_ip() sees.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from authlib.integrations.base_client import OAuthError
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import make_router as make_auth_router
from app.ingest import FailedAuthLimiter
from app.ingest import make_router as make_ingest_router

MAX_FAILURES = 3


# --- /ingest: FailedAuthLimiter keyed by client_ip, no session/CSRF involved ---


def _ingest_app(*, trusted_hosts):
    app = FastAPI()
    app.state.ingest_limiter = FailedAuthLimiter(max_failures=MAX_FAILURES, window_s=900)
    app.state.config = SimpleNamespace(
        ingest_username="owntracks",
        ingest_password="secret",
        ingest_max_body_bytes=65536,
    )
    app.include_router(make_ingest_router())
    return ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)


async def _post_ingest(app, *, peer, xff=None):
    transport = httpx.ASGITransport(app=app, client=peer)
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post("/ingest", headers=headers)


def test_ingest_limiter_blocks_untrusted_peer_regardless_of_spoofed_xff():
    # Attacker connects directly (not through the trusted proxy) and rotates
    # a fake X-Forwarded-For on every attempt, hoping to look like a fresh
    # IP each time and dodge the limiter entirely.
    app = _ingest_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)

    async def run():
        results = []
        for i in range(MAX_FAILURES + 2):
            response = await _post_ingest(app, peer=attacker, xff=f"10.0.0.{i}")
            results.append(response.status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [401] * MAX_FAILURES
    assert results[MAX_FAILURES:] == [429] * 2


def test_ingest_spoofed_xff_cannot_poison_a_victims_ledger():
    # Attacker (untrusted peer) tries to burn a victim's real IP by forging
    # X-Forwarded-For as that IP on every failed attempt. If the forgery
    # worked, the victim would show up blocked despite never having made a
    # request themselves.
    app = _ingest_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)
    victim_ip = "198.51.100.7"

    async def run():
        for _ in range(MAX_FAILURES + 2):
            await _post_ingest(app, peer=attacker, xff=victim_ip)
        # The victim's own real request, made fresh, must not already be
        # blocked -- the forged attempts never landed on their ledger entry.
        return await _post_ingest(app, peer=(victim_ip, 6000))

    response = asyncio.run(run())
    assert response.status_code == 401


def test_ingest_spoofed_xff_cannot_reset_attackers_own_block():
    # Once the attacker's real IP is blocked, alternating the spoofed header
    # value per request must not buy a fresh window under the real address.
    app = _ingest_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)

    async def run():
        for _ in range(MAX_FAILURES):
            await _post_ingest(app, peer=attacker, xff="1.2.3.4")
        blocked = await _post_ingest(app, peer=attacker, xff="9.9.9.9")
        still_blocked = await _post_ingest(app, peer=attacker, xff=None)
        return blocked, still_blocked

    blocked, still_blocked = asyncio.run(run())
    assert blocked.status_code == 429
    assert still_blocked.status_code == 429


def test_ingest_trusted_proxy_still_gets_forwarded_ip_honored():
    # Sanity control: the safe topology this whole scheme depends on --
    # requests actually arriving from the trusted proxy peer still get
    # their forwarded client honored, so the limiter tracks the real
    # end-user, not the proxy, for every legitimate visitor behind it.
    app = _ingest_app(trusted_hosts="127.0.0.1")
    trusted_proxy = ("127.0.0.1", 12345)

    async def run():
        results = []
        for _ in range(MAX_FAILURES + 1):
            results.append(
                await _post_ingest(app, peer=trusted_proxy, xff="198.51.100.42")
            )
        # A different real visitor forwarded through the same trusted proxy
        # must not be caught by the first visitor's failures.
        other_visitor = await _post_ingest(app, peer=trusted_proxy, xff="198.51.100.99")
        return results, other_visitor

    results, other_visitor = asyncio.run(run())
    assert [r.status_code for r in results] == [401] * MAX_FAILURES + [429]
    assert other_visitor.status_code == 401


# --- /login/local: FailedAuthLimiter behind session and CSRF ---


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
        return _FakeCursor((None,) if "current_setting" in args[0] else (self._row is not None,))


class _FakeConnCtx:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _FakeConn(self._row)

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    def __init__(self, row):
        self._row = row

    def connection(self):
        return _FakeConnCtx(self._row)


class _CapturingTemplates:
    """Captures the last rendered context so a test can pull the real csrf
    token out of it, the way the real Jinja template embeds it as a hidden
    form field.
    """

    def __init__(self):
        self.last_context: dict | None = None

    def TemplateResponse(self, request, name, context, status_code=200):
        self.last_context = context
        return PlainTextResponse(f"rendered:{name}", status_code=status_code)


def _login_app(*, trusted_hosts):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )
    app.state.config = SimpleNamespace(
        dev_no_auth=False,
        initial_admin_signup=False,
        allowed_email="",
        oidc_configured=False,
    )
    # Wrong email on every submission short-circuits login_local's `and`
    # chain before verify_password, so failures are cheap and deterministic
    # without needing a real scrypt hash here.
    app.state.control_pool = _FakePool({
        "id": 1,
        "email": "admin@example.com",
        "password_hash": "x",
        "is_admin": True,
        "is_enabled": True,
        "auth_version": 1,
    })
    app.state.oauth = None
    templates = _CapturingTemplates()
    app.state.templates = templates
    app.state.login_limiter = FailedAuthLimiter(max_failures=MAX_FAILURES, window_s=900)
    app.include_router(make_auth_router())
    return ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts), templates


async def _fail_login(app, templates, client: httpx.AsyncClient, *, xff=None):
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    await client.get("/login", headers=headers)
    csrf = templates.last_context["csrf"]
    return await client.post(
        "/login/local",
        data={"email": "not-the-admin@example.com", "password": "wrong", "csrf_token": csrf},
        headers=headers,
    )


def test_login_limiter_blocks_untrusted_peer_regardless_of_spoofed_xff():
    app, templates = _login_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)
    transport = httpx.ASGITransport(app=app, client=attacker)

    async def run():
        results = []
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            for i in range(MAX_FAILURES + 2):
                response = await _fail_login(app, templates, client, xff=f"192.0.2.{i}")
                results.append(response.status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [401] * MAX_FAILURES
    assert results[MAX_FAILURES:] == [429] * 2


def test_login_spoofed_xff_cannot_poison_a_victims_ledger():
    app, templates = _login_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)
    victim_ip = "198.51.100.7"

    async def run():
        transport = httpx.ASGITransport(app=app, client=attacker)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            for _ in range(MAX_FAILURES + 2):
                await _fail_login(app, templates, client, xff=victim_ip)

        victim_transport = httpx.ASGITransport(app=app, client=(victim_ip, 6000))
        async with httpx.AsyncClient(
            transport=victim_transport, base_url="http://testserver"
        ) as client:
            return await _fail_login(app, templates, client)

    response = asyncio.run(run())
    assert response.status_code == 401


# --- /auth/callback: FailedAuthLimiter shared with local credential checks ---


class _RejectingOAuthClient:
    """Every call raises OAuthError, like authlib does for a garbage/replayed
    code or a mismatched state -- a scripted caller looping this is exactly
    what /auth/callback's rate limit exists to cap.
    """

    async def authorize_access_token(self, request):
        raise OAuthError(error="invalid_grant", description="bad code")


def _callback_app(*, trusted_hosts):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )
    app.state.config = SimpleNamespace(
        dev_no_auth=False,
        initial_admin_signup=False,
        allowed_email="",
        oidc_issuer="https://idp.example.com",
    )
    app.state.oauth = SimpleNamespace(pocketid=_RejectingOAuthClient())
    app.state.control_pool = _FakePool(None)
    app.state.login_limiter = FailedAuthLimiter(max_failures=MAX_FAILURES, window_s=900)
    app.include_router(make_auth_router())
    return ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)


async def _get_callback(app, *, peer, xff=None):
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    transport = httpx.ASGITransport(app=app, client=peer)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get("/auth/callback?code=garbage&state=whatever", headers=headers)


def test_callback_limiter_blocks_untrusted_peer_regardless_of_spoofed_xff():
    app = _callback_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)

    async def run():
        results = []
        for i in range(MAX_FAILURES + 2):
            response = await _get_callback(app, peer=attacker, xff=f"192.0.2.{i}")
            results.append(response.status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [401] * MAX_FAILURES
    assert results[MAX_FAILURES:] == [429] * 2


def test_callback_spoofed_xff_cannot_poison_a_victims_ledger():
    app = _callback_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)
    victim_ip = "198.51.100.7"

    async def run():
        for _ in range(MAX_FAILURES + 2):
            await _get_callback(app, peer=attacker, xff=victim_ip)
        # The victim's own real request, made fresh, must not already be
        # blocked -- the forged attempts never landed on their ledger entry.
        return await _get_callback(app, peer=(victim_ip, 6000))

    response = asyncio.run(run())
    assert response.status_code == 401


def test_callback_spoofed_xff_cannot_reset_attackers_own_block():
    app = _callback_app(trusted_hosts="127.0.0.1")
    attacker = ("203.0.113.5", 51000)

    async def run():
        for _ in range(MAX_FAILURES):
            await _get_callback(app, peer=attacker, xff="1.2.3.4")
        blocked = await _get_callback(app, peer=attacker, xff="9.9.9.9")
        still_blocked = await _get_callback(app, peer=attacker, xff=None)
        return blocked, still_blocked

    blocked, still_blocked = asyncio.run(run())
    assert blocked.status_code == 429
    assert still_blocked.status_code == 429
