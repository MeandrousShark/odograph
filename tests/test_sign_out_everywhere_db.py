"""Sign out everywhere under the restricted control role."""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from fastapi import Depends, FastAPI, Request, Response
from psycopg import errors
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.accounts import create_admin, get_account, replace_password, sign_out_everywhere
from app.account_context import AccountPrincipal
from app.application_roles import application_role_pools, prepare_application_roles
from app.auth import AuthRedirect, make_router, require_user
from app.db import make_pool
from app.ingest import FailedAuthLimiter
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
                account = await create_admin(conn, "a@example.invalid", "hash-a")
            await callback(owner, pools, account)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


def _app(pools, *, dev_no_auth=False, dev_principal=None):
    from app.main import make_templates

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="disposable-secret", https_only=False)
    config = auth_config(TEST_DB, dev_no_auth=dev_no_auth)
    app.state.config = config
    app.state.control_pool = pools.control
    app.state.runtime_pool = pools.runtime
    app.state.oauth = None
    app.state.login_limiter = FailedAuthLimiter(20, 900)
    app.state.templates = make_templates(config)
    app.state.make_detector_runner = lambda pool: None
    app.state.dev_principal = dev_principal

    @app.exception_handler(AuthRedirect)
    async def auth_redirect(_request, _exc):
        return RedirectResponse("/login", status_code=303)

    @app.post("/test/session/{account_id}/{version}")
    async def session(request: Request, account_id: int, version: int):
        request.session.clear()
        request.session.update(account_id=account_id, auth_version=version, csrf="csrf")
        return Response(status_code=204)

    @app.get("/test/who")
    async def who(user: dict = Depends(require_user)):
        return {"id": user["id"]}

    @app.post("/test/malformed")
    async def malformed(request: Request):
        request.session.clear()
        request.session.update(account_id=True, auth_version=2, csrf="csrf")
        return Response(status_code=204)

    @app.post("/test/transient/{account_id}")
    async def transient(request: Request, account_id: int):
        request.session.clear()
        request.session.update(
            account_id=account_id, auth_version=1, csrf="csrf",
            oidc_browser_nonce="browser-nonce", oidc_action_proof_nonce="proof-nonce",
            oidc_protected_attempt={
                "action": "reauth", "state": "reauth.pending", "nonce": "nonce",
                "browser_nonce": "browser-nonce", "account_id": account_id,
                "auth_version": 1,
            },
        )
        return Response(status_code=204)

    @app.get("/test/transient-state")
    async def transient_state(request: Request):
        return {
            key: key in request.session
            for key in ("oidc_browser_nonce", "oidc_action_proof_nonce", "oidc_protected_attempt")
        }

    app.include_router(make_router())
    return app


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        follow_redirects=False,
    )


async def _wait_for_row_lock(owner, pids):
    deadline = asyncio.get_running_loop().time() + 10
    expected = set(pids)
    while asyncio.get_running_loop().time() < deadline:
        async with owner.connection() as conn:
            rows = await (await conn.execute(
                "SELECT pid,wait_event_type FROM pg_stat_activity WHERE pid = ANY(%s)",
                (list(expected),),
            )).fetchall()
        if {pid for pid, kind in rows if kind == "Lock"} == expected:
            return
        await asyncio.sleep(0.05)
    pytest.fail("security actions did not both reach the account row lock")


def test_route_rejects_get_csrf_stale_disabled_and_malformed_sessions():
    async def check(owner, pools, account):
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{account['id']}/1")
            assert (await client.get("/settings/account/sign-out-everywhere")).status_code == 405
            assert (await client.post("/settings/account/sign-out-everywhere")).status_code == 403
            assert (await client.get("/test/who")).json() == {"id": account["id"]}
            async with pools.control.connection() as conn:
                assert await sign_out_everywhere(conn, account["id"], expected_auth_version=1)
            stale = await client.post(
                "/settings/account/sign-out-everywhere", headers={"X-CSRF-Token": "csrf"})
            assert stale.status_code == 303
            await client.post(f"/test/session/{account['id']}/2")
            async with owner.connection() as conn:
                await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (account["id"],))
            disabled = await client.post(
                "/settings/account/sign-out-everywhere", headers={"X-CSRF-Token": "csrf"})
            assert disabled.status_code == 303
            await client.post("/test/malformed")
            malformed = await client.post(
                "/settings/account/sign-out-everywhere", headers={"X-CSRF-Token": "csrf"})
            assert malformed.status_code == 303
        async with owner.connection() as conn:
            current = await get_account(conn, account["id"])
            assert current["auth_version"] == 2
    asyncio.run(_scenario(check))


