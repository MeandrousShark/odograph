"""Administrator account transitions through restricted-role HTTP routes."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.db import make_pool
from conftest import full_schema_reset
from tests.test_admin_routes_db import _app, _client

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        async with owner.connection() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
        await full_schema_reset(owner)
        await prepare_application_roles(TEST_DB)
        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                admin = await create_admin(conn, "admin@example.invalid", "admin-hash")
            await callback(owner, pools, admin)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


def test_lifecycle_routes_require_live_admin_csrf_and_activated_accounts():
    async def check(owner, pools, admin):
        app = _app(pools)
        async with await _client(app) as first, await _client(app) as member_client:
            await first.post(f"/test/session/{admin['id']}/1")
            unavailable = await first.post(
                "/admin/accounts/2/disable", data={"csrf_token": "route-csrf"},
            )
            assert unavailable.status_code == 409
            assert "multi-account mode" in unavailable.text

            async with owner.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                member_id = (await (await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin) "
                    "VALUES ('member@example.invalid','member-hash',false) RETURNING id"
                )).fetchone())[0]
                await conn.execute(
                    "INSERT INTO account_settings(account_id,display_tz) VALUES (%s,'UTC')",
                    (member_id,),
                )

            await member_client.post(f"/test/session/{member_id}/1")
            page = await first.get("/admin/accounts")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store, private"
            assert page.headers["referrer-policy"] == "no-referrer"
            assert f'/admin/accounts/{member_id}/disable' in page.text
            assert f'/admin/accounts/{admin["id"]}/disable' not in page.text
            assert "member-hash" not in page.text

            denied = await member_client.post(
                f"/admin/accounts/{admin['id']}/disable",
                content=b"not a form", headers={"Content-Type": "text/plain"},
            )
            assert denied.status_code == 403
            bad_csrf = await first.post(
                f"/admin/accounts/{member_id}/disable", data={"csrf_token": "wrong"},
            )
            assert bad_csrf.status_code == 403
            forged = await first.post(
                f"/admin/accounts/{member_id}/disable",
                data={"csrf_token": "route-csrf", "actor_id": str(member_id)},
            )
            assert forged.status_code == 400
            self_refused = await first.post(
                f"/admin/accounts/{admin['id']}/disable",
                data={"csrf_token": "route-csrf"},
            )
            assert self_refused.status_code == 409
            assert "cannot change their own account" in self_refused.text

            disabled = await first.post(
                f"/admin/accounts/{member_id}/disable",
                data={"csrf_token": "route-csrf"},
            )
            assert disabled.status_code == 200
            assert "Account disabled" in disabled.text
            assert f'/admin/accounts/{member_id}/enable' in disabled.text
            assert "disable_account" in disabled.text
            async with owner.connection() as conn:
                state = await (await conn.execute(
                    "SELECT is_enabled,auth_version FROM accounts WHERE id=%s", (member_id,),
                )).fetchone()
            assert state == (False, 2)

            noop = await first.post(
                f"/admin/accounts/{member_id}/disable",
                data={"csrf_token": "route-csrf"},
            )
            assert noop.status_code == 200
            assert "already disabled" in noop.text

            enabled = await first.post(
                f"/admin/accounts/{member_id}/enable",
                data={"csrf_token": "route-csrf"},
            )
            assert enabled.status_code == 200
            assert "Account enabled" in enabled.text
            async with owner.connection() as conn:
                state = await (await conn.execute(
                    "SELECT is_enabled,auth_version FROM accounts WHERE id=%s", (member_id,),
                )).fetchone()
                audit = await (await conn.execute(
                    "SELECT action,outcome FROM account_security_audit "
                    "WHERE target_account_id=%s ORDER BY id", (member_id,),
                )).fetchall()
            assert state == (True, 2)
            assert audit == [
                ("disable_account", "disabled"),
                ("disable_account", "already_disabled"),
                ("enable_account", "enabled"),
            ]

    asyncio.run(_scenario(check))


def test_second_admin_transition_invalidates_the_target_and_rejects_stale_calls():
    async def check(owner, pools, admin):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            second_id = (await (await conn.execute(
                "INSERT INTO accounts(email,password_hash,is_admin) "
                "VALUES ('second@example.invalid','second-hash',true) RETURNING id"
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO account_settings(account_id,display_tz) VALUES (%s,'UTC')",
                (second_id,),
            )

        app = _app(pools)
        async with await _client(app) as first, await _client(app) as second:
            await first.post(f"/test/session/{admin['id']}/1")
            await second.post(f"/test/session/{second_id}/1")
            disabled = await first.post(
                f"/admin/accounts/{second_id}/disable",
                data={"csrf_token": "route-csrf"},
            )
            assert disabled.status_code == 200
            assert (await second.get("/admin/accounts")).status_code == 303
            rejected = await second.post(
                f"/admin/accounts/{admin['id']}/disable",
                data={"csrf_token": "route-csrf"},
            )
            assert rejected.status_code == 303
            async with owner.connection() as conn:
                state = await (await conn.execute(
                    "SELECT is_enabled,auth_version FROM accounts WHERE id=%s", (second_id,),
                )).fetchone()
            assert state == (False, 2)

            await first.post(
                f"/admin/accounts/{second_id}/enable",
                data={"csrf_token": "route-csrf"},
            )
            await second.post(f"/test/session/{second_id}/1")
            assert (await second.get("/admin/accounts")).status_code == 303

    asyncio.run(_scenario(check))


def test_disable_returns_when_target_row_is_locked_without_changing_state():
    async def check(owner, pools, admin):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            member_id = (await (await conn.execute(
                "INSERT INTO accounts(email,password_hash,is_admin) "
                "VALUES ('locked@example.invalid','member-hash',false) RETURNING id"
            )).fetchone())[0]
            await conn.execute(
                "INSERT INTO account_settings(account_id,display_tz) VALUES (%s,'UTC')",
                (member_id,),
            )

        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{admin['id']}/1")
            async with owner.connection() as lock_conn:
                async with lock_conn.transaction():
                    await lock_conn.execute(
                        "SELECT id FROM accounts WHERE id=%s FOR UPDATE", (member_id,),
                    )
                    started = time.monotonic()
                    pending = asyncio.create_task(client.post(
                        f"/admin/accounts/{member_id}/disable",
                        data={"csrf_token": "route-csrf"},
                    ))
                    await asyncio.sleep(0.2)
                    assert not pending.done()
                    response = await asyncio.wait_for(pending, timeout=7.5)
                    elapsed = time.monotonic() - started
                    assert 4.0 <= elapsed < 7.5
                    assert response.status_code == 409
                    assert "Account access could not be changed" in response.text

            async with owner.connection() as conn:
                state = await (await conn.execute(
                    "SELECT is_enabled,auth_version FROM accounts WHERE id=%s", (member_id,),
                )).fetchone()
                audit_count = await (await conn.execute(
                    "SELECT count(*) FROM account_security_audit WHERE target_account_id=%s",
                    (member_id,),
                )).fetchone()
            assert state == (True, 1)
            assert audit_count == (0,)

    asyncio.run(_scenario(check))
