"""Report identity checks do not eagerly load variable account data."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth
from app.account_context import AccountPool, AccountPrincipal

pytestmark = pytest.mark.unit


def request_for(session, *, dev=False, tab=None):
    app = SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(dev_no_auth=dev), control_pool=object(),
        runtime_pool=object(), dev_principal=AccountPrincipal(7, True, 3),
    ))
    headers = [] if tab is None else [(b'x-odograph-account', tab.encode())]
    return Request({'type': 'http', 'app': app, 'session': session, 'headers': headers})


def profile(**changes):
    return dict(id=7, is_admin=False, is_enabled=True, auth_version=3,
                has_avatar=True, avatar_updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                **changes)


def install_profile(monkeypatch, account):
    calls = []

    @asynccontextmanager
    async def control(pool):
        yield object()

    async def lookup(conn, owner):
        calls.append(owner)
        return account

    async def forbidden(*args, **kwargs):
        raise AssertionError('variable account/settings data read before report admission')

    monkeypatch.setattr(auth, 'control_connection', control)
    monkeypatch.setattr(auth, '_get_report_account', lookup)
    monkeypatch.setattr(auth, 'get_account', forbidden)
    monkeypatch.setattr(auth, 'load_account_settings', forbidden)
    return calls


@pytest.mark.parametrize('dev', [False, True])
def test_report_auth_preserves_principal_without_variable_data(monkeypatch, dev):
    calls = install_profile(monkeypatch, profile())
    request = request_for({} if dev else {'account_id': 7, 'auth_version': 3}, dev=dev)
    user = asyncio.run(auth.require_report_user(request))
    assert calls == [7]
    assert user['id'] == 7 and user['has_avatar']
    assert 'email' not in user and 'name' not in user
    assert request.state.principal == AccountPrincipal(7, True, 3)
    assert request.state.account_pool.principal == request.state.principal
    assert not hasattr(request.state, 'account_settings')
    assert request.session['csrf']


@pytest.mark.parametrize('changes', [{'is_enabled': False}, {'auth_version': 4}])
def test_report_auth_rejects_disabled_or_stale_identity(monkeypatch, changes):
    account = profile()
    account.update(changes)
    install_profile(monkeypatch, account)
    request = request_for({'account_id': 7, 'auth_version': 3})
    with pytest.raises(auth.AuthRedirect):
        asyncio.run(auth.require_report_user(request))
    assert request.session == {}
    assert not hasattr(request.state, 'account_pool')


def test_report_auth_keeps_account_tab_fence(monkeypatch):
    install_profile(monkeypatch, profile())
    request = request_for({'account_id': 7, 'auth_version': 3}, tab='8')
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.require_report_user(request))
    assert error.value.status_code == 409
    assert not hasattr(request.state, 'account_pool')


@pytest.mark.parametrize('session', [
    {'account_id': True, 'auth_version': 3}, {'account_id': 7},
    {'account_id': 7, 'auth_version': '3'}, {'account_id': 0, 'auth_version': 3},
])
def test_report_auth_rejects_malformed_session_before_lookup(monkeypatch, session):
    calls = install_profile(monkeypatch, profile())
    request = request_for(session)
    with pytest.raises(auth.AuthRedirect):
        asyncio.run(auth.require_report_user(request))
    assert calls == [] and request.session == {}


@pytest.mark.parametrize('snapshot', ['bad', "0001-0022-1'; SELECT 1", 'a' * 65, 7])
def test_invalid_snapshot_cannot_borrow_a_connection(snapshot):
    class Pool:
        def connection(self, **kwargs):
            raise AssertionError('invalid snapshot reached the pool')

    async def run():
        async with AccountPool(Pool(), AccountPrincipal(7, True, 3)).connection(snapshot_id=snapshot):
            raise AssertionError('unreachable')

    with pytest.raises(ValueError):
        asyncio.run(run())