def test_ordinary_logout_clears_only_its_cookie_and_dev_mode_rejects_global_action():
    async def check(owner, pools, account):
        app = _app(pools)
        async with await _client(app) as first, await _client(app) as second:
            for client in (first, second):
                await client.post(f"/test/session/{account['id']}/1")
            response = await first.post("/logout", headers={"X-CSRF-Token": "csrf"})
            assert response.status_code == 204
            assert (await first.get("/test/who")).status_code == 303
            assert (await second.get("/test/who")).json() == {"id": account["id"]}
        dev = _app(
            pools, dev_no_auth=True,
            dev_principal=AccountPrincipal(account["id"], True, 1),
        )
        async with await _client(dev) as client:
            await client.post(f"/test/session/{account['id']}/1")
            response = await client.post(
                "/settings/account/sign-out-everywhere", headers={"X-CSRF-Token": "csrf"})
            assert response.status_code == 403
        async with owner.connection() as conn:
            assert (await get_account(conn, account["id"]))["auth_version"] == 1
    asyncio.run(_scenario(check))


def test_ordinary_logout_discards_protected_browser_state_before_callback():
    async def check(owner, pools, account):
        app = _app(pools)
        app.state.oauth = object()
        async with await _client(app) as client:
            await client.post(f"/test/transient/{account['id']}")
            assert all((await client.get("/test/transient-state")).json().values())
            response = await client.post("/logout", headers={"X-CSRF-Token": "csrf"})
            assert response.status_code == 204
            assert not any((await client.get("/test/transient-state")).json().values())
            callback = await client.get("/auth/callback?state=reauth.pending")
            assert callback.status_code == 401
        async with owner.connection() as conn:
            assert (await get_account(conn, account["id"]))["auth_version"] == 1
    asyncio.run(_scenario(check))


def test_sign_out_everywhere_ends_only_current_account_sessions_including_oidc_only():
    async def check(owner, pools, account):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            row = await (await conn.execute(
                "INSERT INTO accounts(email,password_hash,is_admin) "
                "VALUES ('b@example.invalid','hash-b',false) RETURNING id")).fetchone()
            b_id = row[0]
            await conn.execute(
                "INSERT INTO account_settings(account_id,display_tz) VALUES (%s,'UTC')", (b_id,))
            await conn.execute("UPDATE accounts SET password_hash=NULL WHERE id=%s", (account["id"],))
        app = _app(pools)
        async with await _client(app) as a1, await _client(app) as a2, await _client(app) as b:
            for client, account_id in ((a1, account["id"]), (a2, account["id"]), (b, b_id)):
                await client.post(f"/test/session/{account_id}/1")
            assert (await a1.get("/test/who")).json() == {"id": account["id"]}
            assert (await a2.get("/test/who")).json() == {"id": account["id"]}
            assert (await b.get("/test/who")).json() == {"id": b_id}
            stale_page = {"X-Odograph-Account": str(account["id"])}
            assert (await b.get("/test/who", headers=stale_page)).status_code == 409
            assert (await b.post(
                "/settings/account/sign-out-everywhere",
                headers={**stale_page, "X-CSRF-Token": "csrf"},
            )).status_code == 409
            assert (await b.get("/test/who")).json() == {"id": b_id}
            response = await a1.post(
                "/settings/account/sign-out-everywhere",
                headers={"X-CSRF-Token": "csrf", "HX-Request": "true"})
            assert response.status_code == 204
            assert response.headers["HX-Redirect"] == "/login?signed_out=1"
            assert (await a1.get("/test/who")).status_code == 303
            assert (await a2.get("/test/who")).status_code == 303
            assert (await b.get("/test/who")).json() == {"id": b_id}
            assert (await b.post("/logout", headers={"X-CSRF-Token": "csrf"})).status_code == 204
        async with owner.connection() as conn:
            assert (await get_account(conn, account["id"]))["auth_version"] == 2
            assert (await get_account(conn, b_id))["auth_version"] == 1
    asyncio.run(_scenario(check))


