"""Administrator account routes under the restricted application roles."""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from app import admin
from app.invitations import issue_invitation_record
from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.auth import AuthRedirect
from app.db import make_pool
from app.ingest import FailedAuthLimiter
from app.main import SecurityHeadersMiddleware, make_templates
from app.password_reset import SecurityMailAdmission
from tests.auth_db_fixtures import auth_config
from conftest import full_schema_reset

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
                account = await create_admin(conn, "admin@example.invalid", "test-hash")
            await callback(owner, pools, account)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


def _app(pools, *, config=None):
    cfg = config or auth_config(TEST_DB, initial_admin_signup=False, dev_no_auth=False)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="admin-route-test-secret", https_only=False)
    app.add_middleware(SecurityHeadersMiddleware, tile_host="https://tiles.example", hsts_max_age=0)
    app.state.config = cfg
    app.state.control_pool = pools.control
    app.state.runtime_pool = pools.runtime
    app.state.templates = make_templates(cfg)
    app.state.oauth = None
    app.state.login_limiter = FailedAuthLimiter(20, 900)
    app.state.make_detector_runner = lambda _pool: None
    app.state.security_mail = SecurityMailAdmission()
    app.state.password_reset_queue = None

    @app.exception_handler(AuthRedirect)
    async def auth_redirect(_request, _exc):
        return RedirectResponse("/login", status_code=303)

    @app.post("/test/session/{account_id}/{version}")
    async def set_session(request: Request, account_id: int, version: int):
        request.session.clear()
        request.session.update(account_id=account_id, auth_version=version, csrf="route-csrf")
        return Response(status_code=204)

    @app.get("/test/session")
    async def read_session(request: Request):
        return JSONResponse(dict(request.session))

    app.include_router(admin.make_router())
    return app


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver",
        follow_redirects=False,
    )


def test_admin_routes_require_a_live_admin_and_ignore_forged_actor_fields():
    async def check(owner, pools, account):
        app = _app(pools)
        async with await _client(app) as admin_client, await _client(app) as member_client:
            await admin_client.post(f"/test/session/{account['id']}/1")

            page = await admin_client.get("/admin/accounts")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store, private"
            assert page.headers["referrer-policy"] == "no-referrer"
            assert "Invitations are unavailable until multi-account mode is activated." in page.text
            assert "Administrator" in page.text

            locked = await admin_client.post("/admin/invitations", data={
                "csrf_token": "route-csrf", "email": "new@example.invalid",
            })
            assert locked.status_code == 409

            stale = await _client(app)
            async with stale:
                await stale.post(f"/test/session/{account['id']}/2")
                assert (await stale.get("/admin/accounts")).status_code == 303

            async with owner.connection() as conn:
                await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (account["id"],))
            disabled = await _client(app)
            async with disabled:
                await disabled.post(f"/test/session/{account['id']}/1")
                assert (await disabled.get("/admin/accounts")).status_code == 303
            async with owner.connection() as conn:
                await conn.execute("UPDATE accounts SET is_enabled=true WHERE id=%s", (account["id"],))

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
            assert (await member_client.get("/admin/accounts")).status_code == 403
            denied_before_body_parse = await member_client.post(
                "/admin/invitations", content=b"not a form", headers={"Content-Type": "text/plain"},
            )
            assert denied_before_body_parse.status_code == 403

            forged = await admin_client.post("/admin/invitations", data={
                "csrf_token": "route-csrf", "email": "new@example.invalid",
                "actor_id": str(member_id),
            })
            assert forged.status_code == 400
            bad_csrf = await admin_client.post("/admin/invitations", data={
                "csrf_token": "wrong", "email": "new@example.invalid",
            })
            assert bad_csrf.status_code == 403

    asyncio.run(_scenario(check))


