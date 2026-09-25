"""Reset request and confirmation routes without a database or SMTP."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, PlainTextResponse

import app.auth as auth
from app.main import SecurityHeadersMiddleware
from app.password_reset import AttemptLimiter, SecurityMailAdmission

TOKEN = "T" * 43


class _Templates:
    def TemplateResponse(self, request, name, context, status_code=200, headers=None):
        message = context.get("error") or context.get("notice") or ""
        return PlainTextResponse(f"{name}:{message}", status_code=status_code, headers=headers)


class _Queue:
    def __init__(self):
        self.submitted = []

    def submit_public(self, email):
        self.submitted.append(email)
        return True


def _app(monkeypatch, *, smtp_host="smtp.example.com", app_url="https://odograph.example.com",
         dev_no_auth=False, attempts=10):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.state.config = SimpleNamespace(
        dev_no_auth=dev_no_auth, smtp_host=smtp_host, email_from="odograph@example.com",
        app_url=app_url,
    )
    app.state.control_pool = object()
    app.state.templates = _Templates()
    app.state.login_limiter = auth.FailedAuthLimiter(5, 60)
    app.state.reset_request_client_limiter = AttemptLimiter("client", attempts, 60)
    app.state.reset_request_identifier_limiter = AttemptLimiter("identifier", attempts, 60)
    app.state.reset_validation_limiter = AttemptLimiter("validation", attempts, 60)
    app.state.security_mail = SecurityMailAdmission()
    app.state.password_reset_queue = _Queue()
    app.state.calls = []

    @asynccontextmanager
    async def fake_connection(pool):
        yield object()

    async def usable(conn, token):
        app.state.calls.append(("usable", token))
        return token == TOKEN

    async def consume(conn, token, password_hash):
        app.state.calls.append(("consume", token, password_hash))
        return 7

    def fake_hash(password):
        app.state.calls.append(("hash",))
        return "new-hash"

    monkeypatch.setattr(auth, "control_connection", fake_connection)
    monkeypatch.setattr(auth, "password_reset_usable", usable)
    monkeypatch.setattr(auth, "consume_password_reset", consume)
    monkeypatch.setattr(auth, "hash_password", fake_hash)

    @app.get("/seed")
    async def seed(request: Request):
        request.session["account_id"] = 9
        request.session["auth_version"] = 4
        request.session["csrf"] = "csrf-test"
        return PlainTextResponse("ok")

    @app.get("/session")
    async def session(request: Request):
        return dict(request.session)

    app.include_router(auth.make_router())
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _post(app, path, data):
    async def run():
        async with _client(app) as client:
            await client.get("/seed")
            response = await client.post(path, data=data)
            session = (await client.get("/session")).json()
            return response, session
    return asyncio.run(run())


def test_forgot_password_reply_is_identical_and_only_queues_valid_identifiers(monkeypatch):
    app = _app(monkeypatch)
    replies = []
    for email in (" Known@Example.com ", "unknown@example.com", "not an email", "x@example.com\r\nBcc: y"):
        response, _ = _post(app, "/forgot-password", {"email": email, "csrf_token": "csrf-test"})
        replies.append((response.status_code, response.text))
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
    assert len(set(replies)) == 1
    assert replies[0] == (200, f"forgot_password.html:{auth.RESET_REQUEST_NOTICE}")
    assert app.state.password_reset_queue.submitted == ["known@example.com", "unknown@example.com"]


def test_forgot_password_limits_stay_behind_the_generic_reply(monkeypatch):
    app = _app(monkeypatch, attempts=2)
    replies = []
    for _ in range(4):
        response, _ = _post(app, "/forgot-password", {"email": "a@example.com", "csrf_token": "csrf-test"})
        replies.append((response.status_code, response.text))
    assert len(set(replies)) == 1
    assert app.state.password_reset_queue.submitted == ["a@example.com", "a@example.com"]


def test_forgot_password_requires_csrf_and_exact_fields(monkeypatch):
    app = _app(monkeypatch)
    response, _ = _post(app, "/forgot-password", {"email": "a@example.com", "csrf_token": "wrong"})
    assert response.status_code == 403
    response, _ = _post(app, "/forgot-password", {
        "email": "a@example.com", "csrf_token": "csrf-test", "redirect": "https://evil.example"})
    assert response.status_code == 400
    response, _ = _post(app, "/forgot-password", {
        "email": "a@example.com", "csrf_token": "csrf-test", "account_id": "1"})
    assert response.status_code == 400
    assert app.state.password_reset_queue.submitted == []


@pytest.mark.parametrize("overrides", [
    {"smtp_host": ""}, {"app_url": ""}, {"app_url": "https://odograph.example.com?x=1"},
])
def test_forgot_password_without_delivery_queues_nothing(monkeypatch, overrides):
    app = _app(monkeypatch, **overrides)
    response, _ = _post(app, "/forgot-password", {"email": "a@example.com", "csrf_token": "csrf-test"})
    assert response.status_code == 200
    assert app.state.password_reset_queue.submitted == []


def test_reset_routes_are_absent_without_authentication(monkeypatch):
    app = _app(monkeypatch, dev_no_auth=True)

    async def run():
        async with _client(app) as client:
            return [
                (await client.get("/forgot-password")).status_code,
                (await client.get("/reset-password")).status_code,
            ]
    assert asyncio.run(run()) == [404, 404]


def test_reset_rejects_malformed_proof_and_weak_password_before_any_work(monkeypatch):
    app = _app(monkeypatch)
    base = {"password": "new password", "password_confirm": "new password", "csrf_token": "csrf-test"}
    for token in ("", "short", "T" * 44, "T" * 42 + "!"):
        response, _ = _post(app, "/reset-password", {**base, "token": token})
        assert response.status_code == 400
        assert response.text == f"reset_password.html:{auth.GENERIC_RESET_ERROR}"
    response, _ = _post(app, "/reset-password", {**base, "token": TOKEN, "password_confirm": "different"})
    assert response.status_code == 400
    response, _ = _post(app, "/reset-password", {**base, "token": TOKEN, "password": "short", "password_confirm": "short"})
    assert response.status_code == 400
    response, _ = _post(app, "/reset-password", {**base, "token": TOKEN, "csrf_token": "wrong"})
    assert response.status_code == 403
    assert app.state.calls == []


def test_unusable_proof_fails_generically_without_hashing(monkeypatch):
    app = _app(monkeypatch)
    response, session = _post(app, "/reset-password", {
        "token": "U" * 43, "password": "new password", "password_confirm": "new password",
        "csrf_token": "csrf-test"})
    assert response.status_code == 400
    assert response.text == f"reset_password.html:{auth.GENERIC_RESET_ERROR}"
    assert app.state.calls == [("usable", "U" * 43)]
    assert session["account_id"] == 9


def test_successful_reset_clears_this_browser_and_returns_to_sign_in(monkeypatch):
    app = _app(monkeypatch)
    response, session = _post(app, "/reset-password", {
        "token": f" {TOKEN} ", "password": "new password", "password_confirm": "new password",
        "csrf_token": "csrf-test"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?signed_out=1"
    assert session == {"login_notice": auth.RESET_COMPLETE_NOTICE}
    assert app.state.calls == [("usable", TOKEN), ("hash",), ("consume", TOKEN, "new-hash")]


def test_reset_attempts_are_bounded_per_client(monkeypatch):
    app = _app(monkeypatch, attempts=2)
    statuses = []
    for _ in range(3):
        response, _ = _post(app, "/reset-password", {
            "token": "U" * 43, "password": "new password", "password_confirm": "new password",
            "csrf_token": "csrf-test"})
        statuses.append(response.status_code)
    assert statuses == [400, 400, 429]


def test_reset_hashing_shares_bounded_password_admission(monkeypatch):
    app = _app(monkeypatch)

    async def saturated(operation):
        raise auth._AuthSaturated

    app.state.login_limiter.run_bounded = saturated
    response, _ = _post(app, "/reset-password", {
        "token": TOKEN, "password": "new password", "password_confirm": "new password",
        "csrf_token": "csrf-test"})
    assert response.status_code == 429
    assert app.state.calls == []


@pytest.mark.parametrize("path", ["/forgot-password", "/reset-password"])
def test_reset_pages_keep_private_headers_through_outer_middleware(path):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, tile_host="https://tiles.example", hsts_max_age=0)

    @app.get(path)
    async def page():
        return HTMLResponse("<h1>Reset</h1>")

    async def check():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    response = asyncio.run(check())
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Referrer-Policy"] == "no-referrer"
