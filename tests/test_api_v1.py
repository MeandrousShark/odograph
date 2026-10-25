"""API v1 conventions that need no database: envelope, bearer parsing,
cookie isolation, capabilities and the token request shape."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Form, Request
from starlette.responses import Response

from app.api_errors import ERRORS, register_api_errors
from app.api_grants import (
    DEFAULT_DEVICE_LABEL, DEVICE_LABEL_MAX_LENGTH, IssuedTokens, format_instant,
    normalize_device_label, token_digest,
)
from app.api_v1 import WebSessionMiddleware, make_router
from app.capacity_routes import busy_response
from app.ingest import FailedAuthLimiter

TOKEN = "A" * 43


class _NoDatabase:
    def connection(self):
        raise AssertionError("this request must not reach the database")


def _app(limiter=None, *, mobile_signin=False):
    app = FastAPI()
    app.add_middleware(WebSessionMiddleware, secret_key="disposable-secret", https_only=False)
    app.state.config = SimpleNamespace(mobile_signin=mobile_signin)
    app.state.api_auth_limiter = limiter or FailedAuthLimiter(50, 900)
    app.state.control_pool = _NoDatabase()

    @app.post("/test/session")
    async def session(request: Request):
        request.session.update(account_id=1, auth_version=1, csrf="csrf")
        return Response(status_code=204)

    @app.get("/test/session")
    async def read_session(request: Request):
        return dict(request.session)

    @app.post("/test/form")
    async def form(value: int = Form(...)):
        return {"value": value}

    register_api_errors(app)
    app.include_router(make_router())
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _run(check, app=None):
    async def run():
        async with _client(app or _app()) as client:
            await check(client)
    asyncio.run(run())


def test_device_label_normalization():
    assert normalize_device_label("  Pixel 9  ") == "Pixel 9"
    assert normalize_device_label("<b>Phone</b>") == "<b>Phone</b>"
    assert normalize_device_label("Ph\u0000o\u202ene\u200b\u2028\n") == "Phone"
    assert normalize_device_label("Cafe\u0301") == "Caf\u00e9"
    assert normalize_device_label("x" * 100) == "x" * DEVICE_LABEL_MAX_LENGTH
    assert normalize_device_label(("a" * 63) + " b") == "a" * 63
    for empty in ("", "   ", "\u0000\u200b", None, 5):
        assert normalize_device_label(empty) == DEFAULT_DEVICE_LABEL


def test_instant_format_is_utc_with_fraction_and_z():
    local = datetime(2026, 10, 25, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=-7)))
    assert format_instant(local) == "2026-10-25T10:04:05.000006Z"
    assert format_instant(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00.000000Z"


def test_token_request_repr_hides_the_refresh_token():
    from app.api_v1 import TokenRequest
    assert TOKEN not in repr(TokenRequest(grant_type="refresh_token", refresh_token=TOKEN))


def test_device_label_strips_before_composing():
    # A stripped format character between a base and its combining mark must
    # not leave the result decomposed.
    assert normalize_device_label("e\u200b\u0301") == "\u00e9"


def test_token_digest_shape_and_issued_tokens_repr_hide_secrets():
    assert token_digest(TOKEN) is not None and len(token_digest(TOKEN)) == 64
    for bad in ("", "A" * 42, "A" * 44, "A" * 42 + "=", None, 1):
        assert token_digest(bad) is None
    issued = IssuedTokens("g" * 22, datetime.now(timezone.utc), "access-secret", "refresh-secret")
    assert "secret" not in repr(issued)


def test_capabilities_reveal_nothing_account_or_version_specific():
    async def check(client):
        expected = {
            "product": "odograph",
            "api_versions": ["v1"],
            "authorization": {"authorize_path": "/mobile/authorize", "token_path": "/api/v1/auth/token"},
            "mobile_signin_available": False,
        }
        plain = await client.get("/api/v1/capabilities")
        assert plain.status_code == 200 and plain.json() == expected
        await client.post("/test/session")
        bearer = await client.get("/api/v1/capabilities", headers={"Authorization": f"Bearer {TOKEN}"})
        assert bearer.json() == expected
        assert "set-cookie" not in bearer.headers
        assert not any(name.lower().startswith("access-control-") for name in bearer.headers)
    _run(check)


def test_capabilities_report_the_mobile_signin_setting():
    async def check(client):
        response = await client.get("/api/v1/capabilities")
        assert response.json()["mobile_signin_available"] is True
    _run(check, _app(mobile_signin=True))


def test_token_request_repr_hides_the_code_and_verifier():
    from app.api_v1 import TokenRequest
    request = TokenRequest(grant_type="authorization_code", code=TOKEN, code_verifier="V" * 64)
    assert TOKEN not in repr(request) and "V" * 64 not in repr(request)


def test_authorization_code_exchange_is_refused_while_signin_is_off():
    async def check(client):
        for body in ({"grant_type": "authorization_code", "code": TOKEN, "code_verifier": "V" * 64},
                     {"grant_type": "authorization_code"}):
            response = await client.post("/api/v1/auth/token", json=body)
            assert response.status_code == 403
            assert response.json() == {"error": {"code": "signin_disabled",
                                                 "message": ERRORS["signin_disabled"][1], "fields": None}}
            assert TOKEN not in response.text
    _run(check)


def test_authorization_code_request_validation_without_echoing_input():
    async def check(client):
        cases = [
            ({"grant_type": "authorization_code"},
             {"code": "This field is required.", "code_verifier": "This field is required."}),
            ({"grant_type": "authorization_code", "code": TOKEN}, {"code_verifier": "This field is required."}),
            ({"grant_type": "authorization_code", "code_verifier": TOKEN}, {"code": "This field is required."}),
        ]
        for body, fields in cases:
            response = await client.post("/api/v1/auth/token", json=body)
            assert response.status_code == 422, body
            assert response.json()["error"]["fields"] == fields
            assert TOKEN not in response.text
    _run(check, _app(mobile_signin=True))


def test_malformed_codes_are_invalid_grants_without_a_database_lookup():
    async def check(client):
        for code in ("short", "A" * 44, "A" * 42 + "="):
            response = await client.post("/api/v1/auth/token", json={
                "grant_type": "authorization_code", "code": code, "code_verifier": "V" * 64})
            assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_grant"
        limited = await client.post("/api/v1/auth/token", json={
            "grant_type": "authorization_code", "code": TOKEN, "code_verifier": "V" * 64})
        assert limited.status_code == 429 and limited.headers["retry-after"] == "900"
    _run(check, _app(FailedAuthLimiter(3, 900), mobile_signin=True))


def test_revoke_requires_a_bearer_token():
    async def check(client):
        await client.post("/test/session")
        response = await client.post("/api/v1/auth/revoke")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"
        assert response.headers["www-authenticate"] == "Bearer"
    _run(check)


def test_cookie_only_and_malformed_bearer_requests_get_one_uniform_401():
    async def check(client):
        await client.post("/test/session")
        assert (await client.get("/test/session")).json()["account_id"] == 1
        attempts = [
            {},
            {"Authorization": ""},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer "},
            {"Authorization": "Basic " + TOKEN},
            {"Authorization": "Bearer short"},
            {"Authorization": f"Bearer {TOKEN} extra"},
        ]
        responses = [await client.get("/api/v1/me", headers=headers) for headers in attempts]
        responses.append(await client.get(
            "/api/v1/me", headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")]))
        first = responses[0]
        assert first.status_code == 401
        assert first.json() == {"error": {"code": "invalid_token", "message": ERRORS["invalid_token"][1],
                                          "fields": None}}
        assert first.headers["www-authenticate"] == "Bearer"
        for response in responses:
            assert (response.status_code, response.content, response.headers["www-authenticate"]) == (
                first.status_code, first.content, first.headers["www-authenticate"])
            assert "set-cookie" not in response.headers
        # The browser session is untouched by API requests.
        assert (await client.get("/test/session")).json()["account_id"] == 1
    _run(check)


def test_failed_bearer_attempts_are_rate_limited():
    async def check(client):
        for _ in range(3):
            assert (await client.get("/api/v1/me")).status_code == 401
        limited = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {TOKEN}"})
        assert limited.status_code == 429 and limited.headers["retry-after"] == "900"
        assert limited.json()["error"] == {"code": "rate_limited", "message": ERRORS["rate_limited"][1],
                                           "fields": None}
        refused = await client.post("/api/v1/auth/token",
                                    json={"grant_type": "refresh_token", "refresh_token": TOKEN})
        assert refused.status_code == 429 and refused.headers["retry-after"] == "900"
    _run(check, _app(FailedAuthLimiter(3, 900)))


def test_token_request_validation_uses_the_envelope_without_echoing_input():
    async def check(client):
        cases = [
            ({"grant_type": "password", "refresh_token": TOKEN}, {"grant_type": "This value is not supported."}),
            ({"grant_type": "refresh_token"}, {"refresh_token": "This field is required."}),
            ({"refresh_token": TOKEN}, {"grant_type": "This field is required."}),
            ({"grant_type": 7, "refresh_token": TOKEN}, {"grant_type": "This value is not valid."}),
            ([TOKEN], {"body": "The request body must be a JSON object."}),
        ]
        for body, fields in cases:
            response = await client.post("/api/v1/auth/token", json=body)
            assert response.status_code == 422, body
            assert response.json() == {"error": {"code": "validation_failed",
                                                 "message": ERRORS["validation_failed"][1], "fields": fields}}
            assert TOKEN not in response.text
        broken = await client.post("/api/v1/auth/token", content=b"{" + TOKEN.encode(),
                                   headers={"content-type": "application/json"})
        assert broken.status_code == 422 and TOKEN not in broken.text
        assert broken.json()["error"]["fields"] == {"body": "The request body must be a JSON object."}
    _run(check)


def test_unknown_api_paths_use_the_envelope_and_web_paths_keep_defaults():
    async def check(client):
        missing = await client.get("/api/v1/nothing")
        assert missing.status_code == 404
        assert missing.json() == {"error": {"code": "not_found", "message": "Not found.", "fields": None}}
        web_missing = await client.get("/nothing")
        assert web_missing.status_code == 404 and web_missing.json() == {"detail": "Not Found"}
        web_invalid = await client.post("/test/form", data={"value": "x"})
        assert web_invalid.status_code == 422 and "detail" in web_invalid.json()
    _run(check)


def test_busy_api_responses_use_the_envelope():
    def request(path, headers=()):
        return Request({"type": "http", "method": "GET", "path": path, "query_string": b"",
                        "headers": list(headers), "server": ("test", 80), "scheme": "http"})
    api = busy_response(request("/api/v1/me", [(b"accept", b"text/html")]))
    assert api.status_code == 503 and api.headers["retry-after"] == "1"
    assert api.body == (b'{"error":{"code":"busy","message":"The server is busy. Please try again.",'
                        b'"fields":null}}')
    web = busy_response(request("/settings"))
    assert web.status_code == 503 and b"capacity_busy" in web.body


@pytest.mark.parametrize("path", ["/api/v1/me", "/api/v1/capabilities"])
def test_api_paths_never_decode_or_set_the_session_cookie(path):
    async def check(client):
        await client.post("/test/session")
        cookie = client.cookies.get("session")
        assert cookie
        response = await client.get(path)
        assert "set-cookie" not in response.headers
        assert client.cookies.get("session") == cookie
    _run(check)
