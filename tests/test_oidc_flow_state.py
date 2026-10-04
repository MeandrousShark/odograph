from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import HTTPException
from starlette.responses import RedirectResponse

from app.auth import (
    OIDC_PROTECTED_ATTEMPT_KEY,
    _oidc_authorize_redirect,
    _oidc_protected_redirect,
    _validate_oidc_authorization_url,
)
import app.auth as auth
from app.main import make_templates
from tests.oidc_test_helpers import oidc_authorization_url


class _OAuthClient:
    def __init__(self):
        self.kwargs = None
        self.location = None

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        self.kwargs = kwargs
        for key in list(request.session):
            if key.startswith("_state_pocketid_"):
                request.session.pop(key)
        request.session[f"_state_pocketid_{kwargs['state']}"] = {
            "data": {"redirect_uri": redirect_uri, **kwargs}
        }
        if self.location is not None:
            location = self.location(kwargs) if callable(self.location) else self.location
            return RedirectResponse(location)
        query = urlencode({"redirect_uri": redirect_uri, **kwargs})
        return RedirectResponse(f"https://idp.example/authorize?{query}")


def _request(session=None):
    client = _OAuthClient()
    config = SimpleNamespace(display_tz="UTC", app_version="test")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                oauth=SimpleNamespace(pocketid=client),
                templates=make_templates(config),
                control_pool=object(),
                config=config,
            )
        ),
        state=SimpleNamespace(csp_nonce="test-nonce", config=config),
        session=session if session is not None else {},
        url_for=lambda name: "https://app.example/auth/callback",
    )
    return request, client


def test_link_authorization_uses_server_pending_state_bound_to_current_account(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://issuer.example/")
    request.app.state.control_pool = object()
    client.location = lambda kwargs: (
        "https://login.provider.example/authorize?" + urlencode(kwargs)
    )
    account = {"id": 1, "auth_version": 4}
    attempts = []

    @asynccontextmanager
    async def connection(_pool, *, lane="identity"):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)

    response = asyncio.run(
        _oidc_protected_redirect(request, action="link", account=account)
    )

    destination = oidc_authorization_url(response)
    assert urlsplit(destination).netloc == "login.provider.example"
    assert urlsplit(destination).netloc != "issuer.example"
    assert parse_qs(urlsplit(destination).query)["state"] == [client.kwargs["state"]]
    assert client.kwargs["state"].startswith("link.")
    assert client.kwargs["nonce"]
    assert attempts[0]["action"] == "link"
    assert attempts[0]["target"] == "https://issuer.example"
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
    async def connection(_pool, *, lane="identity"):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    account = {"id": 1, "auth_version": 4}
    first_response = asyncio.run(
        _oidc_protected_redirect(request, action="link", account=account)
    )
    assert oidc_authorization_url(first_response)
    old_state = client.kwargs["state"]
    second_response = asyncio.run(
        _oidc_protected_redirect(request, action="link", account=account)
    )
    assert oidc_authorization_url(second_response)

    assert client.kwargs["state"] != old_state
    assert attempts[0]["browser_nonce"] == attempts[1]["browser_nonce"]
    assert request.session[OIDC_PROTECTED_ATTEMPT_KEY]["state"] == client.kwargs["state"]


def test_cancelled_protected_redirect_consumes_attempt_after_caller_leaves(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    entered, release, consumed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def connection(_pool, *, lane="identity"):
        yield object()

    async def start(_conn, **_kwargs):
        return True

    async def consume(_conn, **kwargs):
        assert kwargs["action"] == "reauth"
        await release.wait()
        consumed.set()

    async def waiting_redirect(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)
    client.authorize_redirect = waiting_redirect

    async def run():
        caller = asyncio.create_task(_oidc_protected_redirect(
            request, action="reauth", account={"id": 1, "auth_version": 4},
            proof_action="add_password", target="",
        ))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert OIDC_PROTECTED_ATTEMPT_KEY not in request.session
        release.set()
        await asyncio.wait_for(consumed.wait(), 2)

    asyncio.run(run())


def test_rejected_link_start_does_not_replace_existing_cookie_attempt(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    request.session[OIDC_PROTECTED_ATTEMPT_KEY] = {"state": "link.current"}

    @asynccontextmanager
    async def connection(_pool, *, lane="identity"):
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


@pytest.mark.parametrize(
    "destination",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "//attacker.example/authorize",
        "https:///authorize",
        "https://provider.example:bad/authorize",
        "https://[invalid]/authorize",
        "https://[v1.provider]/authorize",
        "https://user:password@provider.example/authorize",
        "https://provider.example/%zz",
        "https://bad_host.example/authorize",
    ],
)
def test_unsafe_authorization_destination_fails_closed_and_consumes_attempt(
    monkeypatch, destination
):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://issuer.example")
    request.app.state.control_pool = object()
    client.location = destination
    started = []
    consumed = []

    @asynccontextmanager
    async def connection(_pool, *, lane="identity"):
        yield object()

    async def start(_conn, **kwargs):
        started.append(kwargs)
        return True

    async def consume(_conn, **kwargs):
        consumed.append(kwargs)

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_oidc_protected_redirect(
            request, action="link", account={"id": 1, "auth_version": 4}
        ))

    assert rejected.value.status_code == 502
    assert rejected.value.detail == auth.GENERIC_OIDC_ERROR
    assert len(consumed) == 1
    for key in ("action", "state", "nonce", "browser_nonce", "account_id", "auth_version"):
        assert consumed[0][key] == started[0][key]
    assert OIDC_PROTECTED_ATTEMPT_KEY not in request.session
    assert not any(key.startswith("_state_pocketid_") for key in request.session)


