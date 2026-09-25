"""Route-level checks for protected OIDC method changes."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import Response

import app.auth as auth
from app.ingest import FailedAuthLimiter


def _endpoint(path: str, method: str):
    for route in auth.make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@asynccontextmanager
async def _connection(_pool):
    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

    yield Connection()


def _account(*, password_hash=None, auth_version=2):
    return {
        "id": 7, "email": "old@example.com", "password_hash": password_hash,
        "is_admin": False, "is_enabled": True, "auth_version": auth_version,
        "avatar_mime": None, "avatar_updated_at": None,
    }


def _request(*, session=None, oauth=None, userinfo=None):
    class OAuthClient:
        def __init__(self):
            self.calls = 0

        async def authorize_access_token(self, _request):
            self.calls += 1
            return {"userinfo": userinfo or {}}

    client = OAuthClient()
    cfg = SimpleNamespace(
        dev_no_auth=False, oidc_issuer="https://idp.example/", app_url="https://app.example",
        smtp_host="smtp.example", email_from="odograph@example.com",
        smtp_port=25, smtp_username="", smtp_password="", smtp_security="none",
        smtp_tls_insecure=False, display_tz="UTC",
    )
    def response(_request, _name, _context, status_code=200, **_kwargs):
        return Response(status_code=status_code)

    state = SimpleNamespace(
        config=cfg, oauth=oauth if oauth is not None else SimpleNamespace(pocketid=client),
        control_pool=object(), login_limiter=FailedAuthLimiter(100, 900),
        templates=SimpleNamespace(TemplateResponse=response),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=state), session=session if session is not None else {},
        state=SimpleNamespace(principal=SimpleNamespace(auth_version=2)),
        client=SimpleNamespace(host="203.0.113.8"), query_params={},
    )
    return request, client


def _protected_session(state="reauth.valid"):
    return {
        "account_id": 7, "auth_version": 2, "csrf": "csrf",
        auth.OIDC_PROTECTED_ATTEMPT_KEY: {
            "action": "reauth", "state": state, "nonce": "nonce",
            "browser_nonce": "browser", "account_id": 7, "auth_version": 2,
        },
    }


def test_session_refresh_rebinds_request_context_after_method_change():
    request, _ = _request(session={"account_id": 7, "auth_version": 2, "csrf": "old"})
    runtime_pool = object()
    request.app.state.runtime_pool = runtime_pool
    request.app.state.make_detector_runner = lambda pool: ("runner", pool)
    request.state.principal = SimpleNamespace(account_id=7, auth_version=2)

    auth._set_account_session(request, _account(password_hash="hash", auth_version=3))

    assert request.session["account_id"] == 7
    assert request.session["auth_version"] == 3
    assert request.state.principal.account_id == 7
    assert request.state.principal.auth_version == 3
    assert request.state.account_pool.principal.auth_version == 3
    assert request.state.account_pool.runtime_pool is runtime_pool
    assert request.state.detector_runner[1] is request.state.account_pool


@pytest.mark.parametrize("callback_state", ["invite.valid", "reauth.other"])
def test_callback_rejects_swapped_or_mismatched_state_before_token_exchange(
    monkeypatch, callback_state,
):
    request, client = _request(session=_protected_session())
    request.query_params = {"state": callback_state}
    monkeypatch.setattr(auth, "control_connection", _connection)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert rejected.value.status_code == 401
    assert client.calls == 0


@pytest.mark.parametrize("auth_time", [None, 1, "future"])
def test_reauth_callback_rejects_absent_stale_or_future_auth_time(monkeypatch, auth_time):
    if auth_time == "future":
        auth_time = time.time() + 300
    request, client = _request(
        session=_protected_session(),
        userinfo={"sub": "exact-subject", "auth_time": auth_time},
    )
    request.query_params = {"state": "reauth.valid"}
    consumed = []

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def consume(_conn, **kwargs):
        consumed.append(kwargs)
        return None

    async def finish(_conn, **kwargs):
        return time.time() - 60 <= kwargs["auth_time"] <= time.time() + 60

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert rejected.value.status_code == 401
    assert client.calls == 1
    assert "oidc_action_proof_nonce" not in request.session
    if auth_time is None:
        assert len(consumed) == 1


def test_reauth_callback_rejects_wrong_nonce(monkeypatch):
    request, client = _request(
        session=_protected_session(),
        userinfo={"sub": "exact-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid"}

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def finish(_conn, **kwargs):
        assert kwargs["nonce"] == "nonce"
        return False

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert rejected.value.status_code == 401
    assert client.calls == 1
    assert "oidc_action_proof_nonce" not in request.session


def test_oidc_only_password_post_without_proof_never_hashes(monkeypatch):
    request, _ = _request(session={"account_id": 7, "auth_version": 2, "csrf": "csrf"})
    monkeypatch.setattr(auth, "control_connection", _connection)

    async def get_account(_conn, _account_id):
        return _account()

    async def identity(_conn, _account_id, _issuer):
        return None

    async def verified(_conn, _account_id):
        return False

    def hash_password(_password):
        raise AssertionError("proofless request started scrypt")

    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "get_identity_for_account", identity)
    monkeypatch.setattr(auth, "is_current_email_verified", verified)
    monkeypatch.setattr(auth, "hash_password", hash_password)
    endpoint = _endpoint("/settings/account/password", "POST")
    user = {"id": 7, "email": "old@example.com"}

    for _ in range(3):
        response = asyncio.run(endpoint(
            request, current_password="", password="new password",
            password_confirm="new password", csrf_token="csrf", user=user,
        ))
        assert response.status_code == 401


def test_oidc_only_password_addition_consumes_proof_before_hash(monkeypatch):
    request, _ = _request(session={
        "account_id": 7, "auth_version": 2, "csrf": "csrf",
        "oidc_action_proof_nonce": "browser",
    })
    monkeypatch.setattr(auth, "control_connection", _connection)
    order = []

    async def get_account(_conn, _account_id):
        return _account()

    async def consume(_conn, **kwargs):
        order.append(("proof", kwargs["action"], kwargs["target"], kwargs["browser_nonce"]))
        return True

    def hash_password(_password):
        order.append(("hash",))
        return "hashed"

    async def replace(_conn, _account_id, _hash, *, expected_auth_version):
        order.append(("replace", expected_auth_version))
        return _account(password_hash="hashed", auth_version=3)

    async def identity(_conn, _account_id, _issuer):
        return None

    async def verified(_conn, _account_id):
        return False

    async def to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "consume_action_proof", consume)
    monkeypatch.setattr(auth, "hash_password", hash_password)
    monkeypatch.setattr(auth, "replace_password", replace)
    monkeypatch.setattr(auth, "get_identity_for_account", identity)
    monkeypatch.setattr(auth, "is_current_email_verified", verified)
    monkeypatch.setattr(auth.asyncio, "to_thread", to_thread)

    response = asyncio.run(_endpoint("/settings/account/password", "POST")(
        request, current_password="", password="new password",
        password_confirm="new password", csrf_token="csrf",
        user={"id": 7, "email": "old@example.com"},
    ))

    assert response.status_code == 200
    assert order == [("proof", "add_password", "", "browser"), ("hash",), ("replace", 2)]
    assert request.session["auth_version"] == 3
    assert "oidc_action_proof_nonce" not in request.session


def test_oidc_only_email_request_requires_target_bound_proof(monkeypatch):
    request, _ = _request(session={
        "account_id": 7, "auth_version": 2, "csrf": "csrf",
        "oidc_action_proof_nonce": "browser",
    })
    monkeypatch.setattr(auth, "control_connection", _connection)
    calls = []

    async def get_account(_conn, _account_id):
        return _account()

    async def form(_request, fields):
        assert fields == {"new_email", "new_email_confirm", "csrf_token"}
        return {
            "new_email": "New@Example.com", "new_email_confirm": "new@example.com",
            "csrf_token": "csrf",
        }

    async def consume(_conn, **kwargs):
        calls.append(("proof", kwargs["action"], kwargs["target"]))
        return False

    async def issue(*_args):
        calls.append(("issue",))
        return "token"

    async def identity(_conn, _account_id, _issuer):
        return None

    async def verified(_conn, _account_id):
        return False

    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "_email_form", form)
    monkeypatch.setattr(auth, "consume_action_proof", consume)
    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "get_identity_for_account", identity)
    monkeypatch.setattr(auth, "is_current_email_verified", verified)

    response = asyncio.run(_endpoint("/settings/account/email/change/request", "POST")(
        request, user={"id": 7, "email": "old@example.com"},
    ))
    assert response.status_code == 401
    assert calls == [("proof", "change_email", "new@example.com")]
