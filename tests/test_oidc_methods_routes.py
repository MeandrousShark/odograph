"""Route-level checks for protected OIDC method changes."""
from __future__ import annotations

import asyncio
import time
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import Response

import app.auth as auth
from app.ingest import FailedAuthLimiter
from app.password_reset import SecurityMailAdmission
from security_mail_support import PreparedFakeReceiver, configure_fake_mailer


def _endpoint(path: str, method: str):
    for route in auth.make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


@asynccontextmanager
async def _connection(_pool, *, lane="identity"):
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
        security_mail=None,
        security_link_base=auth.security_link_base(cfg.app_url),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=state), session=session if session is not None else {},
        state=SimpleNamespace(principal=SimpleNamespace(auth_version=2)),
        client=SimpleNamespace(host="203.0.113.8"), query_params={},
    )
    return request, client


def _protected_session(state="reauth.valid", *, proof_action=None, target=""):
    return {
        "account_id": 7, "auth_version": 2, "csrf": "csrf",
        auth.OIDC_PROTECTED_ATTEMPT_KEY: {
            "action": "reauth", "state": state, "nonce": "nonce",
            "browser_nonce": "browser", "proof_action": proof_action,
            "target": target, "account_id": 7, "auth_version": 2,
        },
    }


class _SecurityMail(SecurityMailAdmission):
    pass


@pytest.mark.parametrize(
    "purpose,target",
    [
        (auth.PURPOSE_CURRENT, "old@example.com"),
        (auth.PURPOSE_CHANGE, "new@example.com"),
    ],
)
def test_email_reauth_callback_consumes_bound_proof_sends_and_redirects(
    monkeypatch, purpose, target,
):
    request, client = _request(
        session=_protected_session(proof_action=purpose, target=target),
        userinfo={"sub": "exact-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid", "code": "provider-code"}
    request.app.state.security_mail = _SecurityMail()
    proof_calls, issue_calls, sent = [], [], []

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def get_account(_conn, account_id):
        assert account_id == 7
        return _account()

    async def finish(_conn, **kwargs):
        assert kwargs["subject"] == "exact-subject"
        return True

    async def consume(_conn, **kwargs):
        proof_calls.append(kwargs)
        return True

    async def issue(_conn, account_id, auth_version, action, exact_target):
        issue_calls.append((account_id, auth_version, action, exact_target))
        return "secret-token"

    async def send_usable(_conn, account_id, auth_version, action, token):
        assert (account_id, auth_version, action, token) == (7, 2, purpose, "secret-token")
        return True

    @asynccontextmanager
    async def lease(*_args):
        yield

    class Mailer(PreparedFakeReceiver):
        def __init__(self, *args):
            configure_fake_mailer(self, args)
            self.target = args[-1]

        def compose(self, subject, body):
            return body

        async def send(self, message):
            sent.append((self.target, message))

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)
    monkeypatch.setattr(auth, "consume_action_proof", consume)
    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "email_challenge_send_usable", send_usable)
    monkeypatch.setattr(auth, "external_account_work", lease)
    monkeypatch.setattr(auth, "Mailer", Mailer)

    response = asyncio.run(_endpoint("/auth/callback", "GET")(request))

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/account"
    assert "provider-code" not in response.headers["location"]
    assert request.session["account_notice"] == auth.EMAIL_REQUEST_NOTICE
    assert "oidc_action_proof_nonce" not in request.session
    assert proof_calls == [{
        "account_id": 7, "auth_version": 2, "action": purpose,
        "target": target, "browser_nonce": "browser",
    }]
    assert issue_calls == [(7, 2, purpose, target)]
    assert len(sent) == 1 and sent[0][0] == target
    assert "#purpose=" + purpose + "&token=secret-token" in sent[0][1]
    assert client.calls == 1
    with pytest.raises(HTTPException) as replay:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert replay.value.status_code == 401
    assert len(sent) == 1


@pytest.mark.parametrize(
    "account_changes",
    [
        {"is_enabled": False},
        {"auth_version": 3},
        {"password_hash": "added-locally"},
    ],
    ids=["disabled", "stale-version", "password-added"],
)
def test_email_reauth_callback_rechecks_enabled_version_and_password_state(
    monkeypatch, account_changes,
):
    request, _ = _request(
        session=_protected_session(
            proof_action=auth.PURPOSE_CHANGE, target="new@example.com",
        ),
        userinfo={"sub": "exact-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid"}
    events = []

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def get_account(_conn, _account_id):
        return {**_account(), **account_changes}

    async def finish(_conn, **_kwargs):
        return True

    async def consume(_conn, **_kwargs):
        events.append("consume")
        return True

    async def unexpected_issue(*_args):
        events.append("issue")
        raise AssertionError("issued after account state changed")

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)
    monkeypatch.setattr(auth, "consume_action_proof", consume)
    monkeypatch.setattr(auth, "issue_email_challenge", unexpected_issue)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert rejected.value.status_code == 401
    assert events == ["consume"]