@pytest.mark.parametrize(
    "destination",
    [
        "http://localhost:18091/authorize",
        "https://login.provider.example/authorize",
        "https://[2001:db8::1]:8443/authorize",
    ],
)
def test_absolute_http_authorization_destinations_are_valid(destination):
    assert _validate_oidc_authorization_url(destination) == destination


def test_authorization_destination_may_use_a_different_host_than_the_issuer():
    issuer = "https://issuer.example"
    destination = "https://login.provider.example/authorize"

    assert urlsplit(issuer).hostname != urlsplit(destination).hostname
    assert _validate_oidc_authorization_url(destination) == destination


def test_link_provider_departure_failure_consumes_bound_pending_attempt(monkeypatch):
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    consumed = []

    @asynccontextmanager
    async def connection(_pool, *, lane="identity"):
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
    async def connection(_pool, *, lane="identity"):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    token = "private-invitation-token"
    client.location = (
        'https://idp.example/authorize?state=protected&value="'
        '><script>alert(1)</script>'
    )
    response = asyncio.run(_oidc_protected_redirect(
        request, action="invite", invite_token=token, timezone_name="UTC"
    ))

    destination = oidc_authorization_url(response)
    assert attempts[0]["invite_token"] == token
    assert attempts[0]["target"] == "UTC"
    assert token not in repr(request.session)
    assert token not in repr(client.kwargs)
    assert token not in response.body.decode("utf-8")
    assert "private-invitation-token" not in destination
    assert b"<script>alert(1)" not in response.body
    assert response.body.count(b"<script") == 1
    assert "account_id" not in request.session
    assert client.kwargs["state"].startswith("invite.")
    assert client.kwargs["nonce"]


def test_fresh_action_requests_max_age_and_reuses_browser_binding(monkeypatch):
    request, client = _request({"account_id": 7, "auth_version": 2, "csrf": "csrf"})
    request.app.state.config = SimpleNamespace(oidc_issuer="https://idp.example")
    request.app.state.control_pool = object()
    attempts = []

    @asynccontextmanager
    async def connection(_pool, *, lane="identity"):
        yield object()

    async def start(_conn, **kwargs):
        attempts.append(kwargs)
        return True

    monkeypatch.setattr(auth, "control_connection", connection)
    monkeypatch.setattr(auth, "start_oidc_attempt", start)
    account = {"id": 7, "auth_version": 2}
    first_response = asyncio.run(_oidc_protected_redirect(
        request, action="reauth", account=account,
        proof_action="change_email", target="new@example.com",
    ))
    destination = oidc_authorization_url(first_response)
    destination_params = parse_qs(urlsplit(destination).query)
    assert destination_params["max_age"] == ["0"]
    assert destination_params["prompt"] == ["login"]
    first_state = client.kwargs["state"]
    second_response = asyncio.run(_oidc_protected_redirect(
        request, action="reauth", account=account,
        proof_action="change_email", target="new@example.com",
    ))
    assert oidc_authorization_url(second_response)

    assert client.kwargs["max_age"] == 0
    assert client.kwargs["prompt"] == "login"
    assert client.kwargs["state"] != first_state
    assert attempts[0]["browser_nonce"] == attempts[1]["browser_nonce"]
    assert attempts[1]["account_id"] == 7
    assert attempts[1]["auth_version"] == 2
    assert attempts[1]["proof_action"] == "change_email"
    assert attempts[1]["target"] == "new@example.com"
