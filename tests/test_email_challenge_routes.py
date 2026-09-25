"""Focused route coverage for email challenges without a database or SMTP."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse

import app.auth as auth


ACCOUNT = {
    "id": 7, "email": "old@example.com", "password_hash": "hash",
    "is_admin": True, "is_enabled": True, "auth_version": 3,
    "avatar_mime": None, "avatar_updated_at": None,
}


class _Templates:
    def TemplateResponse(self, request, name, context, status_code=200, headers=None):
        message = context.get("error") or context.get("success") or ""
        return PlainTextResponse(f"{name}:{message}", status_code=status_code, headers=headers)


def _app(monkeypatch, *, smtp_host="smtp.example.com"):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.state.config = SimpleNamespace(
        dev_no_auth=False, smtp_host=smtp_host, smtp_port=587,
        smtp_username="", smtp_password="", smtp_security="starttls",
        smtp_tls_insecure=False, email_from="odograph@example.com",
        app_url="https://odograph.example.com", account_avatar_max_bytes=1024,
    )
    app.state.control_pool = object()
    app.state.oauth = None
    app.state.templates = _Templates()
    app.state.login_limiter = auth.FailedAuthLimiter(5, 60)

    @asynccontextmanager
    async def fake_connection(pool):
        yield object()

    async def fake_get_account(conn, account_id):
        return ACCOUNT.copy()

    async def fake_verified(request, user, current_password, *, generic_error, precheck_error=None):
        if current_password != "correct":
            raise auth._AccountActionRejected(ACCOUNT.copy(), generic_error, 401)
        return ACCOUNT.copy()

    async def fake_verified_state(conn, account_id):
        return False

    monkeypatch.setattr(auth, "control_connection", fake_connection)
    monkeypatch.setattr(auth, "get_account", fake_get_account)
    monkeypatch.setattr(auth, "_verified_account", fake_verified)
    monkeypatch.setattr(auth, "is_current_email_verified", fake_verified_state)

    async def fake_user(request: Request):
        if not request.session.get("signed_in"):
            raise auth.AuthRedirect()
        request.state.principal = SimpleNamespace(auth_version=3)
        return {"id": 7, "email": ACCOUNT["email"]}

    app.dependency_overrides[auth.require_user] = fake_user

    @app.exception_handler(auth.AuthRedirect)
    async def redirect_login(request, error):
        return PlainTextResponse("sign in", status_code=401)

    @app.get("/seed")
    async def seed(request: Request):
        request.session["signed_in"] = True
        request.session["csrf"] = "csrf-test"
        return PlainTextResponse("ok")

    app.include_router(auth.make_router())
    return app


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_change_request_delivers_only_to_new_address_with_fragment_token(monkeypatch):
    calls = []

    async def issue(conn, account_id, auth_version, purpose, target):
        calls.append((account_id, auth_version, purpose, target))
        return "secret-token"

    class FakeMailer:
        def __init__(self, *args):
            calls.append(("to", args[-1]))

        def compose(self, subject, body):
            calls.append(("body", body))
            return body

        async def send(self, message):
            calls.append(("sent",))

    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "Mailer", FakeMailer)
    app = _app(monkeypatch)

    async def run():
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/change/request", data={
                "new_email": " New@Example.com ", "new_email_confirm": "new@example.com",
                "current_password": "correct", "csrf_token": "csrf-test",
            })

    response = asyncio.run(run())
    assert response.status_code == 200
    assert calls[0] == (7, 3, auth.PURPOSE_CHANGE, "new@example.com")
    assert calls[1] == ("to", "new@example.com")
    assert "confirm#purpose=change_email&token=secret-token" in calls[2][1]
    assert "secret-token" not in response.text


def test_change_request_rejects_unsafe_address_before_issue(monkeypatch):
    async def unexpected_issue(*args):
        raise AssertionError("challenge issued for unsafe address")

    monkeypatch.setattr(auth, "issue_email_challenge", unexpected_issue)
    app = _app(monkeypatch)

    async def run(address):
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/change/request", data={
                "new_email": address, "new_email_confirm": address,
                "current_password": "correct", "csrf_token": "csrf-test",
            })

    for address in ("a@example.com\nBcc:evil@example.com", "a@example.com,evil@example.com", "x" * 255 + "@example.com"):
        assert asyncio.run(run(address)).status_code == 400


def test_request_requires_csrf_and_reauthentication(monkeypatch):
    async def unexpected_issue(*args):
        raise AssertionError("challenge issued without admission")

    monkeypatch.setattr(auth, "issue_email_challenge", unexpected_issue)
    app = _app(monkeypatch)

    async def run(csrf, password):
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/verify/request", data={
                "current_password": password, "csrf_token": csrf,
            })

    assert asyncio.run(run("wrong", "correct")).status_code == 403
    assert asyncio.run(run("csrf-test", "wrong")).status_code == 401


def test_missing_smtp_and_issuance_rejection_do_not_expose_target_status(monkeypatch):
    issued = []

    async def issue(*args):
        issued.append(True)
        return None

    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    unavailable = _app(monkeypatch, smtp_host="")
    available = _app(monkeypatch)

    async def run(app):
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/change/request", data={
                "new_email": "busy@example.com", "new_email_confirm": "busy@example.com",
                "current_password": "correct", "csrf_token": "csrf-test",
            })

    missing = asyncio.run(run(unavailable))
    assert missing.status_code == 503
    assert issued == []
    rejected = asyncio.run(run(available))
    assert rejected.status_code == 200
    assert issued == [True]
    assert "busy@example.com" not in rejected.text
    assert "eligible" in rejected.text


def test_oversize_form_is_rejected_before_issuance(monkeypatch):
    async def unexpected_issue(*args):
        raise AssertionError("issued after oversized request")

    monkeypatch.setattr(auth, "issue_email_challenge", unexpected_issue)
    app = _app(monkeypatch)

    async def run():
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/verify/request", data={
                "current_password": "x" * auth.MAX_EMAIL_FORM_BYTES,
                "csrf_token": "csrf-test",
            })

    assert asyncio.run(run()).status_code == 413


def test_delivery_failure_revokes_challenge_without_exposing_token(monkeypatch):
    revoked = []

    async def issue(*args):
        return "secret-token"

    async def revoke(conn, account_id, purpose, token):
        revoked.append((account_id, purpose, token))

    class FailingMailer:
        def __init__(self, *args):
            pass

        def compose(self, subject, body):
            return body

        async def send(self, message):
            raise RuntimeError("secret-token in SMTP exception")

    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "revoke_email_challenge", revoke)
    monkeypatch.setattr(auth, "Mailer", FailingMailer)
    app = _app(monkeypatch)

    async def run():
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/verify/request", data={
                "current_password": "correct", "csrf_token": "csrf-test",
            })

    response = asyncio.run(run())
    assert response.status_code == 503
    assert "secret-token" not in response.text
    assert revoked == [(7, auth.PURPOSE_CURRENT, "secret-token")]


def test_confirmation_requires_session_and_csrf_and_renews_change_session(monkeypatch):
    calls = []

    async def consume(conn, account_id, auth_version, purpose, token):
        calls.append((account_id, auth_version, purpose, token))
        return {**ACCOUNT, "email": "new@example.com", "auth_version": 4}

    monkeypatch.setattr(auth, "consume_email_challenge", consume)
    app = _app(monkeypatch)

    async def run():
        async with await _client(app) as client:
            signed_out_page = await client.get("/settings/account/email/confirm")
            unsigned = await client.post("/settings/account/email/confirm", data={
                "purpose": "change_email", "token": "secret-token", "csrf_token": "csrf-test",
            })
            await client.get("/seed")
            bad_csrf = await client.post("/settings/account/email/confirm", data={
                "purpose": "change_email", "token": "secret-token", "csrf_token": "bad",
            })
            good = await client.post("/settings/account/email/confirm", data={
                "purpose": "change_email", "token": "secret-token", "csrf_token": "csrf-test",
            })
            return signed_out_page, unsigned, bad_csrf, good

    signed_out_page, unsigned, bad_csrf, good = asyncio.run(run())
    assert signed_out_page.status_code == 200
    assert "no-store" in signed_out_page.headers["cache-control"]
    assert (unsigned.status_code, bad_csrf.status_code, good.status_code) == (401, 403, 200)
    assert calls == [(7, 3, auth.PURPOSE_CHANGE, "secret-token")]
    assert "secret-token" not in good.text
    assert "no-store" in good.headers["cache-control"]


def test_current_email_confirmation_keeps_session_version(monkeypatch):
    async def consume(conn, account_id, auth_version, purpose, token):
        return ACCOUNT.copy()

    def unexpected_renew(*args):
        raise AssertionError("current verification renewed the session")

    monkeypatch.setattr(auth, "consume_email_challenge", consume)
    monkeypatch.setattr(auth, "_set_account_session", unexpected_renew)
    app = _app(monkeypatch)

    async def run():
        async with await _client(app) as client:
            await client.get("/seed")
            return await client.post("/settings/account/email/confirm", data={
                "purpose": "verify_current", "token": "secret-token", "csrf_token": "csrf-test",
            })

    assert asyncio.run(run()).status_code == 200