def test_admin_invitation_token_is_copy_once_and_never_in_server_urls_or_lists(monkeypatch):
    delivered = []
    fail_delivery = [False]

    async def capture_send(_mailer, message):
        if fail_delivery[0]:
            raise OSError("disposable SMTP failure")
        delivered.append(message)

    monkeypatch.setattr("app.mailer.Mailer.send", capture_send)

    async def check(owner, pools, account):
        cfg = auth_config(
            TEST_DB, initial_admin_signup=False, dev_no_auth=False,
            smtp_host="smtp.example.invalid", email_from="odograph@example.invalid",
            app_url="https://odograph.example.invalid",
        )
        app = _app(pools, config=cfg)
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with await _client(app) as client:
            await client.post(f"/test/session/{account['id']}/1")
            issued = await client.post("/admin/invitations", data={
                "csrf_token": "route-csrf", "email": "invitee@example.invalid", "send_email": "1",
            })
            assert issued.status_code == 200
            assert issued.headers["cache-control"] == "no-store, private"
            assert issued.headers["referrer-policy"] == "no-referrer"
            assert "The configured SMTP server accepted the invitation email." in issued.text
            token = re.search(r'id="admin-invitation-token" value="([^"]+)"', issued.text).group(1)
            link = re.search(r'id="admin-invitation-link" value="([^"]+)"', issued.text).group(1)
            assert link == f"https://odograph.example.invalid/invite#token={token}"
            assert token not in str(issued.request.url)
            assert not issued.request.url.query
            assert len(delivered) == 1
            assert delivered[0]["To"] == "invitee@example.invalid"
            assert token in delivered[0].get_content()
            assert f"/invite#token={token}" in delivered[0].get_content()

            listing = await client.get("/admin/accounts")
            assert listing.status_code == 200
            assert token not in listing.text
            assert "token_digest" not in listing.text
            assert "invitee@example.invalid" in listing.text

            async with owner.connection() as conn:
                invitation = await (await conn.execute(
                    "SELECT token_digest, consumed_at, revoked_at FROM invitations"
                )).fetchone()
            assert invitation[0] != token
            assert invitation[1:] == (None, None)

            await app.state.security_mail._slots.acquire()
            await app.state.security_mail._slots.acquire()
            try:
                full = await client.post("/admin/invitations", data={
                    "csrf_token": "route-csrf", "email": "full@example.invalid", "send_email": "1",
                })
            finally:
                app.state.security_mail._slots.release()
                app.state.security_mail._slots.release()
            assert "The invitation email was not sent." in full.text
            full_token = re.search(r'id="admin-invitation-token" value="([^"]+)"', full.text).group(1)
            async with owner.connection() as conn:
                full_row = await (await conn.execute(
                    "SELECT consumed_at, revoked_at FROM invitations WHERE email='full@example.invalid'"
                )).fetchone()
            assert full_row == (None, None)
            assert full_token not in str(full.request.url)

            fail_delivery[0] = True
            uncertain = await client.post("/admin/invitations", data={
                "csrf_token": "route-csrf", "email": "uncertain@example.invalid", "send_email": "1",
            })
            assert "Email delivery could not be confirmed." in uncertain.text
            uncertain_token = re.search(
                r'id="admin-invitation-token" value="([^"]+)"', uncertain.text,
            ).group(1)
            async with owner.connection() as conn:
                uncertain_row = await (await conn.execute(
                    "SELECT consumed_at, revoked_at FROM invitations "
                    "WHERE email='uncertain@example.invalid'"
                )).fetchone()
            assert uncertain_row == (None, None)
            assert uncertain_token not in str(uncertain.request.url)

            async with owner.connection() as conn:
                invite_row = await conn.execute(
                    "SELECT id FROM invitations WHERE email='invitee@example.invalid'"
                )
                invitation_id = (await invite_row.fetchone())[0]
            revoked = await client.post(
                f"/admin/invitations/{invitation_id}/revoke", data={"csrf_token": "route-csrf"},
            )
            assert revoked.status_code == 200
            assert "Revoked" in revoked.text
            assert token not in revoked.text

    asyncio.run(_scenario(check))


