"""Account routes reuse the account row they just loaded to verify the
current password, hash a new password outside the write borrow, and render
settings in one borrow. A concurrent disable, password change or
sign-out-everywhere must still win at the write."""
from __future__ import annotations

import asyncio
import functools
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.auth as auth
from app.accounts import create_admin, get_account
from app.db import make_pool
from app.ingest import FailedAuthLimiter
from app.local_auth import hash_password, verify_password
from app.main import make_templates
from conftest import provisioned_role_pools, reset_db
from tests.auth_db_fixtures import auth_config

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

OLD_PASSWORD = "old local password"
NEW_PASSWORD = "new local password"
EMAIL = "fresh@example.invalid"


@functools.cache
def _routes():
    return tuple(auth.make_router().routes)


def _endpoint(path: str, method: str):
    for route in _routes():
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"missing route {method} {path}")


def _request(pools, account):
    config = auth_config(
        TEST_DB, dev_no_auth=False, initial_admin_signup=False,
        oidc_client_id="test-client", oidc_client_secret="test-secret",
        oidc_issuer="https://idp.example/",
        smtp_host="smtp.example.invalid", email_from="odograph@example.invalid",
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            config=config, control_pool=pools.control, runtime_pool=pools.runtime,
            security_link_base="https://odograph.example.invalid", oauth=SimpleNamespace(pocketid=None),
            templates=make_templates(config),
            login_limiter=FailedAuthLimiter(20, 900),
            make_detector_runner=lambda pool: SimpleNamespace(pool=pool),
        )),
        state=SimpleNamespace(),
        session={"csrf": "csrf", "account_id": account["id"], "auth_version": account["auth_version"]},
        query_params={}, headers={}, client=SimpleNamespace(host="203.0.113.10"),
    )


