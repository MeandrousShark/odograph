"""Administrator storage limit editor through the restricted-role HTTP route."""
from __future__ import annotations

import asyncio
import os

import pytest

from app import admin as admin_module
from app.accounts import create_admin
from app.db import make_pool
from conftest import provisioned_role_pools, reset_db
from tests.test_admin_routes_db import _app, _client

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
GIB = 1024 ** 3


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await reset_db(owner)
        async with owner.connection() as conn:
            # 2 GiB of grant budget with a 1 GiB default and no sub-limits.
            await conn.execute("SELECT public.storage_configure_limits(%s,NULL,NULL,%s,%s)",
                               (GIB, 3 * GIB, GIB))
        async with provisioned_role_pools(owner) as pools:
            async with pools.control.connection() as conn:
                admin = await create_admin(conn, "admin@example.invalid", "admin-hash")
            members = []
            async with owner.connection() as conn:
                for email in ("first@example.invalid", "second@example.invalid"):
                    member_id = (await (await conn.execute(
                        "INSERT INTO accounts(email,password_hash,is_admin) "
                        "VALUES (%s,'member-hash',false) RETURNING id", (email,))).fetchone())[0]
                    await conn.execute(
                        "INSERT INTO account_settings(account_id,display_tz) VALUES (%s,'UTC')",
                        (member_id,))
                    members.append(member_id)
            await callback(owner, pools, admin, members)
    finally:
        await owner.close()


async def _grant(owner, account_id):
    async with owner.connection() as conn:
        return await (await conn.execute(
            "SELECT limit_mode,account_limit_bytes FROM storage_grants WHERE account_id=%s",
            (account_id,))).fetchone()


def test_storage_limit_route_requires_admin_csrf_and_an_exact_form():
    async def check(owner, pools, admin, members):
        first, second = members
        app = _app(pools)
        async with await _client(app) as client, await _client(app) as member_client:
            await client.post(f"/test/session/{admin['id']}/1")
            page = await client.get("/admin/accounts")
            assert page.status_code == 200
            assert f'action="/admin/accounts/{first}/storage-limit"' in page.text
            assert f'action="/admin/accounts/{admin["id"]}/storage-limit"' not in page.text
            assert "No limit (administrator)" in page.text
            assert "Instance default: 1.00 GiB" in page.text
            assert 'max="0.00"' not in page.text

            await member_client.post(f"/test/session/{first}/1")
            denied = await member_client.post(
                f"/admin/accounts/{second}/storage-limit",
                data={"csrf_token": "route-csrf", "limit_mode": "unlimited"})
            assert denied.status_code == 403
            url = f"/admin/accounts/{first}/storage-limit"
            assert (await client.post(url, data={"csrf_token": "wrong", "limit_mode": "unlimited"})
                    ).status_code == 403
            assert (await client.post(url, data={"limit_mode": "unlimited"})).status_code == 400
            assert (await client.post(url, data={"csrf_token": "route-csrf"})).status_code == 400
            assert (await client.post(url, data={
                "csrf_token": "route-csrf", "limit_mode": "unlimited", "actor_id": str(first),
            })).status_code == 400
            assert (await client.post(
                "/admin/accounts/0/storage-limit",
                data={"csrf_token": "route-csrf", "limit_mode": "unlimited"})).status_code == 404
            missing = await client.post(
                "/admin/accounts/999/storage-limit",
                data={"csrf_token": "route-csrf", "limit_mode": "unlimited"})
            assert missing.status_code == 409
            assert "Storage limit could not be changed." in missing.text
            assert await _grant(owner, first) == ("default", GIB)
    asyncio.run(_scenario(check))


def test_storage_limit_route_validates_sizes_and_reports_outcomes():
    async def check(owner, pools, admin, members):
        first, second = members
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{admin['id']}/1")
            url = f"/admin/accounts/{first}/storage-limit"

            async def post(mode, size=None, target=url):
                data = {"csrf_token": "route-csrf", "limit_mode": mode}
                if size is not None:
                    data["account_gib"] = size
                return await client.post(target, data=data)

            for size in ("0", "0.001", "0.00", "-1", "abc", "1.234", "1e2", " 1", "NaN", "1_0"):
                refused = await post("custom", size)
                assert refused.status_code == 400, size
                assert "at least 0.01 GiB, with at most two decimal places" in refused.text
            # 1 GiB of the 2 GiB budget funds the other member.
            over = await post("custom", "1.01")
            assert over.status_code == 409
            assert "from 0.01 GiB to 1.00 GiB" in over.text
            for size in (None, ""):
                missing = await post("custom", size)
                assert missing.status_code == 400
                assert "Enter a size for a custom limit." in missing.text
            for mode in ("default", "unlimited"):
                extra = await post(mode, "1")
                assert extra.status_code == 400
                assert "Leave the size empty unless the limit is custom." in extra.text
            assert (await post("other")).status_code == 400
            for mode in ("custom", "default"):
                admin_refused = await post(mode, "1" if mode == "custom" else "",
                                           target=f"/admin/accounts/{admin['id']}/storage-limit")
                assert admin_refused.status_code == 409
                assert "Administrator accounts always have no storage limit." in admin_refused.text
            admin_noop = await post("unlimited", target=f"/admin/accounts/{admin['id']}/storage-limit")
            assert admin_noop.status_code == 200
            assert "already set that way" in admin_noop.text
            assert await _grant(owner, first) == ("default", GIB)

            removed = await post("unlimited", "", target=f"/admin/accounts/{second}/storage-limit")
            assert removed.status_code == 200
            assert "Storage limit removed. This account has no storage limit." in removed.text
            assert 'max="2.00"' in removed.text
            custom = await post("custom", "1.50")
            assert custom.status_code == 200
            assert "Storage limit set to 1.50 GiB. Existing data is kept." in custom.text
            assert "Custom: 1.50 GiB" in custom.text
            assert 'value="1.50"' in custom.text
            assert await _grant(owner, first) == ("custom", int(1.5 * GIB))
            same = await post("custom", "1.5")
            assert same.status_code == 200
            assert "already set that way" in same.text
            # Restoring the other default would need 2.5 GiB of the 2 GiB budget.
            unfunded = await post("default", target=f"/admin/accounts/{second}/storage-limit")
            assert unfunded.status_code == 409
            assert admin_module.STORAGE_LIMIT_UNFUNDED_NOTICE in unfunded.text
            assert await _grant(owner, second) == ("unlimited", None)
            restored = await post("default", "")
            assert restored.status_code == 200
            assert "Storage limit set to the instance default. Existing data is kept." in restored.text
            assert await _grant(owner, first) == ("default", GIB)
            exact = await post("custom", "2", target=f"/admin/accounts/{second}/storage-limit")
            assert exact.status_code == 409
            assert await _grant(owner, second) == ("unlimited", None)
            at_budget = await post("custom", "1", target=f"/admin/accounts/{second}/storage-limit")
            assert at_budget.status_code == 200
            assert await _grant(owner, second) == ("custom", GIB)
    asyncio.run(_scenario(check))


