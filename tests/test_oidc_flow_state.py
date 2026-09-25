from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import RedirectResponse

from app.auth import (
    OIDC_PROTECTED_ATTEMPT_KEY,
    _oidc_authorize_redirect,
    _oidc_protected_redirect,
)
import app.auth as auth


class _OAuthClient:
    def __init__(self):
        self.kwargs = None

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        self.kwargs = kwargs
        return RedirectResponse("https://idp.example/authorize")


def _request(session=None):
    client = _OAuthClient()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(oauth=SimpleNamespace(pocketid=client))
        ),
        session=session if session is not None else {},
        url_for=lambda name: "https://app.example/auth/callback",
    )
    return request, client


def test_link_authorization_uses_server_pending_state_bound_to_current_account(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example/")
    request.app.state.control_pool = object()
    account = {"id": 1, "auth_version": 4}
    attempts = []

    @asynccontextmanager
    async def connection(_pool):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)

    response = asyncio.run(
        _oidc_protected_redirect(request, action="link", account=account)
    )

    assert response.status_code == 307
    assert client.kwargs["state"].startswith("link.")
    assert client.kwargs["nonce"]
    assert attempts[0]["action"] == "link"
    assert attempts[0]["target"] == "https://idp.example"
    assert attempts[0]["account_id"] == 1
    assert attempts[0]["auth_version"] == 4
    assert request.session[OIDC_PROTECTED_ATTEMPT_KEY]["state"] == client.kwargs["state"]
    assert "password" not in repr(request.session).lower()


def test_login_authorization_uses_separate_state_and_nonce_and_clears_old_session():
    request, client = _request({"evil": "planted", "csrf": "old"})

    asyncio.run(_oidc_authorize_redirect(request))

    assert client.kwargs["state"].startswith("login.")
    assert client.kwargs["nonce"]
    assert request.session.get("evil") is None
    assert OIDC_PROTECTED_ATTEMPT_KEY not in request.session


def test_link_restart_reuses_browser_binding_and_replaces_cookie_attempt(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    attempts = []

    @asynccontextmanager
    async def connection(_pool):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    account = {"id": 1, "auth_version": 4}
    asyncio.run(_oidc_protected_redirect(request, action="link", account=account))
    old_state = client.kwargs["state"]
    asyncio.run(_oidc_protected_redirect(request, action="link", account=account))

    assert client.kwargs["state"] != old_state
    assert attempts[0]["browser_nonce"] == attempts[1]["browser_nonce"]
    assert request.session[OIDC_PROTECTED_ATTEMPT_KEY]["state"] == client.kwargs["state"]


def test_rejected_link_start_does_not_replace_existing_cookie_attempt(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    request.session[OIDC_PROTECTED_ATTEMPT_KEY] = {"state": "link.current"}

    @asynccontextmanager
    async def connection(_pool):
        yield object()

    async def reject(_conn, **_kwargs):
        return False

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", reject)

    with pytest.raises(HTTPException) as denied:
        asyncio.run(_oidc_protected_redirect(
            request, action="link", account={"id": 1, "auth_version": 4}
        ))
    assert denied.value.status_code == 400
    assert request.session[OIDC_PROTECTED_ATTEMPT_KEY]["state"] == "link.current"
    assert client.kwargs is None


def test_link_provider_departure_failure_consumes_bound_pending_attempt(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    consumed = []

    @asynccontextmanager
    async def connection(_pool):
        yield object()

    async def start(_conn, **_kwargs):
        return True

    async def consume(_conn, **kwargs):
        consumed.append(kwargs)

    async def depart(_request, _redirect_uri, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)
    client.authorize_redirect = depart

    with pytest.raises(RuntimeError):
        asyncio.run(_oidc_protected_redirect(
            request, action="link", account={"id": 1, "auth_version": 4}
        ))
    assert consumed[0]["account_id"] == 1
    assert consumed[0]["auth_version"] == 4
    assert OIDC_PROTECTED_ATTEMPT_KEY not in request.session


def test_protected_invitation_keeps_bearer_out_of_session_and_provider(monkeypatch):
    request, client = _request({"account_id": 7, "auth_version": 2, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    attempts = []

    @asynccontextmanager
    async def connection(_pool):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    token = "private-invitation-token"
    asyncio.run(_oidc_protected_redirect(
        request, action="invite", invite_token=token, timezone_name="UTC"
    ))

    assert attempts[0]["invite_token"] == token
    assert attempts[0]["target"] == "UTC"
    assert token not in repr(request.session)
    assert token not in repr(client.kwargs)
    assert "account_id" not in request.session
    assert client.kwargs["state"].startswith("invite.")
    assert client.kwargs["nonce"]


def test_fresh_action_requests_max_age_and_reuses_browser_binding(monkeypatch):
    request, client = _request({"account_id": 7, "auth_version": 2, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    attempts = []

    @asynccontextmanager
    async def connection(_pool):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    account = {"id": 7, "auth_version": 2}
    asyncio.run(_oidc_protected_redirect(
        request, action="reauth", account=account,
        proof_action="change_email", target="new@example.com",
    ))
    first_state = client.kwargs["state"]
    asyncio.run(_oidc_protected_redirect(
        request, action="reauth", account=account,
        proof_action="change_email", target="new@example.com",
    ))

    assert client.kwargs["max_age"] == 0
    assert client.kwargs["prompt"] == "login"
    assert client.kwargs["state"] != first_state
    assert attempts[0]["browser_nonce"] == attempts[1]["browser_nonce"]
    assert attempts[1]["account_id"] == 7
    assert attempts[1]["auth_version"] == 2
    assert attempts[1]["proof_action"] == "change_email"
    assert attempts[1]["target"] == "new@example.com"