@pytest.mark.parametrize("barrier", ["before_admission", "inside_transaction"])
def test_cancelled_invitation_send_owns_its_real_pool_connection(monkeypatch, barrier):
    async def check(owner, pools, account):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        actor = dict(account, auth_version=1)
        async with pools.control.connection() as conn:
            invitation_id, token = await issue_invitation_record(
                conn, actor, "invitee@example.invalid",
            )
        cfg = auth_config(
            TEST_DB, initial_admin_signup=False, dev_no_auth=False,
            smtp_host="smtp.example.invalid", email_from="odograph@example.invalid",
            app_url="https://odograph.example.invalid",
        )
        admission = SecurityMailAdmission(limit=1)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            control_pool=pools.control, security_mail=admission, config=cfg,
        )))
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []
        original_lease = admin.external_account_work
        original_admission = admin.invitation_mail_admission
        original_connection = admin.control_connection
        checked_out = set()

        @asynccontextmanager
        async def tracked_connection(pool, **kwargs):
            async with original_connection(pool, **kwargs) as conn:
                checked_out.add(id(conn))
                try:
                    yield conn
                finally:
                    checked_out.remove(id(conn))

        @asynccontextmanager
        async def lease(*args):
            if barrier == "before_admission":
                entered.set()
                await release.wait()
            async with original_lease(*args):
                yield

        @asynccontextmanager
        async def admit(conn, *args):
            assert id(conn) in checked_out
            async with original_admission(conn, *args) as target:
                if barrier == "inside_transaction":
                    entered.set()
                    await release.wait()
                    assert (await (await conn.execute("SELECT pg_backend_pid()"))
                            .fetchone())[0] > 0
                yield target

        async def send(_mailer, _message):
            sent.append(True)

        monkeypatch.setattr(admin, "external_account_work", lease)
        monkeypatch.setattr(admin, "control_connection", tracked_connection)
        monkeypatch.setattr(admin, "invitation_mail_admission", admit)
        monkeypatch.setattr("app.mailer.Mailer.send", send)
        caller = asyncio.create_task(admin._send_invitation_email(
            request, actor, invitation_id, "invitee@example.invalid", token,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert len(admission._tasks) == 1
        finally:
            release.set()
            await admission.drain()
        assert sent == [True]
        async with owner.connection() as conn:
            row = await (await conn.execute(
                "SELECT revoked_at, consumed_at FROM invitations WHERE id=%s", (invitation_id,),
            )).fetchone()
            assert row == (None, None)

    asyncio.run(_scenario(check))


def test_admin_resend_uses_atomic_rotation_and_rejects_a_stale_invitation():
    async def check(owner, pools, account):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{account['id']}/1")
            first = await client.post("/admin/invitations", data={
                "csrf_token": "route-csrf", "email": "resend@example.invalid",
            })
            old_token = re.search(r'id="admin-invitation-token" value="([^"]+)"', first.text).group(1)
            async with owner.connection() as conn:
                row = await (await conn.execute(
                    "SELECT id FROM invitations WHERE email='resend@example.invalid'"
                )).fetchone()
                old_id = row[0]
                await conn.execute(
                    "UPDATE invitations SET created_at=now()-interval '2 minutes' WHERE id=%s",
                    (old_id,),
                )

            resent = await client.post(
                f"/admin/invitations/{old_id}/resend", data={"csrf_token": "route-csrf"},
            )
            assert resent.status_code == 200
            new_token = re.search(r'id="admin-invitation-token" value="([^"]+)"', resent.text).group(1)
            assert new_token != old_token
            assert old_token not in resent.text
            async with owner.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT id, consumed_at, revoked_at FROM invitations "
                    "WHERE email='resend@example.invalid' ORDER BY id"
                )).fetchall()
            assert len(rows) == 2
            assert rows[0][2] is not None
            assert rows[1][1:] == (None, None)

            stale = await client.post(
                f"/admin/invitations/{old_id}/resend", data={"csrf_token": "route-csrf"},
            )
            assert stale.status_code == 400
            assert new_token not in stale.text
            async with owner.connection() as conn:
                assert await (await conn.execute(
                    "SELECT count(*) FROM invitations WHERE email='resend@example.invalid'"
                )).fetchone() == (2,)

    asyncio.run(_scenario(check))


