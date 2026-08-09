"""/auth/callback rate limiting (app/auth.py). Authlib's `state` check
stops a cold drive-by, but a scripted caller can loop "fetch a fresh
state-bearing session, then hand back a garbage code" indefinitely, and
each iteration is a real outbound token-exchange request to the identity
provider from this app's own egress address. These tests prove: repeated
rejected callbacks trip the shared login_limiter; a blocked caller never
reaches authorize_access_token at all (the actual outbound call); an
ALLOWED_EMAIL rejection also counts as a failure; and a non-OAuthError
failure (standing in for the provider being unreachable) is not charged
to the caller's ledger.

tests/test_forwarded_allow_ips_spoofing.py covers the header-spoofing
property for this same endpoint, following that file's conventions.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from authlib.integrations.base_client import OAuthError
from fastapi import FastAPI, Request, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app.auth import make_router
from app.ingest import FailedAuthLimiter

MAX_FAILURES = 3


class _RejectingOAuthClient:
    """Raises OAuthError every time, like authlib does for a garbage/
    replayed code or a mismatched state -- the abuse case this limiter
    exists to cap. Counts calls so a test can prove a blocked caller never
    reaches this at all.
    """

    def __init__(self):
        self.calls = 0

    async def authorize_access_token(self, request):
        self.calls += 1
        raise OAuthError(error="invalid_grant", description="bad code")


class _OutageOAuthClient:
    """Raises something other than OAuthError, standing in for a transport
    failure or a 5xx from the identity provider -- not the caller's fault.
    """

    def __init__(self):
        self.calls = 0

    async def authorize_access_token(self, request):
        self.calls += 1
        raise ConnectionError("idp unreachable")


class _SucceedingOAuthClient:
    def __init__(self, userinfo):
        self.calls = 0
        self.userinfo = userinfo

    async def authorize_access_token(self, request):
        self.calls += 1
        return {"userinfo": self.userinfo}


def _app(oauth_client, *, allowed_email: str = ""):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )
    app.state.config = SimpleNamespace(dev_no_auth=False, allowed_email=allowed_email)
    app.state.oauth = SimpleNamespace(pocketid=oauth_client)
    app.state.login_limiter = FailedAuthLimiter(max_failures=MAX_FAILURES, window_s=900)

    # Test-only routes to plant and inspect session content around the
    # callback, same approach tests/test_auth_logout.py uses -- the real
    # session is only reachable through actual dispatch, not by calling the
    # handler function directly.
    @app.post("/test/plant-session")
    async def plant(request: Request):
        request.session["csrf"] = "preplanted-csrf"
        request.session["evil"] = "sneaky-preplanted-value"
        return Response(status_code=204)

    @app.get("/test/session")
    async def read_session(request: Request):
        return JSONResponse(dict(request.session))

    app.include_router(make_router())
    return app


async def _get_callback(app, *, peer=("203.0.113.5", 51000)):
    transport = httpx.ASGITransport(app=app, client=peer)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        return await client.get("/auth/callback?code=garbage&state=whatever")


def test_repeated_rejected_callbacks_trip_the_limiter():
    oauth_client = _RejectingOAuthClient()
    app = _app(oauth_client)

    async def run():
        results = []
        for _ in range(MAX_FAILURES + 2):
            results.append((await _get_callback(app)).status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [401] * MAX_FAILURES
    assert results[MAX_FAILURES:] == [429] * 2
    # One real token-exchange attempt per rejected callback, no more.
    assert oauth_client.calls == MAX_FAILURES


def test_blocked_caller_never_reaches_authorize_access_token():
    oauth_client = _RejectingOAuthClient()
    app = _app(oauth_client)

    async def run():
        for _ in range(MAX_FAILURES):
            await _get_callback(app)
        assert oauth_client.calls == MAX_FAILURES
        blocked = await _get_callback(app)
        return blocked

    blocked = asyncio.run(run())
    assert blocked.status_code == 429
    # The whole point: a blocked caller must never trigger the outbound
    # call to the identity provider.
    assert oauth_client.calls == MAX_FAILURES


def test_rejected_allowed_email_also_counts_as_a_failure():
    oauth_client = _SucceedingOAuthClient({"email": "not-the-admin@example.com"})
    app = _app(oauth_client, allowed_email="admin@example.com")

    async def run():
        results = []
        for _ in range(MAX_FAILURES + 1):
            results.append((await _get_callback(app)).status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [403] * MAX_FAILURES
    assert results[MAX_FAILURES] == 429


def test_successful_callback_records_no_failure():
    oauth_client = _SucceedingOAuthClient({"email": "admin@example.com", "sub": "1"})
    app = _app(oauth_client, allowed_email="admin@example.com")

    async def run():
        results = []
        for _ in range(MAX_FAILURES + 2):
            results.append((await _get_callback(app)).status_code)
        return results

    results = asyncio.run(run())
    # A real OIDC flow never repeats the same code, but the point here is
    # only that success itself never feeds the limiter -- every one of
    # these redirects, none is ever blocked.
    assert results == [303] * (MAX_FAILURES + 2)


def test_non_oauth_error_failure_is_not_charged_to_the_callers_ledger():
    oauth_client = _OutageOAuthClient()
    app = _app(oauth_client)

    async def run():
        results = []
        for _ in range(MAX_FAILURES + 2):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                results.append((await client.get("/auth/callback?code=x&state=y")).status_code)
        return results

    results = asyncio.run(run())
    # Every attempt reaches the identity-provider call and every one
    # surfaces as the app's generic 500 (an outage is diagnosable, not a
    # rejected credential) -- none of them ever gets blocked.
    assert results == [500] * (MAX_FAILURES + 2)
    assert oauth_client.calls == MAX_FAILURES + 2


def test_successful_callback_clears_pre_login_session_and_remints_csrf():
    oauth_client = _SucceedingOAuthClient({"email": "admin@example.com", "sub": "1"})
    app = _app(oauth_client, allowed_email="admin@example.com")

    async def run():
        transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            await client.post("/test/plant-session")
            callback_response = await client.get("/auth/callback?code=garbage&state=whatever")
            assert callback_response.status_code == 303

            session = (await client.get("/test/session")).json()
            # session.clear() ran: the pre-planted key is gone and the CSRF
            # token minted for the now-authenticated session is a fresh one,
            # not the pre-login value a fixation attack would have planted.
            assert "evil" not in session
            assert session["user"]["email"] == "admin@example.com"
            assert session["csrf"] != "preplanted-csrf"

    asyncio.run(run())