def _count_control_borrows(monkeypatch) -> list[str]:
    borrows = []
    original = auth.control_connection

    @asynccontextmanager
    async def counted(pool, **kwargs):
        borrows.append(kwargs.get("lane", "identity"))
        async with original(pool, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(auth, "control_connection", counted)
    return borrows


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await reset_db(owner)
        async with provisioned_role_pools(owner) as pools:
            async with pools.control.connection() as conn:
                account = await create_admin(conn, EMAIL, hash_password(OLD_PASSWORD))
            await callback(owner, pools, account)
    finally:
        await owner.close()


async def _change_password(request, user):
    return await _endpoint("/settings/account/password", "POST")(
        request, current_password=OLD_PASSWORD, password=NEW_PASSWORD,
        password_confirm=NEW_PASSWORD, csrf_token="csrf", user=user,
    )


async def _stored(pools, account_id):
    async with pools.control.connection() as conn:
        return await get_account(conn, account_id)


def test_change_password_reuses_the_loaded_account_and_hashes_outside_the_write(monkeypatch):
    async def check(owner, pools, account):
        request = _request(pools, account)
        user = await auth.require_user(request)
        borrows = _count_control_borrows(monkeypatch)
        original_thread = auth.owned_thread
        borrowed_during_hash = []

        async def observed(function, *args):
            if function is auth.hash_password:
                borrowed_during_hash.append(len(borrows))
            return await original_thread(function, *args)

        monkeypatch.setattr(auth, "owned_thread", observed)
        response = await _change_password(request, user)
        assert response.status_code == 200
        # Route read, password write, then one borrow for the whole settings
        # render. Verifying the current password reuses the route's row.
        assert borrows == ["identity", "lifecycle", "identity"]
        # The hash ran after the route read and before the write borrow.
        assert borrowed_during_hash == [1]
        stored = await _stored(pools, account["id"])
        assert verify_password(NEW_PASSWORD, stored["password_hash"])
        assert stored["auth_version"] == account["auth_version"] + 1
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("change", [
    "UPDATE accounts SET is_enabled=false WHERE id=%s",
    "UPDATE accounts SET auth_version=auth_version+1 WHERE id=%s",
    "SELECT public.sign_out_account_everywhere(%s, 1)",
])
def test_change_password_write_refuses_a_change_made_during_the_hash(monkeypatch, change):
    async def check(owner, pools, account):
        request = _request(pools, account)
        user = await auth.require_user(request)
        original_thread = auth.owned_thread

        async def change_during_hash(function, *args):
            result = await original_thread(function, *args)
            if function is auth.hash_password:
                async with owner.connection() as conn:
                    await conn.execute(change, (account["id"],))
            return result

        monkeypatch.setattr(auth, "owned_thread", change_during_hash)
        with pytest.raises(auth.AuthRedirect):
            await _change_password(request, user)
        assert "account_id" not in request.session
        stored = await _stored(pools, account["id"])
        assert verify_password(OLD_PASSWORD, stored["password_hash"])
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("change", [
    "UPDATE accounts SET is_enabled=false WHERE id=%s",
    "UPDATE accounts SET auth_version=auth_version+1 WHERE id=%s",
])
def test_change_password_refuses_a_change_made_after_authentication(monkeypatch, change):
    async def check(owner, pools, account):
        request = _request(pools, account)
        user = await auth.require_user(request)
        # The session was authenticated; the account changes before the
        # route loads it.
        async with owner.connection() as conn:
            await conn.execute(change, (account["id"],))
        verified = []
        original_thread = auth.owned_thread

        async def observed(function, *args):
            verified.append(function)
            return await original_thread(function, *args)

        monkeypatch.setattr(auth, "owned_thread", observed)
        with pytest.raises(auth.AuthRedirect):
            await _change_password(request, user)
        assert verified == []
        assert "account_id" not in request.session
        assert not request.app.state.login_limiter.blocked("203.0.113.10")
        stored = await _stored(pools, account["id"])
        assert verify_password(OLD_PASSWORD, stored["password_hash"])
    asyncio.run(_scenario(check))


def test_wrong_current_password_counts_with_the_reused_account(monkeypatch):
    async def check(owner, pools, account):
        request = _request(pools, account)
        request.app.state.login_limiter = FailedAuthLimiter(1, 900)
        user = await auth.require_user(request)
        borrows = _count_control_borrows(monkeypatch)
        response = await _endpoint("/settings/account/password", "POST")(
            request, current_password="not the password", password=NEW_PASSWORD,
            password_confirm=NEW_PASSWORD, csrf_token="csrf", user=user,
        )
        assert response.status_code == 401
        # Route read, then the render's linked identity and verified-email
        # reads together.
        assert borrows == ["identity", "identity"]
        assert request.app.state.login_limiter.blocked("203.0.113.10")
        stored = await _stored(pools, account["id"])
        assert verify_password(OLD_PASSWORD, stored["password_hash"])
    asyncio.run(_scenario(check))


CHANGES = (
    "UPDATE accounts SET is_enabled=false WHERE id=%s",
    "UPDATE accounts SET auth_version=auth_version+1 WHERE id=%s",
    "SELECT public.sign_out_account_everywhere(%s, 1)",
)


def _change_during_verification(monkeypatch, owner, account, change):
    """Apply `change` after the route loaded the account, while the reused
    row's current password is being verified, before any write."""
    original_thread = auth.owned_thread

    async def change_while_verifying(function, *args):
        result = await original_thread(function, *args)
        if function is auth.verify_password:
            async with owner.connection() as conn:
                await conn.execute(change, (account["id"],))
        return result

    monkeypatch.setattr(auth, "owned_thread", change_while_verifying)


@pytest.mark.parametrize("change", CHANGES)
def test_change_password_refuses_a_change_after_the_loaded_row(monkeypatch, change):
    async def check(owner, pools, account):
        request = _request(pools, account)
        user = await auth.require_user(request)
        _change_during_verification(monkeypatch, owner, account, change)
        with pytest.raises(auth.AuthRedirect):
            await _change_password(request, user)
        assert "account_id" not in request.session
        stored = await _stored(pools, account["id"])
        assert verify_password(OLD_PASSWORD, stored["password_hash"])
    asyncio.run(_scenario(check))


@pytest.mark.parametrize("change", CHANGES)
def test_email_request_refuses_a_change_after_the_loaded_row(monkeypatch, change):
    async def check(owner, pools, account):
        request = _request(pools, account)
        user = await auth.require_user(request)
        borrows = _count_control_borrows(monkeypatch)

        async def form(_request, fields):
            assert fields == {"current_password", "csrf_token"}
            return {"current_password": OLD_PASSWORD, "csrf_token": "csrf"}

        monkeypatch.setattr(auth, "_email_form", form)
        _change_during_verification(monkeypatch, owner, account, change)
        await _endpoint("/settings/account/email/verify/request", "POST")(request, user=user)
        # Route read, post-body read, refused issue, then the render. The
        # current password is verified against the post-body row.
        assert borrows == ["identity", "identity", "identity", "identity"]
        async with owner.connection() as conn:
            issued = await (await conn.execute(
                "SELECT count(*) FROM email_challenges WHERE account_id=%s", (account["id"],),
            )).fetchone()
        assert issued == (0,)
    asyncio.run(_scenario(check))