def test_admin_recovery_queues_only_a_verified_target_and_binds_actor_identity():
    async def check(owner, pools, account):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            member_id = (await (await conn.execute(
                "INSERT INTO accounts(email,password_hash,is_admin,email_verified_at) "
                "VALUES ('verified-member@example.invalid','member-hash',false,now()) RETURNING id"
            )).fetchone())[0]
            unverified_id = (await (await conn.execute(
                "INSERT INTO accounts(email,password_hash,is_admin) "
                "VALUES ('unverified-member@example.invalid','member-hash',false) RETURNING id"
            )).fetchone())[0]
            for target_id in (member_id, unverified_id):
                await conn.execute(
                    "INSERT INTO account_settings(account_id,display_tz) VALUES (%s,'UTC')",
                    (target_id,),
                )
        queued = []

        class Queue:
            def submit_admin(self, actor_id, actor_auth_version, target_account_id):
                queued.append((actor_id, actor_auth_version, target_account_id))
                return True

        app = _app(pools)
        app.state.password_reset_queue = Queue()
        async with await _client(app) as client:
            await client.post(f"/test/session/{account['id']}/1")
            response = await client.post(
                f"/admin/accounts/{member_id}/recovery", data={"csrf_token": "route-csrf"},
            )
            assert response.status_code == 200
            assert "Password recovery was queued. Delivery is not confirmed." in response.text
            assert queued == [(account["id"], 1, member_id)]
            assert "password reset token" not in response.text.lower()

            unverified = await client.post(
                f"/admin/accounts/{unverified_id}/recovery", data={"csrf_token": "route-csrf"},
            )
            assert "no verified login address" in unverified.text
            assert queued == [(account["id"], 1, member_id)]

    asyncio.run(_scenario(check))


@pytest.mark.capacity_contract
@pytest.mark.parametrize("action", ["disable", "invite"])
def test_committed_admin_outcome_survives_saturated_identity_refresh(monkeypatch, action):
    from dataclasses import replace
    from app.capacity import AdmissionManager
    from app.role_setup import RolePools

    async def check(owner, raw_pools, account):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) VALUES (42,'member@example.invalid','unused',false)")
        cfg = replace(auth_config(TEST_DB), capacity_identity_pending=0)
        manager = AdmissionManager(cfg)
        pools = RolePools(manager.manage_pool(raw_pools.control, "control"),
                          manager.manage_pool(raw_pools.runtime, "runtime"))
        app = _app(pools, config=cfg)
        app.state.capacity = manager
        app.state.security_mail = SecurityMailAdmission(capacity=manager)
        original = admin._load_page_data
        entered = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        holders = []

        async def occupy(index):
            async with manager.operation("identity"):
                entered[index].set()
                await release.wait()

        async def busy_refresh(request, actor):
            holders.extend(asyncio.create_task(occupy(i)) for i in range(2))
            await asyncio.gather(*(event.wait() for event in entered))
            return await original(request, actor)

        monkeypatch.setattr(admin, "_load_page_data", busy_refresh)
        try:
            async with await _client(app) as client:
                await client.post(f"/test/session/{account['id']}/1")
                if action == "disable":
                    response = await client.post("/admin/accounts/42/disable", data={"csrf_token": "route-csrf"})
                    assert response.status_code == 200
                    assert "Account disabled." in response.text
                    async with owner.connection() as conn:
                        assert (await (await conn.execute("SELECT is_enabled FROM accounts WHERE id=42")).fetchone()) == (False,)
                else:
                    response = await client.post("/admin/invitations", data={
                        "csrf_token": "route-csrf", "email": "invited@example.invalid",
                    })
                    assert response.status_code == 200
                    assert "Invitation issued." in response.text
                    assert "Invitation token: <code>" in response.text
                    assert "Email was not requested." in response.text
                    async with owner.connection() as conn:
                        assert (await (await conn.execute("SELECT count(*) FROM invitations WHERE email='invited@example.invalid'")).fetchone()) == (1,)
                assert response.headers["cache-control"] == "no-store, private"
                assert "Reload account details" in response.text
        finally:
            release.set()
            await asyncio.gather(*holders, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(_scenario(check))
