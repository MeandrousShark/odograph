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

Under FORWARDED_ALLOW_IPS=*, uvicorn trusts every hop and picks the leftmost
X-Forwarded-For entry; the app adds RightmostForwardedForMiddleware inside it
so the client address is the rightmost entry, the address the immediate proxy
saw. The tests at the end run both together, as create_app arranges them.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from authlib.integrations.base_client import OAuthError
from fastapi import FastAPI, Request
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import make_router as make_auth_router
from app.config import Config
from app.forwarded import RightmostForwardedForMiddleware, wildcard_trust
from app.ingest import FailedAuthLimiter, client_ip
from app.ingest import make_router as make_ingest_router

MAX_FAILURES = 3


def _behind_uvicorn(app, trusted_hosts):
    # Same order as production: uvicorn's middleware outside, the app's inside.
    if wildcard_trust(trusted_hosts):
        app.add_middleware(RightmostForwardedForMiddleware)
    return ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)


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
    return _behind_uvicorn(app, trusted_hosts)


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
    assert results[MAX_FAILURES:] == [503] * 2


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
    assert blocked.status_code == 503
    assert still_blocked.status_code == 503


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
    assert [r.status_code for r in results] == [401] * MAX_FAILURES + [503]
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
    app.state.security_link_base = ""
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
    return _behind_uvicorn(app, trusted_hosts), templates


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
    app.state.security_link_base = ""
    app.state.templates = _CapturingTemplates()
    app.state.control_pool = _FakePool(None)
    app.state.login_limiter = FailedAuthLimiter(max_failures=MAX_FAILURES, window_s=900)
    app.include_router(make_auth_router())
    return _behind_uvicorn(app, trusted_hosts)


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


# --- documented reverse-proxy configuration, end to end -----------------------

ROOT = Path(__file__).resolve().parents[1]
REAL_CLIENT = "203.0.113.9"
CLIENT_SUPPLIED = "6.6.6.6"


def _fenced_block(language: str) -> str:
    guide = (ROOT / "docs/reverse-proxy.md").read_text(encoding="utf-8")
    return guide.split(f"```{language}\n", 1)[1].split("```", 1)[0]


def _documented_nginx_directive(header: str) -> str:
    block = _fenced_block("nginx")
    match = re.search(rf"^\s*proxy_set_header\s+{header}\s+(.+?);\s*$", block, re.M)
    assert match, f"docs/reverse-proxy.md nginx block has no {header} directive"
    return match.group(1)


def _shipped_forwarded_allow_ips() -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^FORWARDED_ALLOW_IPS=(.*)$", text, re.M)
    assert match, ".env.example has no uncommented FORWARDED_ALLOW_IPS line"
    return match.group(1).strip()


def _nginx_header_value(directive: str, *, remote_addr: str, incoming: str | None) -> str:
    """Emulate the nginx variables the documented directives may use."""
    if directive == "$remote_addr":
        return remote_addr
    if directive == "$proxy_add_x_forwarded_for":
        return f"{incoming}, {remote_addr}" if incoming else remote_addr
    if directive == "$scheme":
        return "https"
    pytest.fail(
        f"unreviewed nginx value {directive!r} in docs/reverse-proxy.md; "
        "extend this emulation after checking what it forwards"
    )


