from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from starlette.responses import RedirectResponse

from app.auth import (
    OIDC_LINK_ATTEMPT_KEY,
    OIDC_LINK_ATTEMPT_TTL_S,
    _consume_link_attempt,
    _oidc_authorize_redirect,
)


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


def test_link_authorization_uses_state_and_nonce_bound_to_current_account_session():
    request, client = _request({"account_id": 1, "auth_version": 4, "csrf": "csrf"})
    account = {"id": 1, "auth_version": 4}

    response = asyncio.run(
        _oidc_authorize_redirect(request, flow="link", account=account)
    )

    assert response.status_code == 307
    assert client.kwargs["state"].startswith("link.")
    assert client.kwargs["nonce"]
    assert request.session[OIDC_LINK_ATTEMPT_KEY]["state"] == client.kwargs["state"]
    assert request.session[OIDC_LINK_ATTEMPT_KEY]["account_id"] == 1
    assert request.session[OIDC_LINK_ATTEMPT_KEY]["auth_version"] == 4
    assert "password" not in repr(request.session).lower()


def test_login_authorization_uses_separate_state_and_nonce_and_clears_old_session():
    request, client = _request({"evil": "planted", "csrf": "old"})

    asyncio.run(_oidc_authorize_redirect(request, flow="login"))

    assert client.kwargs["state"].startswith("login.")
    assert client.kwargs["nonce"]
    assert request.session.get("evil") is None
    assert OIDC_LINK_ATTEMPT_KEY not in request.session


def test_link_attempt_is_single_use_and_replay_fails():
    state = "link.current"
    request, _ = _request(
        {
            OIDC_LINK_ATTEMPT_KEY: {
                "state": state,
                "account_id": 1,
                "auth_version": 2,
                "issued_at": time.time(),
            }
        }
    )

    assert _consume_link_attempt(request, state) == {
        "account_id": 1,
        "auth_version": 2,
    }
    assert _consume_link_attempt(request, state) is None


def test_stale_and_cross_session_link_attempts_fail_closed():
    state = "link.current"
    stale, _ = _request(
        {
            OIDC_LINK_ATTEMPT_KEY: {
                "state": state,
                "account_id": 1,
                "auth_version": 2,
                "issued_at": time.time() - OIDC_LINK_ATTEMPT_TTL_S - 1,
            }
        }
    )
    other_session, _ = _request()

    assert _consume_link_attempt(stale, state) is None
    assert OIDC_LINK_ATTEMPT_KEY not in stale.session
    assert _consume_link_attempt(other_session, state) is None


def test_mismatched_link_state_does_not_consume_the_valid_attempt():
    request, _ = _request(
        {
            OIDC_LINK_ATTEMPT_KEY: {
                "state": "link.expected",
                "account_id": 1,
                "auth_version": 2,
                "issued_at": time.time(),
            }
        }
    )

    assert _consume_link_attempt(request, "link.other") is None
    assert request.session[OIDC_LINK_ATTEMPT_KEY]["state"] == "link.expected"