def test_plain_post_redirects_to_sign_in_and_clears_submitting_cookie():
    async def check(owner, pools, account):
        app = _app(pools)
        async with await _client(app) as client:
            await client.post(f"/test/session/{account['id']}/1")
            response = await client.post(
                "/settings/account/sign-out-everywhere", headers={"X-CSRF-Token": "csrf"})
            assert response.status_code == 303
            assert response.headers["location"] == "/login?signed_out=1"
            assert (await client.get("/test/who")).status_code == 303
        async with owner.connection() as conn:
            assert (await get_account(conn, account["id"]))["auth_version"] == 2
    asyncio.run(_scenario(check))


def test_revoke_pending_proofs_and_serialize_security_changes():
    async def check(owner, pools, account):
        digest = "a" * 64
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email_verified_at=now() WHERE id=%s", (account["id"],))
            await conn.execute(
                "INSERT INTO email_challenges(account_id,purpose,target_email,issued_email,"
                "issued_auth_version,token_digest,expires_at,initiator) "
                "VALUES (%s,'reset_password','a@example.invalid','a@example.invalid',1,%s,"
                "now()+interval '10 minutes','public')", (account["id"], digest))
            await conn.execute(
                "INSERT INTO oidc_attempts(state_digest,nonce_digest,browser_digest,action,"
                "account_id,auth_version,target,created_at,expires_at) "
                "VALUES (%s,%s,%s,'link',%s,1,'',now(),now()+interval '10 minutes')",
                (digest, "b" * 64, "c" * 64, account["id"]))
            await conn.execute(
                "INSERT INTO oidc_action_proofs(browser_digest,account_id,auth_version,action,"
                "target,created_at,expires_at) VALUES (%s,%s,1,'add_password','',"
                "now(),now()+interval '10 minutes')", ("d" * 64, account["id"]))
        async with pools.control.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("DELETE FROM oidc_action_proofs")
        async with pools.runtime.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT public.sign_out_account_everywhere(%s,1)", (account["id"],))
        ready = asyncio.Queue()
        tasks = []

        async def sign_out():
            async with pools.control.connection() as conn:
                pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                await ready.put(pid)
                return await sign_out_everywhere(conn, account["id"], expected_auth_version=1)

        async def change_password():
            async with pools.control.connection() as conn:
                pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                await ready.put(pid)
                return await replace_password(
                    conn, account["id"], "next-hash", expected_auth_version=1)

        app = _app(pools)
        async with await _client(app) as stale_client:
            await stale_client.post(f"/test/session/{account['id']}/1")
            assert (await stale_client.get("/test/who")).json() == {"id": account["id"]}
            try:
                async with owner.connection() as lock_conn:
                    async with lock_conn.transaction():
                        await lock_conn.execute(
                            "SELECT id FROM accounts WHERE id=%s FOR UPDATE", (account["id"],))
                        tasks.append(asyncio.create_task(sign_out()))
                        sign_out_pid = await asyncio.wait_for(ready.get(), timeout=5)
                        await _wait_for_row_lock(owner, (sign_out_pid,))
                        tasks.append(asyncio.create_task(change_password()))
                        change_pid = await asyncio.wait_for(ready.get(), timeout=5)
                        await _wait_for_row_lock(owner, (sign_out_pid, change_pid))
                signed_out, changed = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            assert signed_out is True
            assert changed is None
            assert (await stale_client.get("/test/who")).status_code == 303
        async with owner.connection() as conn:
            current = await get_account(conn, account["id"])
            assert current["auth_version"] == 2
            assert (await (await conn.execute(
                "SELECT count(*) FROM oidc_attempts WHERE account_id=%s", (account["id"],)
            )).fetchone())[0] == 0
            assert (await (await conn.execute(
                "SELECT count(*) FROM oidc_action_proofs WHERE account_id=%s", (account["id"],)
            )).fetchone())[0] == 0
            assert (await (await conn.execute(
                "SELECT revoked_at IS NOT NULL FROM email_challenges WHERE account_id=%s",
                (account["id"],))).fetchone()) == (True,)
    asyncio.run(_scenario(check))