def test_email_reauth_identity_failure_never_consumes_or_issues(monkeypatch):
    request, _ = _request(
        session=_protected_session(
            proof_action=auth.PURPOSE_CHANGE, target="new@example.com",
        ),
        userinfo={"sub": "wrong-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid"}
    events = []

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def finish(_conn, **_kwargs):
        return False

    async def unexpected(*_args, **_kwargs):
        events.append("called")
        raise AssertionError("email flow started after identity failure")

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)
    monkeypatch.setattr(auth, "consume_action_proof", unexpected)
    monkeypatch.setattr(auth, "issue_email_challenge", unexpected)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert rejected.value.status_code == 401
    assert events == []


def test_email_reauth_callback_rejects_another_signed_in_account(monkeypatch):
    request, client = _request(
        session=_protected_session(
            proof_action=auth.PURPOSE_CHANGE, target="new@example.com",
        ),
        userinfo={"sub": "exact-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid"}
    attempts = []

    async def require_other_account(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 8}

    async def consume(_conn, **kwargs):
        attempts.append(kwargs)
        return None

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("email action started for the wrong account")

    monkeypatch.setattr(auth, "require_user", require_other_account)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)
    monkeypatch.setattr(auth, "consume_action_proof", unexpected)
    monkeypatch.setattr(auth, "issue_email_challenge", unexpected)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(_endpoint("/auth/callback", "GET")(request))
    assert rejected.value.status_code == 401
    assert client.calls == 0
    assert len(attempts) == 1
    assert attempts[0]["account_id"] == 7


def test_oidc_email_reauth_is_not_started_without_delivery_configuration(monkeypatch):
    request, client = _request(session={"account_id": 7, "auth_version": 2, "csrf": "csrf"})
    request.app.state.config.smtp_host = ""

    async def get_account(_conn, _account_id):
        return _account()

    async def identity(_conn, _account_id, _issuer):
        return {
            "issuer": "https://idp.example", "subject": "exact",
            "provider_email": None, "provider_display_name": None,
        }

    async def unverified(_conn, _account_id):
        return False

    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "get_identity_for_account", identity)
    monkeypatch.setattr(auth, "is_current_email_verified", unverified)

    response = asyncio.run(_endpoint("/settings/account/oidc/reauth", "POST")(
        request, action=auth.PURPOSE_CHANGE, target="new@example.com",
        target_confirm="new@example.com", csrf_token="csrf", user={"id": 7},
    ))
    assert response.status_code == 503
    assert client.calls == 0
    assert auth.OIDC_PROTECTED_ATTEMPT_KEY not in request.session


def test_cancelled_email_callback_keeps_owning_issuance_and_delivery(monkeypatch):
    request, _ = _request(
        session=_protected_session(
            proof_action=auth.PURPOSE_CHANGE, target="new@example.com",
        ),
        userinfo={"sub": "exact-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid"}
    request.app.state.security_mail = _SecurityMail()
    issue_started, release_issue, delivered = (
        asyncio.Event(), asyncio.Event(), asyncio.Event()
    )

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def get_account(_conn, _account_id):
        return _account()

    async def finish(_conn, **_kwargs):
        return True

    async def consume(_conn, **_kwargs):
        return True

    async def issue(*_args):
        issue_started.set()
        await release_issue.wait()
        return "secret-token"

    async def send_usable(*_args):
        return True

    @asynccontextmanager
    async def lease(*_args):
        yield

    class Mailer(PreparedFakeReceiver):
        def __init__(self, *_args):
            configure_fake_mailer(self, _args)

        def compose(self, _subject, body):
            return body

        async def send(self, message):
            assert "secret-token" in message
            delivered.set()

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)
    monkeypatch.setattr(auth, "consume_action_proof", consume)
    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "email_challenge_send_usable", send_usable)
    monkeypatch.setattr(auth, "external_account_work", lease)
    monkeypatch.setattr(auth, "Mailer", Mailer)

    async def run():
        caller = asyncio.create_task(_endpoint("/auth/callback", "GET")(request))
        await asyncio.wait_for(issue_started.wait(), 2)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        release_issue.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        await asyncio.wait_for(delivered.wait(), 2)

    asyncio.run(run())