def _seen_by_app(*, directive, incoming, peer_host, trusted_hosts, app_middleware=True):
    """Run the app's client_ip() behind uvicorn's proxy-headers middleware.

    With app_middleware, the stack matches create_app: uvicorn's middleware
    outside and, under `*`, RightmostForwardedForMiddleware inside it.
    """

    async def whoami(request):
        return PlainTextResponse(f"{client_ip(request)} {request.url.scheme}")

    proxy_headers = {
        "X-Forwarded-For": _nginx_header_value(
            directive, remote_addr=REAL_CLIENT, incoming=incoming
        ),
        "X-Forwarded-Proto": _nginx_header_value(
            _documented_nginx_directive("X-Forwarded-Proto"),
            remote_addr=REAL_CLIENT,
            incoming=None,
        ),
    }
    inner = Starlette(routes=[Route("/", whoami)])
    if app_middleware:
        app = _behind_uvicorn(inner, trusted_hosts)
    else:
        app = ProxyHeadersMiddleware(inner, trusted_hosts=trusted_hosts)

    async def run():
        transport = httpx.ASGITransport(app=app, client=(peer_host, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # Only the proxy-built headers reach the app.
            return await client.get("/", headers=proxy_headers)

    return asyncio.run(run()).text.split()


INCOMING_VALUES = [None, CLIENT_SUPPLIED, f"{CLIENT_SUPPLIED}, 7.7.7.7", "2001:db8::1"]
PROXY_PEERS = ["127.0.0.1", "10.88.0.1"]


@pytest.mark.parametrize("peer_host", PROXY_PEERS)
@pytest.mark.parametrize("incoming", INCOMING_VALUES)
def test_documented_nginx_config_records_the_connecting_address(incoming, peer_host):
    client, scheme = _seen_by_app(
        directive=_documented_nginx_directive("X-Forwarded-For"),
        incoming=incoming,
        peer_host=peer_host,
        trusted_hosts=_shipped_forwarded_allow_ips(),
    )
    assert client == REAL_CLIENT
    assert scheme == "https"


def test_appending_directive_records_the_connecting_address_under_the_shipped_setting():
    """An appending proxy keeps the client-supplied value ahead of its own.

    uvicorn alone under `*` would record that leftmost value; the app's
    rightmost-entry rule records the address the proxy saw instead.
    """
    kwargs = dict(
        directive="$proxy_add_x_forwarded_for",
        incoming=CLIENT_SUPPLIED,
        peer_host="127.0.0.1",
        trusted_hosts=_shipped_forwarded_allow_ips(),
    )
    assert _seen_by_app(**kwargs, app_middleware=False)[0] == CLIENT_SUPPLIED
    assert _seen_by_app(**kwargs) == [REAL_CLIENT, "https"]


def test_documented_caddy_block_leaves_forwarded_header_handling_to_caddy():
    # Verified live (2026-10-15, Caddy 2.11.7): without trusted_proxies Caddy
    # discards a client-supplied X-Forwarded-For. Keep the block in that shape.
    block = _fenced_block("caddyfile").lower()
    assert "trusted_proxies" not in block
    assert not re.search(r"header_up\b[^\n]*x-forwarded-for", block)


# --- FORWARDED_ALLOW_IPS=*: the rightmost X-Forwarded-For entry is the client ---


async def _echo(scope, receive, send):
    host, port = scope["client"]
    body = f"{host}|{port}|{scope['scheme']}".encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


def _wildcard_echo():
    return ProxyHeadersMiddleware(RightmostForwardedForMiddleware(_echo), trusted_hosts="*")


async def _seen(app, headers=(), *, peer=("127.0.0.1", 40000)):
    transport = httpx.ASGITransport(app=app, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/", headers=list(headers))
    host, port, scheme = response.text.split("|")
    return host, int(port), scheme


def _xff(*values):
    return [("X-Forwarded-For", value) for value in values]


def test_wildcard_trust_matches_uvicorns_bare_wildcard_only():
    assert wildcard_trust("*")
    for value in ("", " * ", "*,127.0.0.1", "127.0.0.1,*", "127.0.0.1", "10.0.0.0/8"):
        assert not wildcard_trust(value)


def test_uvicorn_alone_under_wildcard_uses_the_leftmost_entry():
    # Pins the uvicorn behavior the app middleware exists to correct.
    app = ProxyHeadersMiddleware(_echo, trusted_hosts="*")
    assert asyncio.run(_seen(app, _xff("6.6.6.6, 203.0.113.9")))[0] == "6.6.6.6"


def test_wildcard_single_entry_is_the_client():
    assert asyncio.run(_seen(_wildcard_echo(), _xff("203.0.113.9")))[:2] == ("203.0.113.9", 0)


def test_wildcard_client_supplied_leading_entry_is_ignored():
    assert asyncio.run(_seen(_wildcard_echo(), _xff("6.6.6.6, 203.0.113.9")))[0] == "203.0.113.9"


def test_wildcard_three_entries_use_the_rightmost():
    seen = asyncio.run(_seen(_wildcard_echo(), _xff("6.6.6.6, 198.51.100.1, 203.0.113.9")))
    assert seen[0] == "203.0.113.9"


def test_wildcard_ipv6_and_port_forms():
    app = _wildcard_echo()
    assert asyncio.run(_seen(app, _xff("6.6.6.6, 2001:db8::9")))[:2] == ("2001:db8::9", 0)
    assert asyncio.run(_seen(app, _xff("6.6.6.6, [2001:db8::9]")))[:2] == ("2001:db8::9", 0)
    assert asyncio.run(_seen(app, _xff("6.6.6.6, [2001:db8::9]:4711")))[:2] == ("2001:db8::9", 4711)
    assert asyncio.run(_seen(app, _xff("6.6.6.6, 203.0.113.9:4711")))[:2] == ("203.0.113.9", 4711)


def test_wildcard_multiple_header_lines_are_combined_in_order():
    seen = asyncio.run(_seen(_wildcard_echo(), _xff("6.6.6.6, 7.7.7.7", "203.0.113.9")))
    assert seen[0] == "203.0.113.9"


def test_wildcard_skips_empty_trailing_entries():
    assert asyncio.run(_seen(_wildcard_echo(), _xff("6.6.6.6, 203.0.113.9, ,")))[0] == "203.0.113.9"


def test_wildcard_all_empty_header_leaves_the_peer():
    seen = asyncio.run(_seen(_wildcard_echo(), _xff(" , "), peer=("192.0.2.10", 5555)))
    assert seen[:2] == ("192.0.2.10", 5555)


def test_wildcard_no_header_leaves_the_peer():
    assert asyncio.run(_seen(_wildcard_echo(), peer=("192.0.2.10", 5555)))[:2] == ("192.0.2.10", 5555)


def test_wildcard_non_ip_rightmost_entry_is_used_as_is():
    # Like uvicorn's own choice of entry, the value is not validated. Only the
    # immediate peer controls the rightmost entry, and under * it is trusted.
    seen = asyncio.run(_seen(_wildcard_echo(), _xff("203.0.113.9, not-an-ip")))
    assert seen[0] == "not-an-ip"


def test_wildcard_scheme_still_comes_from_x_forwarded_proto():
    headers = _xff("6.6.6.6, 203.0.113.9") + [("X-Forwarded-Proto", "https")]
    assert asyncio.run(_seen(_wildcard_echo(), headers)) == ("203.0.113.9", 0, "https")


def test_wildcard_forwarded_header_is_ignored():
    headers = [("Forwarded", "for=6.6.6.6")]
    seen = asyncio.run(_seen(_wildcard_echo(), headers, peer=("192.0.2.10", 5555)))
    assert seen[0] == "192.0.2.10"


def test_wildcard_websocket_scope_uses_the_rightmost_entry():
    seen = {}

    async def inner(scope, receive, send):
        seen.update(client=scope["client"], scheme=scope["scheme"])

    app = ProxyHeadersMiddleware(RightmostForwardedForMiddleware(inner), trusted_hosts="*")
    scope = {
        "type": "websocket", "scheme": "ws", "client": ("127.0.0.1", 40000),
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9"), (b"x-forwarded-proto", b"https")],
    }
    asyncio.run(app(scope, None, None))
    assert seen == {"client": ("203.0.113.9", 0), "scheme": "wss"}


def test_lifespan_scope_passes_through_untouched():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope)

    scope = {"type": "lifespan"}
    asyncio.run(RightmostForwardedForMiddleware(inner)(scope, None, None))
    assert seen == [{"type": "lifespan"}]


# --- create_app applies the middleware only for the bare wildcard ---


_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://unused/unused",
    "INGEST_PASSWORD": "ingest-password",
    "SESSION_SECRET": "session-secret-long-enough-for-config",
}


def _created_app(monkeypatch, forwarded_allow_ips):
    from app.main import create_app

    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("DEV_NO_AUTH", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", forwarded_allow_ips)
    app = create_app(Config.from_env())

    @app.get("/test-client")
    async def test_client(request: Request):
        return PlainTextResponse(f"{request.client.host}|{request.client.port}|{request.url.scheme}")

    return app


def test_create_app_under_wildcard_uses_the_rightmost_entry(monkeypatch):
    app = _created_app(monkeypatch, "*")
    assert app.user_middleware[0].cls is RightmostForwardedForMiddleware
    served = ProxyHeadersMiddleware(app, trusted_hosts="*")

    async def run():
        transport = httpx.ASGITransport(app=served, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/test-client", headers={
                "X-Forwarded-For": "6.6.6.6, 203.0.113.9", "X-Forwarded-Proto": "https"})

    assert asyncio.run(run()).text == "203.0.113.9|0|https"


def test_create_app_with_exact_trust_keeps_uvicorns_right_to_left_walk(monkeypatch):
    # With an exact address, uvicorn skips trusted hops from the right; the
    # rightmost-entry rule would pick 127.0.0.1 here instead.
    app = _created_app(monkeypatch, "127.0.0.1")
    assert all(m.cls is not RightmostForwardedForMiddleware for m in app.user_middleware)
    served = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")

    async def run():
        transport = httpx.ASGITransport(app=served, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/test-client", headers={
                "X-Forwarded-For": "203.0.113.9, 127.0.0.1"})

    assert asyncio.run(run()).text == "203.0.113.9|0|http"