def test_lowered_limit_shows_over_allowance_and_keeps_data():
    async def check(owner, pools, admin, members):
        first, _second = members
        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO raw_messages(account_id,payload) "
                "VALUES (%s, jsonb_build_object('x', repeat('a', %s)))", (first, 11 * 1024 ** 2))
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{admin['id']}/1")
            lowered = await client.post(f"/admin/accounts/{first}/storage-limit", data={
                "csrf_token": "route-csrf", "limit_mode": "custom", "account_gib": "0.01"})
            assert lowered.status_code == 200
            assert "Storage limit set to 0.01 GiB." in lowered.text
            assert "Over allowance" in lowered.text
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM raw_messages WHERE account_id=%s", (first,))).fetchone() == (1,)
    asyncio.run(_scenario(check))


def test_default_without_a_configured_limit_reports_no_limit():
    async def check(owner, pools, admin, members):
        first, _second = members
        async with owner.connection() as conn:
            await conn.execute("SELECT public.storage_configure_limits(NULL,NULL,NULL,%s,%s)",
                               (3 * GIB, GIB))
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{admin['id']}/1")
            url = f"/admin/accounts/{first}/storage-limit"
            assert (await client.post(url, data={
                "csrf_token": "route-csrf", "limit_mode": "unlimited"})).status_code == 200
            restored = await client.post(url, data={"csrf_token": "route-csrf", "limit_mode": "default"})
            assert restored.status_code == 200
            assert ("Storage limit set to the instance default. The instance default has no limit."
                    in restored.text)
            assert "Instance default: no limit" in restored.text
            assert "Instance default (no limit)" in restored.text
        assert await _grant(owner, first) == ("default", None)
    asyncio.run(_scenario(check))


def test_storage_limit_route_reports_lock_timeout_and_deadlock_as_busy(monkeypatch):
    from psycopg import errors

    from app import storage_limits

    async def check(owner, pools, admin, members):
        first, _second = members
        app = _app(pools)
        form = {"csrf_token": "route-csrf", "limit_mode": "unlimited"}
        async with await _client(app) as client:
            await client.post(f"/test/session/{admin['id']}/1")
            # The setter waits for the policy row past its 5 s lock timeout.
            async with owner.connection() as holder:
                async with holder.transaction():
                    await holder.execute("SELECT 1 FROM storage_policy WHERE id=1 FOR UPDATE")
                    busy = await client.post(f"/admin/accounts/{first}/storage-limit", data=form)
            assert busy.status_code == 503
            assert "Storage limits are busy. Try again." in busy.text
            assert await _grant(owner, first) == ("default", GIB)

            class DeadlockingConnection:
                def __init__(self, conn):
                    self._conn = conn

                def __getattr__(self, name):
                    return getattr(self._conn, name)

                async def execute(self, query, *args, **kwargs):
                    if "storage_set_account_grant" in query:
                        raise errors.DeadlockDetected("deadlock detected")
                    return await self._conn.execute(query, *args, **kwargs)

            real = storage_limits.set_account_storage_limit

            async def deadlocking(conn, *args, **kwargs):
                return await real(DeadlockingConnection(conn), *args, **kwargs)

            monkeypatch.setattr(admin_module, "set_account_storage_limit", deadlocking)
            deadlocked = await client.post(f"/admin/accounts/{first}/storage-limit", data=form)
            assert deadlocked.status_code == 503
            assert "Storage limits are busy. Try again." in deadlocked.text
            assert await _grant(owner, first) == ("default", GIB)
    asyncio.run(_scenario(check))


def test_accounts_page_degrades_when_storage_limits_cannot_be_read(monkeypatch, caplog):
    from app.storage_limits import StorageLimitUnavailable

    async def check(owner, pools, admin, members):
        first, _second = members

        async def unavailable(conn, actor):
            raise StorageLimitUnavailable()

        monkeypatch.setattr(admin_module, "list_account_storage", unavailable)
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{admin['id']}/1")
            page = await client.get("/admin/accounts")
        assert page.status_code == 200
        assert page.text.count('<span class="muted">Unavailable</span>') == 3
        assert "/storage-limit" not in page.text
        assert f'action="/admin/accounts/{first}/disable"' in page.text
        assert 'action="/admin/invitations"' in page.text
        assert "account storage limits unavailable (StorageLimitUnavailable)" in caplog.text
    asyncio.run(_scenario(check))