@pytest.mark.parametrize("proof_succeeds", [False, True])
def test_email_reauth_callback_never_issues_after_failed_proof_or_missing_smtp(
    monkeypatch, proof_succeeds,
):
    request, _ = _request(
        session=_protected_session(
            proof_action=auth.PURPOSE_CHANGE, target="new@example.com",
        ),
        userinfo={"sub": "exact-subject", "auth_time": time.time()},
    )
    request.query_params = {"state": "reauth.valid"}
    request.app.state.security_mail = _SecurityMail()
    if proof_succeeds:
        request.app.state.config.smtp_host = ""
    events = []

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def get_account(_conn, _account_id):
        return _account()

    async def finish(_conn, **_kwargs):
        return True

    async def consume(_conn, **_kwargs):
        events.append("consume")
        return True

    async def issue(*_args):
        events.append("issue")
        return "secret-token"

    async def send_usable(*_args):
        return True

    @asynccontextmanager
    async def lease(*_args):
        yield

    class Mailer(PreparedFakeReceiver):
        def __init__(self, *_args):
            configure_fake_mailer(self, _args)

        def compose(self, *_args):
            return "message"

        async def send(self, _message):
            events.append("send")

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "get_account", get_account)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)
    monkeypatch.setattr(auth, "consume_action_proof", consume)
    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "email_challenge_send_usable", send_usable)
    monkeypatch.setattr(auth, "external_account_work", lease)
    monkeypatch.setattr(auth, "Mailer", Mailer)

    if not proof_succeeds:
        async def rejected_proof(_conn, **_kwargs):
            events.append("consume")
            return False

        monkeypatch.setattr(auth, "consume_action_proof", rejected_proof)
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(_endpoint("/auth/callback", "GET")(request))
        assert rejected.value.status_code == 401
        assert events == ["consume"]
    else:
        response = asyncio.run(_endpoint("/auth/callback", "GET")(request))
        assert response.status_code == 303
        assert request.session["account_error"] == auth.GENERIC_EMAIL_ERROR
        assert events == ["consume"]


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


@pytest.mark.parametrize("path", [
    "/settings/account/oidc/link", "/settings/account/oidc/unlink",
])
def test_method_routes_hold_bounded_verification_after_cancellation(monkeypatch, path):
    request, _ = _request(session={"csrf": "csrf"})
    request.app.state.login_limiter = FailedAuthLimiter(10, 900, max_concurrent_auth=1)
    started, release = threading.Event(), threading.Event()

    def verify(_submitted, _hash):
        started.set()
        release.wait(5)
        return False

    async def account(_conn, _id):
        return _account(password_hash="stored")

    async def identity(*_args):
        return None

    async def verified(*_args):
        return False

    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "get_account", account)
    monkeypatch.setattr(auth, "verify_password", verify)
    monkeypatch.setattr(auth, "get_identity_for_account", identity)
    monkeypatch.setattr(auth, "is_current_email_verified", verified)
    endpoint = _endpoint(path, "POST")

    async def call():
        args = dict(current_password="wrong", csrf_token="csrf", user={"id": 7})
        if path.endswith("/unlink"):
            args["confirm_unlink"] = "yes"
        return await endpoint(request, **args)

    async def run():
        first = asyncio.create_task(call())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done()
            assert len(request.app.state.login_limiter._auth_tasks) == 1
            saturated = await call()
            assert saturated.status_code == 503
            assert not request.app.state.login_limiter.blocked(request.client.host)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.gather(*request.app.state.login_limiter._auth_tasks, return_exceptions=True)
        assert request.app.state.login_limiter.blocked(request.client.host) is False
        assert sum(len(q) for q in request.app.state.login_limiter._failures.values()) == 1
        assert (await call()).status_code == 401

    asyncio.run(run())


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


def test_cancelled_reauth_token_exchange_consumes_exact_attempt(monkeypatch):
    request, client = _request(session=_protected_session())
    request.query_params = {"state": "reauth.valid"}
    entered, release, consumed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def exchange(_request):
        entered.set()
        await asyncio.Event().wait()

    async def consume(_conn, **kwargs):
        assert kwargs == {
            "action": "reauth", "state": "reauth.valid", "nonce": "nonce",
            "browser_nonce": "browser", "account_id": 7, "auth_version": 2,
        }
        await release.wait()
        consumed.set()

    client.authorize_access_token = exchange
    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)

    async def run():
        caller = asyncio.create_task(_endpoint("/auth/callback", "GET")(request))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        assert "oidc_action_proof_nonce" not in request.session
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        assert consumed.is_set()

    asyncio.run(run())


def test_successful_reauth_finishing_is_not_aborted_with_cancelled_caller(monkeypatch):
    request, _ = _request(session=_protected_session(), userinfo={
        "sub": "exact", "auth_time": time.time(),
    })
    request.query_params = {"state": "reauth.valid"}
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def require_user(req):
        req.state.principal = SimpleNamespace(auth_version=2)
        return {"id": 7}

    async def finish(_conn, **kwargs):
        assert kwargs["subject"] == "exact"
        entered.set()
        await release.wait()
        finished.set()
        return True

    async def no_consume(*args, **kwargs):
        raise AssertionError("successful proof was consumed")

    monkeypatch.setattr(auth, "require_user", require_user)
    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "finish_oidc_reauth", finish)
    monkeypatch.setattr(auth, "consume_oidc_attempt", no_consume)

    async def run():
        caller = asyncio.create_task(_endpoint("/auth/callback", "GET")(request))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        assert finished.is_set()

    asyncio.run(run())


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
    monkeypatch.setattr(auth, "owned_thread", to_thread)

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