def test_create_app_does_not_treat_padded_or_listed_wildcard_as_wildcard(monkeypatch):
    for value in (" * ", "127.0.0.1,*"):
        app = _created_app(monkeypatch, value)
        assert all(m.cls is not RightmostForwardedForMiddleware for m in app.user_middleware)


# --- FORWARDED_ALLOW_IPS=* with an appending proxy: the limiters key on the proxy's view ---


PROXY = ("127.0.0.1", 40000)


def _appended(sent, seen_by_proxy):
    # What an appending proxy forwards for a client that sent `sent`.
    return f"{sent}, {seen_by_proxy}" if sent else seen_by_proxy


def test_wildcard_ingest_limiter_ignores_rotating_leading_entries():
    app = _ingest_app(trusted_hosts="*")
    client_address = "203.0.113.5"

    async def run():
        results = []
        for i in range(MAX_FAILURES + 2):
            response = await _post_ingest(app, peer=PROXY, xff=_appended(f"10.0.0.{i}", client_address))
            results.append(response.status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [401] * MAX_FAILURES
    assert results[MAX_FAILURES:] == [503] * 2


def test_wildcard_ingest_leading_entry_does_not_count_against_that_address():
    app = _ingest_app(trusted_hosts="*")
    other_address = "198.51.100.7"

    async def run():
        for _ in range(MAX_FAILURES + 2):
            await _post_ingest(app, peer=PROXY, xff=_appended(other_address, "203.0.113.5"))
        return await _post_ingest(app, peer=PROXY, xff=_appended(None, other_address))

    assert asyncio.run(run()).status_code == 401


def test_wildcard_login_limiter_ignores_rotating_leading_entries():
    app, templates = _login_app(trusted_hosts="*")
    transport = httpx.ASGITransport(app=app, client=PROXY)

    async def run():
        results = []
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for i in range(MAX_FAILURES + 2):
                xff = _appended(f"192.0.2.{i}", "203.0.113.5")
                response = await _fail_login(app, templates, client, xff=xff)
                results.append(response.status_code)
        return results

    results = asyncio.run(run())
    assert results[:MAX_FAILURES] == [401] * MAX_FAILURES
    assert results[MAX_FAILURES:] == [429] * 2


def test_wildcard_login_leading_entry_does_not_count_against_that_address():
    app, templates = _login_app(trusted_hosts="*")
    other_address = "198.51.100.7"
    transport = httpx.ASGITransport(app=app, client=PROXY)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for _ in range(MAX_FAILURES + 2):
                await _fail_login(app, templates, client, xff=_appended(other_address, "203.0.113.5"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await _fail_login(app, templates, client, xff=_appended(None, other_address))

    assert asyncio.run(run()).status_code == 401
