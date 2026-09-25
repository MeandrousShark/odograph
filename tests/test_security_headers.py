"""SecurityHeadersMiddleware (app/main.py): the exact D2 header set on an
HTML response, a distinct per-request nonce that matches what the template
actually rendered, HSTS staying off unless HSTS_MAX_AGE is set and the
request is HTTPS, img-src following a configurable MAP_TILE_URL, and no
template retaining an inline event-handler attribute CSP can't nonce.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

from app.config import Config
from app.main import create_app

ROOT = Path(__file__).parents[1]
REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://unused/unused",
    "INGEST_PASSWORD": "ingest-password",
    "SESSION_SECRET": "session-secret",
}


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    async def execute(self, *args, **kwargs):
        return self

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row=None):
        self.row = row

    def cursor(self, row_factory=None):
        return _Cursor(self.row)

    async def execute(self, query, *args, **kwargs):
        if "current_setting" in query:
            return _Cursor((None,))
        if "SELECT EXISTS" in query:
            return _Cursor((False,))
        return _Cursor(self.row)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exc_info):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self, timeout=None):
        return _ConnectionContext(self.connection_value)


def _build_app(monkeypatch, **env):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("DEV_NO_AUTH", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    app = create_app(Config.from_env())
    app.state.control_pool = _Pool(_Connection(None))
    return app


async def _get(app, path="/login", *, scheme="http"):
    transport = httpx.ASGITransport(app=app)
    base_url = f"{scheme}://testserver"
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        return await client.get(path)


def test_html_response_carries_the_exact_header_set(monkeypatch):
    app = _build_app(monkeypatch)
    response = asyncio.run(_get(app))

    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Permissions-Policy"] == (
        "geolocation=(), camera=(), microphone=(), payment=()"
    )
    assert "Strict-Transport-Security" not in response.headers

    csp = response.headers["Content-Security-Policy"]
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "img-src 'self' data: https://tile.openstreetmap.org" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "object-src 'none'" in csp
    assert "form-action 'self'" in csp
    match = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
    assert match, csp


def test_unauthenticated_private_redirect_is_not_cacheable(monkeypatch):
    app = _build_app(monkeypatch)
    response = asyncio.run(_get(app, "/trips"))
    assert response.status_code == 303
    assert response.headers["Cache-Control"] == "no-store, private"


def test_nonce_differs_per_request_and_matches_rendered_template(monkeypatch):
    app = _build_app(monkeypatch)

    first = asyncio.run(_get(app))
    second = asyncio.run(_get(app))

    def nonce_from(response):
        csp_nonce = re.search(
            r"nonce-([^']+)'", response.headers["Content-Security-Policy"]
        ).group(1)
        rendered_nonces = set(re.findall(r'nonce="([^"]+)"', response.text))
        assert rendered_nonces == {csp_nonce}
        return csp_nonce

    first_nonce = nonce_from(first)
    second_nonce = nonce_from(second)
    assert first_nonce != second_nonce


def test_hsts_absent_when_max_age_unset(monkeypatch):
    app = _build_app(monkeypatch)
    response = asyncio.run(_get(app, scheme="https"))
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_present_only_on_https_when_max_age_set(monkeypatch):
    app = _build_app(monkeypatch, HSTS_MAX_AGE="3600")

    https_response = asyncio.run(_get(app, scheme="https"))
    assert https_response.headers["Strict-Transport-Security"] == "max-age=3600"

    http_response = asyncio.run(_get(app, scheme="http"))
    assert "Strict-Transport-Security" not in http_response.headers


# A referrer policy that suppresses the header entirely on cross-origin
# requests breaks the map: OpenStreetMap's tile servers reject a refererless
# request with a 403 error tile. These are the only two values that do that.
REFERER_SUPPRESSING_POLICIES = {"no-referrer", "same-origin"}


def test_referrer_policy_still_sends_an_origin_to_the_tile_host(monkeypatch):
    app = _build_app(monkeypatch)
    response = asyncio.run(_get(app))
    assert response.headers["Referrer-Policy"] not in REFERER_SUPPRESSING_POLICIES


def test_img_src_follows_a_custom_map_tile_url(monkeypatch):
    app = _build_app(monkeypatch, MAP_TILE_URL="https://tiles.example.net/{z}/{x}/{y}.png")
    response = asyncio.run(_get(app))
    csp = response.headers["Content-Security-Policy"]
    assert "img-src 'self' data: https://tiles.example.net" in csp
    assert "tile.openstreetmap.org" not in csp


INLINE_HANDLER_RE = re.compile(
    r'\son(click|change|submit|load|input|error|keyup|keydown|mouseover|mouseout|blur|focus)\s*='
)


def test_no_template_retains_an_inline_event_handler_attribute():
    templates_dir = ROOT / "app" / "templates"
    offenders = []
    for path in templates_dir.glob("*.html"):
        text = path.read_text()
        if INLINE_HANDLER_RE.search(text) or "hx-on" in text:
            offenders.append(path.name)
    assert offenders == []
