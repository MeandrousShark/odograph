from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.auth import AuthRedirect, require_user


class _Cursor:
    def __init__(self, row):
        self.row = row

    async def execute(self, *args, **kwargs):
        return self

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, account):
        self.account = account

    def cursor(self, row_factory=None):
        return _Cursor(self.account)

    async def execute(self, *args, **kwargs):
        return _Cursor((self.account is not None,))


class _ConnectionContext:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self.pool.connection_count += 1
        return _Connection(self.pool.account)

    async def __aexit__(self, *exc_info):
        return False


class _Pool:
    def __init__(self, account=None):
        self.account = account
        self.connection_count = 0

    def connection(self):
        return _ConnectionContext(self)


def _request(session, *, account=None, issuer="https://idp.example.com/"):
    pool = _Pool(account)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    dev_no_auth=False,
                    initial_admin_signup=False,
                    oidc_issuer=issuer,
                ),
                oauth=object(),
                pool=pool,
            )
        ),
        session=dict(session),
    )
    return request, pool


@pytest.mark.parametrize(
    "session",
    [
        {"account_id": 1},
        {"auth_version": 1},
        {"account_id": True, "auth_version": 1},
        {"account_id": 1, "auth_version": False},
        {"account_id": 0, "auth_version": 1},
        {"account_id": -1, "auth_version": 1},
        {"account_id": 1, "auth_version": 0},
        {"account_id": "1", "auth_version": 1},
        {"account_id": 1, "auth_version": "é"},
        {"account_id": [], "auth_version": 1},
        {"account_id": 1, "auth_version": {}},
    ],
)
def test_malformed_account_session_fails_closed_without_database_access(session):
    request, pool = _request(session)

    with pytest.raises(AuthRedirect):
        asyncio.run(require_user(request))

    assert request.session == {}
    assert pool.connection_count == 0


def test_exact_positive_integer_account_session_is_accepted():
    account = {
        "id": 1,
        "email": "admin@example.com",
        "password_hash": "hash",
        "is_admin": True,
        "is_enabled": True,
        "auth_version": 2,
    }
    request, pool = _request(
        {"account_id": 1, "auth_version": 2, "csrf": "csrf"}, account=account
    )

    user = asyncio.run(require_user(request))

    assert user["id"] == 1
    assert pool.connection_count == 1


@pytest.mark.parametrize(
    "legacy",
    [
        None,
        True,
        "legacy",
        [],
        {},
        {"issuer": "https://idp.example.com", "subject": ""},
        {"issuer": "https://idp.example.com", "subject": 123},
        {"issuer": 123, "subject": "subject-1"},
        {"issuer": "https://old-idp.example.com", "subject": "subject-1"},
    ],
)
def test_malformed_or_wrong_issuer_legacy_session_fails_closed(legacy):
    request, pool = _request({"legacy_oidc": legacy, "csrf": "csrf"})

    with pytest.raises(AuthRedirect):
        asyncio.run(require_user(request))

    assert request.session == {}
    assert pool.connection_count == 0


def test_configured_issuer_change_invalidates_existing_legacy_cookie():
    request, _ = _request(
        {
            "legacy_oidc": {
                "issuer": "https://idp.example.com",
                "subject": "subject-1",
            }
        },
        issuer="https://replacement.example.com",
    )

    with pytest.raises(AuthRedirect):
        asyncio.run(require_user(request))

    assert request.session == {}


def test_valid_legacy_session_binds_to_normalized_current_issuer():
    request, pool = _request(
        {
            "legacy_oidc": {
                "issuer": "https://idp.example.com",
                "subject": "subject-1",
                "name": "Legacy Admin",
            }
        }
    )

    user = asyncio.run(require_user(request))

    assert user["legacy_oidc"] is True
    assert pool.connection_count == 1


@pytest.mark.parametrize(
    "old_user",
    [
        None,
        True,
        "user",
        {},
        {"sub": ""},
        {"sub": 1},
        {"sub": False},
    ],
)
def test_malformed_pre_m9_user_session_fails_closed(old_user):
    request, pool = _request({"user": old_user, "csrf": "csrf"})

    with pytest.raises(AuthRedirect):
        asyncio.run(require_user(request))

    assert request.session == {}
    assert pool.connection_count == 0
