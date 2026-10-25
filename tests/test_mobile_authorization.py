"""Mobile sign-in browser routes and helpers that need no database: the
client registry, parameter validation, the setting, the per-client limit,
template escaping and button rules, and request-log redaction."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.access_log import QueryStringRedactionFilter
from app.api_v1 import WebSessionMiddleware
from app.main import make_templates
from app.mobile_authorization import (
    CLIENTS, COOKIE_NAME, Completion, PendingReview, ReplacedGrant, callback_url, pkce_matches,
    registered_callback, valid_challenge, valid_state,
)
from app.mobile_routes import INVALID_CLIENT_MESSAGE, RATE_LIMITED_MESSAGE, make_router
from app.password_reset import AttemptLimiter
from tests.test_page_rules import _ButtonParser, _bad_buttons

CALLBACK = "odograph://auth/callback"
VERIFIER = "v" * 20 + "-._~" + "V" * 40
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
STATE = "state-AbC_123.~!"


class _NoDatabase:
    def connection(self):
        raise AssertionError("this request must not reach the database")


def _app(*, mobile_signin=True, limiter=None):
    app = FastAPI()
    app.add_middleware(WebSessionMiddleware, secret_key="disposable-secret", https_only=False)
    config = SimpleNamespace(mobile_signin=mobile_signin, dev_no_auth=False,
                             display_tz=timezone.utc, app_version="test")
    app.state.config = config
    app.state.templates = make_templates(config)
    app.state.control_pool = _NoDatabase()
    app.state.mobile_authorize_limiter = limiter or AttemptLimiter("test", 50, 900)
    app.include_router(make_router())
    return app


def _params(**overrides):
    params = {"client_id": "odograph-ios", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
              "code_challenge_method": "S256", "state": STATE, "device_label": "Phone"}
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def _run(check, app=None):
    async def run():
        transport = httpx.ASGITransport(app=app or _app())
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            await check(client)
    asyncio.run(run())


def test_registry_holds_one_client_with_one_exact_callback():
    assert CLIENTS == {"odograph-ios": CALLBACK}
    assert registered_callback("odograph-ios", CALLBACK)
    for client_id, redirect_uri in [
        ("odograph-ios", CALLBACK + "/"), ("odograph-ios", "odograph://auth/callback?x=1"),
        ("odograph-ios", "ODOGRAPH://auth/callback"), ("odograph-ios", "https://evil.example/cb"),
        ("odograph-android", CALLBACK), ("Odograph-iOS", CALLBACK), (None, CALLBACK),
        ("odograph-ios", None),
    ]:
        assert not registered_callback(client_id, redirect_uri), (client_id, redirect_uri)


def test_state_challenge_and_pkce_checks():
    assert valid_state(STATE) and valid_state("x" * 256)
    for bad in ("", "x" * 257, "has space", "tab\t", "café", None, 5):
        assert not valid_state(bad), bad
    assert valid_challenge(CHALLENGE)
    for bad in (CHALLENGE[:-1], CHALLENGE + "A", CHALLENGE[:-1] + "=", CHALLENGE[:-1] + "+", None):
        assert not valid_challenge(bad)
    assert pkce_matches(VERIFIER, CHALLENGE)
    for bad in (VERIFIER[:-1] + "x", "short", "x" * 129, VERIFIER + " ", None):
        assert not pkce_matches(bad, CHALLENGE)
    assert not pkce_matches(VERIFIER, CHALLENGE.lower())


def test_callback_urls_encode_state_verbatim():
    assert callback_url(CALLBACK, error="invalid_request", state="a&b=c?d#e") == (
        "odograph://auth/callback?error=invalid_request&state=a%26b%3Dc%3Fd%23e")
    approved = Completion(CALLBACK, "s&t", code="C" * 43)
    assert parse_qs(urlsplit(approved.url()).query) == {"code": ["C" * 43], "state": ["s&t"]}
    assert "C" * 43 not in repr(approved) and "s&t" not in repr(approved)
    denied = Completion(CALLBACK, "s", error="access_denied")
    assert denied.url() == "odograph://auth/callback?error=access_denied&state=s"


@pytest.mark.parametrize("method, path", [
    ("GET", "/mobile/authorize"), ("GET", "/mobile/authorize/confirm"),
    ("POST", "/mobile/authorize/confirm"),
])
def test_every_browser_route_is_404_while_signin_is_off(method, path):
    from app.main import create_app
    from tests.auth_db_fixtures import auth_config

    async def check(client):
        response = await client.request(method, path, params=_params() if method == "GET" else None,
                                        data={"decision": "approve", "csrf_token": "x"} if method == "POST" else None)
        assert response.status_code == 404
        assert COOKIE_NAME not in response.headers.get("set-cookie", "")
    _run(check, create_app(auth_config("postgresql://unused@127.0.0.1:1/unused")))


def test_the_router_also_refuses_while_signin_is_off():
    async def check(client):
        assert (await client.get("/mobile/authorize", params=_params())).status_code == 404
        assert (await client.get("/mobile/authorize/confirm")).status_code == 404
    _run(check, _app(mobile_signin=False))


@pytest.mark.parametrize("overrides", [
    {"client_id": "odograph-android"}, {"client_id": None}, {"redirect_uri": "https://evil.example/cb"},
    {"redirect_uri": CALLBACK + "/"}, {"redirect_uri": None}, {"client_id": "Odograph-iOS"},
])
def test_wrong_client_or_callback_renders_an_error_and_never_redirects(overrides):
    async def check(client):
        response = await client.get("/mobile/authorize", params=_params(**overrides))
        assert response.status_code == 400
        assert "location" not in response.headers and COOKIE_NAME not in response.headers.get("set-cookie", "")
        assert INVALID_CLIENT_MESSAGE in response.text
        assert STATE not in response.text and CHALLENGE not in response.text
    _run(check)


def test_repeated_client_parameters_are_refused_like_a_wrong_client():
    async def check(client):
        query = "&".join(f"{key}={value}" for key, value in _params().items())
        response = await client.get(f"/mobile/authorize?{query}&redirect_uri=odograph://other")
        assert response.status_code == 400 and "location" not in response.headers
    _run(check)


@pytest.mark.parametrize("overrides, echoes_state", [
    ({"code_challenge": CHALLENGE[:-1]}, True),
    ({"code_challenge": None}, True),
    ({"code_challenge_method": "plain"}, True),
    ({"code_challenge_method": None}, True),
    ({"state": None}, False),
    ({"state": "x" * 257}, False),
    ({"state": "has space"}, False),
    ({"replaces": "not-a-grant-id"}, True),
])
def test_other_invalid_parameters_redirect_to_the_callback(overrides, echoes_state):
    async def check(client):
        response = await client.get("/mobile/authorize", params=_params(**overrides))
        assert response.status_code == 303
        target = urlsplit(response.headers["location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
        expected = {"error": ["invalid_request"]}
        if echoes_state:
            expected["state"] = [STATE]
        assert parse_qs(target.query) == expected
        assert COOKIE_NAME not in response.headers.get("set-cookie", "")
    _run(check)


def test_the_per_client_limit_counts_every_start():
    limiter = AttemptLimiter("test", 2, 900)

    async def check(client):
        for _ in range(2):
            assert (await client.get("/mobile/authorize", params=_params(state=None))).status_code == 303
        limited = await client.get("/mobile/authorize", params=_params())
        assert limited.status_code == 429 and RATE_LIMITED_MESSAGE in limited.text
        assert "location" not in limited.headers and COOKIE_NAME not in limited.headers.get("set-cookie", "")
        # A wrong client never counts and is still told so.
        assert (await client.get("/mobile/authorize", params=_params(client_id="x"))).status_code == 400
    _run(check, _app(limiter=limiter))


def test_request_log_drops_the_authorize_query():
    target = "/mobile/authorize?" + "&".join(f"{k}={v}" for k, v in _params(replaces="g" * 22).items())
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                               ("192.0.2.10", "GET", target, "1.1", 303), None)
    assert QueryStringRedactionFilter().filter(record)
    message = record.getMessage()
    assert message == '192.0.2.10 - "GET /mobile/authorize HTTP/1.1" 303'
    for value in (STATE, CHALLENGE, "Phone", "g" * 22):
        assert value not in message


def _review(**overrides):
    values = dict(device_label="<img src=x onerror=alert(1)>Phone", expires_at=datetime.now(timezone.utc),
                  active_grants=1, replaces=None, at_limit=False)
    values.update(overrides)
    return PendingReview(**values)


def _render_confirm(review):
    return make_templates(SimpleNamespace(display_tz=timezone.utc, app_version="test")).env.get_template(
        "mobile_authorize_confirm.html").render(
            user={"id": 1, "name": "<b>Ann</b>", "email": "ann@example.invalid", "is_admin": False,
                  "has_avatar": False, "avatar_version": 0},
            csrf="token", account_email="ann@example.invalid", review=review, grant_limit=10)


def _confirm_variants():
    replaced = ReplacedGrant("g" * 22, "<script>old</script>", datetime(2026, 9, 1, tzinfo=timezone.utc), None)
    return {
        "plain": _render_confirm(_review()),
        "replaces": _render_confirm(_review(replaces=replaced)),
        "at limit": _render_confirm(_review(active_grants=10, at_limit=True)),
    }


def test_confirm_page_escapes_untrusted_text_and_names_the_account():
    for label, body in _confirm_variants().items():
        assert "<img src=x" not in body and "&lt;img src=x onerror=alert(1)&gt;Phone" in body, label
        assert "<b>Ann</b>" not in body and "&lt;b&gt;Ann&lt;/b&gt;" in body, label
        assert "ann@example.invalid" in body
    replaced = _confirm_variants()["replaces"]
    assert "<script>old" not in replaced and "&lt;script&gt;old&lt;/script&gt;" in replaced
    assert "Sep 1, 2026" in replaced


def test_confirm_page_buttons_follow_the_shared_control_rules():
    for label, body in _confirm_variants().items():
        parser = _ButtonParser()
        parser.feed(body)
        assert parser.buttons, label
        assert _bad_buttons(parser.buttons) == [], label
        assert "hx-confirm" not in body
    approve = _ButtonParser()
    approve.feed(_confirm_variants()["plain"])
    decisions = {(b["value"], b["class"]) for b in approve.buttons if b.get("name") == "decision"}
    assert decisions == {("approve", "control control-primary auth-submit"),
                         ("deny", "control control-secondary auth-submit")}


def test_at_the_limit_the_only_button_returns_to_the_app():
    body = _confirm_variants()["at limit"]
    parser = _ButtonParser()
    parser.feed(body)
    decisions = [b for b in parser.buttons if b.get("name") == "decision"]
    assert [(b["value"], b["class"]) for b in decisions] == [("deny", "control control-primary auth-submit")]
    assert 'href="/settings/account"' in body
    assert "10 signed-in apps" in body
    assert 'value="approve"' not in body


def test_return_page_links_to_the_callback_and_escapes_it():
    templates = make_templates(SimpleNamespace(display_tz=timezone.utc, app_version="test"))
    url = Completion(CALLBACK, 'x"><script>', code="C" * 43).url()
    body = templates.env.get_template("mobile_authorize_return.html").render(
        callback_url=url, message="done", csp_nonce="nonce")
    assert "<script>" not in body.split("<body>", 1)[1].split("<script nonce")[0]
    assert 'id="mobile-callback-link"' in body
    assert 'class="control control-primary auth-submit"' in body
