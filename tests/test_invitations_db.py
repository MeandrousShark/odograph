from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from psycopg import errors
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app import auth
from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.auth import _account_user
from app.db import make_pool
from app.invitations import (
    InvitationUnavailable, invitation_mail_admission, issue_invitation,
    issue_invitation_record, list_invitations, redeem_invitation,
    resend_invitation_record, revoke_invitation,
)
from app.local_auth import verify_password
from app.local_auth import hash_password
from app.ingest import FailedAuthLimiter
from app.main import SecurityHeadersMiddleware, make_templates
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
                admin = await create_admin(conn, "admin@example.invalid", "existing-hash")
            admin_user = {**_account_user(admin), "auth_version": admin["auth_version"]}
            await callback(owner, pools, admin_user)
    finally:
        try:
            await full_schema_reset(owner)
        finally:
            await owner.close()


async def _wait_for_lock(owner, pid, task):
    for _ in range(150):
        async with owner.connection() as observer:
            row = await (await observer.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,))).fetchone()
        if row == ("Lock",):
            return
        assert not task.done(), "invitation operation bypassed the expected lock"
        await asyncio.sleep(0.01)
    pytest.fail("invitation operation did not wait for the expected lock")


def _invite_route_app(pools):
    cfg = auth_config(TEST_DB, initial_admin_signup=False, dev_no_auth=False)
    app = FastAPI()
    app.state.config = cfg
    app.state.control_pool = pools.control
    app.state.runtime_pool = pools.runtime
    app.state.templates = make_templates(cfg)
    app.state.oauth = None
    app.state.login_limiter = FailedAuthLimiter(20, 900)
    app.state.make_detector_runner = lambda pool: SimpleNamespace(pool=pool)
    app.add_middleware(SessionMiddleware, secret_key="test-secret", https_only=False)
    app.add_middleware(SecurityHeadersMiddleware, tile_host="https://tiles.example", hsts_max_age=0)
    app.include_router(auth.make_router())

    @app.get("/session")
    async def session(request: Request):
        return JSONResponse(dict(request.session))

    return app


def test_restricted_invite_route_respects_singleton_and_switches_session_in_future_fixture():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE accounts SET password_hash=%s WHERE id=%s",
                (hash_password("admin-password"), admin_user["id"]),
            )
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "member@example.invalid")

        app = _invite_route_app(pools)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            await client.get("/invite")
            csrf = (await client.get("/session")).json()["csrf"]
            form = {"csrf_token": csrf, "token": token, "password": "member-password",
                    "password_confirm": "member-password", "display_timezone": "UTC"}
            refused = await client.post("/invite", data=form)
            assert refused.status_code == 400
            assert auth.GENERIC_INVITE_ERROR in refused.text
            assert token not in refused.text
            async with owner.connection() as conn:
                assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")

            signed_in = await client.post(
                "/login/local", data={"email": "admin@example.invalid",
                                      "password": "admin-password", "csrf_token": csrf},
            )
            assert signed_in.status_code == 303
            admin_session = (await client.get("/session")).json()
            assert admin_session["account_id"] == admin_user["id"]
            form["csrf_token"] = admin_session["csrf"]
            redeemed = await client.post("/invite", data=form)
            assert redeemed.status_code == 303
            member_session = (await client.get("/session")).json()
            assert member_session["account_id"] != admin_user["id"]
            assert member_session["csrf"] != admin_session["csrf"]
            assert set(member_session) == {"account_id", "auth_version", "csrf"}
            async with owner.connection() as conn:
                member = await (await conn.execute(
                    "SELECT email,is_admin,password_hash FROM accounts WHERE id=%s",
                    (member_session["account_id"],),
                )).fetchone()
                assert member[0] == "member@example.invalid" and member[1] is False
                assert verify_password("member-password", member[2])
                assert await (await conn.execute(
                    "SELECT consumed_at IS NOT NULL FROM invitations"
                )).fetchone() == (True,)
            replay = await client.post("/invite", data={**form, "csrf_token": member_session["csrf"]})
            assert replay.status_code == 400
            assert auth.GENERIC_INVITE_ERROR in replay.text
            assert (await client.get("/session")).json()["account_id"] == member_session["account_id"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as admin_client:
            await admin_client.get("/login")
            csrf = (await admin_client.get("/session")).json()["csrf"]
            response = await admin_client.post(
                "/login/local", data={"email": "admin@example.invalid",
                                      "password": "admin-password", "csrf_token": csrf},
            )
            assert response.status_code == 303
            assert (await admin_client.get("/session")).json()["account_id"] == admin_user["id"]

    asyncio.run(_scenario(check))


def test_restricted_invite_route_handles_expiry_revoke_disabled_issuer_rollback_and_race():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            tokens = {
                name: await issue_invitation(conn, admin_user, f"{name}@example.invalid")
                for name in ("expired", "revoked", "disabled", "rollback", "race")
            }
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET created_at=now()-interval '49 hours', "
                "expires_at=now()-interval '1 hour' "
                "WHERE email='expired@example.invalid'"
            )
            await conn.execute(
                "UPDATE invitations SET revoked_at=now() WHERE email='revoked@example.invalid'"
            )

        app = _invite_route_app(pools)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            await client.get("/invite")
            csrf = (await client.get("/session")).json()["csrf"]

            async def rejected(token):
                response = await client.post(
                    "/invite", data={"csrf_token": csrf, "token": token,
                                     "password": "member-password", "password_confirm": "member-password",
                                     "display_timezone": "UTC"},
                )
                assert response.status_code == 400
                assert auth.GENERIC_INVITE_ERROR in response.text
                assert all(value not in response.text for value in tokens.values())

            await rejected("invalid-token")
            await rejected(tokens["expired"])
            await rejected(tokens["revoked"])

            async with owner.connection() as conn:
                await conn.execute(
                    "UPDATE accounts SET is_enabled=false WHERE id=%s", (admin_user["id"],)
                )
            try:
                await rejected(tokens["disabled"])
            finally:
                async with owner.connection() as conn:
                    await conn.execute(
                        "UPDATE accounts SET is_enabled=true WHERE id=%s", (admin_user["id"],)
                    )

            async with owner.connection() as conn:
                await conn.execute(
                    "ALTER TABLE vehicles ADD CONSTRAINT invitation_route_failure_probe "
                    f"CHECK (account_id={admin_user['id']})"
                )
            try:
                await rejected(tokens["rollback"])
            finally:
                async with owner.connection() as conn:
                    await conn.execute(
                        "ALTER TABLE vehicles DROP CONSTRAINT invitation_route_failure_probe"
                    )

            async with owner.connection() as conn:
                states = await (await conn.execute(
                    "SELECT email,consumed_at FROM invitations"
                )).fetchall()
                assert len(states) == 5
                assert all(consumed is None for _, consumed in states)
                assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)

            cookie = client.cookies.get("session")
            async with AsyncExitStack() as stack:
                contenders = []
                for _ in range(2):
                    contender = await stack.enter_async_context(httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                        cookies={"session": cookie},
                    ))
                    contenders.append(contender)
                data = {"csrf_token": csrf, "token": tokens["race"],
                        "password": "member-password", "password_confirm": "member-password",
                        "display_timezone": "UTC"}
                responses = await asyncio.gather(
                    *(contender.post("/invite", data=data) for contender in contenders)
                )
                assert sorted(response.status_code for response in responses) == [303, 400]
                assert all(tokens["race"] not in response.text for response in responses)

            async with owner.connection() as conn:
                assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (2,)
                assert await (await conn.execute(
                    "SELECT consumed_at IS NOT NULL FROM invitations WHERE email='race@example.invalid'"
                )).fetchone() == (True,)

    asyncio.run(_scenario(check))


def test_issue_requires_enabled_admin_normalizes_email_and_rate_limits_rotation():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, {**admin_user, "is_admin": False}, "guest@example.invalid")
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, {**admin_user, "is_enabled": False}, "guest@example.invalid")
        async with pools.control.connection() as conn:
            first = await issue_invitation(conn, admin_user, " Guest@Example.Invalid ")
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, admin_user, "guest@example.invalid")
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*),count(*) FILTER (WHERE revoked_at IS NULL) FROM invitations"
            )).fetchone() == (1, 1)
            await conn.execute(
                "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                "expires_at=expires_at-interval '2 minutes' WHERE email='guest@example.invalid'")
        async with pools.control.connection() as conn:
            second = await issue_invitation(conn, admin_user, "guest@example.invalid")
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await issue_invitation(conn, admin_user, "admin@example.invalid")
        async with owner.connection() as conn:
            rows = await (await conn.execute(
                "SELECT token_digest,email,issued_by,expires_at-created_at,revoked_at IS NOT NULL "
                "FROM invitations ORDER BY created_at, token_digest")).fetchall()
            assert len(rows) == 2
            assert {row[0] for row in rows} == {hashlib.sha256(token.encode()).hexdigest() for token in (first, second)}
            assert all(row[1:4] == ("guest@example.invalid", admin_id, rows[0][3]) for row in rows)
            assert all(row[3].total_seconds() == 48 * 3600 for row in rows)
            assert sorted(row[4] for row in rows) == [False, True]
            assert first not in str(rows) and second not in str(rows)
            await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (admin_id,))
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await issue_invitation(conn, admin_user, "next@example.invalid")
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await redeem_invitation(conn, second, "good-password")
    asyncio.run(_scenario(check))


def test_admin_invitation_metadata_revoke_and_final_mail_admission():
    async def check(owner, pools, admin_user):
        async with pools.control.connection() as conn:
            invitation_id, token = await issue_invitation_record(
                conn, admin_user, " Guest@Example.Invalid ")
            rows = await list_invitations(conn, admin_user)
            assert len(rows) == 1
            assert rows[0]["id"] == invitation_id
            assert rows[0]["email"] == "guest@example.invalid"
            assert "token_digest" not in rows[0]
            assert token not in str(rows)
            async with invitation_mail_admission(conn, admin_user, invitation_id) as target:
                assert target == "guest@example.invalid"
            await revoke_invitation(conn, admin_user, invitation_id)
            await revoke_invitation(conn, admin_user, invitation_id)
            with pytest.raises(InvitationUnavailable):
                async with invitation_mail_admission(conn, admin_user, invitation_id):
                    pass
            with pytest.raises(InvitationUnavailable):
                await issue_invitation_record(
                    conn, {**admin_user, "auth_version": admin_user["auth_version"] + 1},
                    "other@example.invalid")
            with pytest.raises(InvitationUnavailable):
                await list_invitations(
                    conn, {**admin_user, "auth_version": admin_user["auth_version"] + 1})
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT public.issue_member_invitation(%s,%s,%s)",
                        (admin_user["id"], "legacy@example.invalid", "0" * 64),
                    )
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL,consumed_at IS NULL FROM invitations WHERE id=%s",
                (invitation_id,),
            )).fetchone() == (True, True)
    asyncio.run(_scenario(check))


def test_resend_requires_current_live_id_and_keeps_token_on_failure():
    async def check(owner, pools, admin_user):
        async with pools.control.connection() as conn:
            old_id, old_token = await issue_invitation_record(
                conn, admin_user, "resend@example.invalid")
            with pytest.raises(InvitationUnavailable):
                await resend_invitation_record(conn, admin_user, old_id)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*),count(*) FILTER (WHERE revoked_at IS NULL) "
                "FROM invitations WHERE email='resend@example.invalid'"
            )).fetchone() == (1, 1)
            await conn.execute(
                "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                "expires_at=expires_at-interval '2 minutes' WHERE id=%s", (old_id,))
        async with pools.control.connection() as conn:
            new_id, target, new_token = await resend_invitation_record(
                conn, admin_user, old_id)
            assert new_id != old_id and new_token != old_token
            assert target == "resend@example.invalid"
            with pytest.raises(InvitationUnavailable):
                await resend_invitation_record(conn, admin_user, old_id)
            with pytest.raises(InvitationUnavailable):
                await resend_invitation_record(
                    conn, {**admin_user, "auth_version": admin_user["auth_version"] + 1}, new_id)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT id FROM invitations WHERE email='resend@example.invalid' "
                "AND revoked_at IS NULL"
            )).fetchone() == (new_id,)
        async with pools.control.connection() as conn:
            await revoke_invitation(conn, admin_user, new_id)
            with pytest.raises(InvitationUnavailable):
                await resend_invitation_record(conn, admin_user, new_id)
    asyncio.run(_scenario(check))


def test_resend_race_with_new_issue_rejects_stale_id():
    async def check(owner, pools, admin_user):
        async with pools.control.connection() as conn:
            old_id, _ = await issue_invitation_record(
                conn, admin_user, "race-resend@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                "expires_at=expires_at-interval '2 minutes' WHERE id=%s", (old_id,))
        async with pools.control.connection() as issuer:
            async with pools.control.connection() as resender:
                pid = (await (await resender.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with issuer.transaction():
                    new_id, _ = await issue_invitation_record(
                        issuer, admin_user, "race-resend@example.invalid")
                    task = asyncio.create_task(resend_invitation_record(
                        resender, admin_user, old_id))
                    await _wait_for_lock(owner, pid, task)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT id FROM invitations WHERE email='race-resend@example.invalid' "
                "AND revoked_at IS NULL"
            )).fetchone() == (new_id,)
            assert await (await conn.execute(
                "SELECT count(*) FROM invitations WHERE email='race-resend@example.invalid'"
            )).fetchone() == (2,)
    asyncio.run(_scenario(check))


def test_resend_race_with_revoke_rejects_terminal_id():
    async def check(owner, pools, admin_user):
        async with pools.control.connection() as conn:
            old_id, _ = await issue_invitation_record(
                conn, admin_user, "revoke-resend@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                "expires_at=expires_at-interval '2 minutes' WHERE id=%s", (old_id,))
        async with pools.control.connection() as revoker:
            async with pools.control.connection() as resender:
                pid = (await (await resender.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with revoker.transaction():
                    await revoke_invitation(revoker, admin_user, old_id)
                    task = asyncio.create_task(resend_invitation_record(
                        resender, admin_user, old_id))
                    await _wait_for_lock(owner, pid, task)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*),count(*) FILTER (WHERE revoked_at IS NULL) "
                "FROM invitations WHERE email='revoke-resend@example.invalid'"
            )).fetchone() == (1, 0)
    asyncio.run(_scenario(check))


def test_admin_invitation_budgets_and_cross_admin_revoke():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
            other = await (await conn.execute(
                "INSERT INTO accounts(email,password_hash,is_admin) "
                "VALUES('admin2@example.invalid','existing-hash',true) "
                "RETURNING id,auth_version,is_enabled,is_admin"
            )).fetchone()
        second_admin = dict(id=other[0], auth_version=other[1],
                            is_enabled=other[2], is_admin=other[3])
        async with pools.control.connection() as conn:
            first_id, first_token = await issue_invitation_record(
                conn, admin_user, "one@example.invalid")
            second_id, _ = await issue_invitation_record(
                conn, second_admin, "two@example.invalid")
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, second_admin, "one@example.invalid")
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT revoked_at FROM invitations WHERE id=%s", (first_id,),
            )).fetchone() == (None,)
            await conn.execute(
                "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                "expires_at=expires_at-interval '2 minutes' WHERE id=%s", (first_id,))
        async with pools.control.connection() as conn:
            replacement_id, replacement_token = await issue_invitation_record(
                conn, second_admin, "one@example.invalid")
            assert replacement_id != first_id and replacement_token != first_token
        async def revoke(actor, invitation_id):
            async with pools.control.connection() as conn:
                await revoke_invitation(conn, actor, invitation_id)
        await asyncio.wait_for(asyncio.gather(
            revoke(admin_user, second_id), revoke(second_admin, replacement_id)
        ), timeout=5)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM invitations WHERE id IN (%s,%s) AND revoked_at IS NOT NULL",
                (second_id, replacement_id),
            )).fetchone() == (2,)
    asyncio.run(_scenario(check))


def test_invitation_target_and_admin_rolling_budgets():
    async def check(owner, pools, admin_user):
        async with pools.control.connection() as conn:
            for _ in range(5):
                await issue_invitation(conn, admin_user, "budget@example.invalid")
                async with owner.connection() as owner_conn:
                    await owner_conn.execute(
                        "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                        "expires_at=expires_at-interval '2 minutes' "
                        "WHERE email='budget@example.invalid'")
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, admin_user, "budget@example.invalid")
            for index in range(5):
                await issue_invitation(conn, admin_user, f"admin-budget-{index}@example.invalid")
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, admin_user, "eleventh@example.invalid")
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*),count(*) FILTER (WHERE revoked_at IS NULL) "
                "FROM invitations WHERE email='budget@example.invalid'"
            )).fetchone() == (5, 1)
            assert await (await conn.execute("SELECT count(*) FROM invitations")).fetchone() == (10,)
            await conn.execute(
                "UPDATE invitations SET created_at=created_at-interval '1 hour', "
                "expires_at=expires_at-interval '1 hour'")
            await conn.execute(
                "INSERT INTO invitations(token_digest,email,issued_by,created_at,expires_at) "
                "SELECT md5(n::text)||md5('extra'||n::text), "
                "'extra-'||n::text||'@example.invalid', %s, "
                "clock_timestamp()-interval '1 hour', clock_timestamp()+interval '47 hours' "
                "FROM generate_series(1,40) n", (admin_user["id"],))
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                await issue_invitation(conn, admin_user, "fifty-first@example.invalid")
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM invitations")).fetchone() == (50,)
    asyncio.run(_scenario(check))


def test_normal_singleton_refuses_member_and_rolls_back_token_use():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "guest@example.invalid")
        async with pools.control.connection() as conn:
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await redeem_invitation(conn, token, "good-password")
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute(
                "SELECT consumed_at IS NULL FROM invitations WHERE token_digest=%s",
                (hashlib.sha256(token.encode()).hexdigest(),))).fetchone() == (True,)
    asyncio.run(_scenario(check))


def test_member_defaults_atomicity_replay_expiry_and_restricted_roles():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "guest@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "ALTER TABLE vehicles ADD CONSTRAINT invitation_failure_probe CHECK (account_id=%s)".replace(
                    "%s", str(admin_id)))
        try:
            async with pools.control.connection() as conn:
                with pytest.raises(InvitationUnavailable):
                    async with conn.transaction():
                        await redeem_invitation(conn, token, "good-password")
        finally:
            async with owner.connection() as conn:
                await conn.execute("ALTER TABLE vehicles DROP CONSTRAINT invitation_failure_probe")
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
        async with pools.control.connection() as conn:
            member_id = await redeem_invitation(conn, token, "good-password", display_timezone="Europe/London")
            with pytest.raises(InvitationUnavailable):
                async with conn.transaction():
                    await redeem_invitation(conn, token, "good-password")
        async with owner.connection() as conn:
            account = await (await conn.execute(
                "SELECT email,password_hash,is_admin FROM accounts WHERE id=%s", (member_id,))).fetchone()
            assert account[0] == "guest@example.invalid" and not account[2]
            assert verify_password("good-password", account[1])
            assert await (await conn.execute("SELECT display_tz FROM account_settings WHERE account_id=%s", (member_id,))).fetchone() == ("Europe/London",)
            assert await (await conn.execute("SELECT name,is_default FROM vehicles WHERE account_id=%s", (member_id,))).fetchall() == [("My Car", True)]
            assert await (await conn.execute("SELECT a_kind,b_kind,category FROM tag_rules WHERE account_id=%s ORDER BY a_kind", (member_id,))).fetchall() == [("home", "work", "personal"), ("work", "work", "business")]
            assert await (await conn.execute("SELECT count(*) FROM mileage_rates WHERE account_id=%s", (member_id,))).fetchone() == await (await conn.execute("SELECT count(*) FROM reference_mileage_rates")).fetchone()
            assert await (await conn.execute("SELECT consumed_at IS NOT NULL FROM invitations")).fetchone() == (True,)
        async with pools.runtime.connection() as conn:
            for statement in (
                "SELECT public.issue_member_invitation(1,'x@example.invalid','a')",
                "SELECT public.issue_member_invitation(1,1,'x@example.invalid','a')",
                "SELECT * FROM public.resend_member_invitation(1,1,1,'a')",
                "SELECT public.revoke_member_invitation(1,1,1)",
                "SELECT * FROM public.list_member_invitations(1,1)",
                "SELECT public.admit_member_invitation_send(1,1,1)",
                "SELECT public.redeem_member_invitation('a','b','UTC')",
                "INSERT INTO accounts(email,password_hash) VALUES('x@example.invalid','hash')",
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(statement)
    asyncio.run(_scenario(check))


def test_expired_revoked_and_concurrent_redemption():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            expired = await issue_invitation(conn, admin_user, "expired@example.invalid")
            revoked = await issue_invitation(conn, admin_user, "revoked@example.invalid")
            live = await issue_invitation(conn, admin_user, "live@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET created_at=now()-interval '49 hours', "
                "expires_at=now()-interval '1 hour' WHERE email='expired@example.invalid'")
            await conn.execute("UPDATE invitations SET revoked_at=now() WHERE email='revoked@example.invalid'")
        for token in (expired, revoked):
            async with pools.control.connection() as conn:
                with pytest.raises(InvitationUnavailable):
                    await redeem_invitation(conn, token, "good-password")
        async def attempt():
            async with pools.control.connection() as conn:
                try:
                    return await redeem_invitation(conn, live, "good-password")
                except InvitationUnavailable:
                    return None
        results = await asyncio.gather(attempt(), attempt())
        assert sum(result is not None for result in results) == 1
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (2,)
    asyncio.run(_scenario(check))


def test_issuer_disable_serializes_with_issue_and_redeem():
    async def check(owner, pools, admin_user):
        admin_id = admin_user["id"]
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "member@example.invalid")

        async def disabled_before(operation):
            async with pools.control.connection() as control:
                pid = (await (await control.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with owner.connection() as blocker:
                    async with blocker.transaction():
                        await blocker.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (admin_id,))
                        task = asyncio.create_task(operation(control))
                        await _wait_for_lock(owner, pid, task)
                    with pytest.raises(InvitationUnavailable):
                        await task

        await disabled_before(lambda conn: issue_invitation(conn, admin_user, "other@example.invalid"))
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET is_enabled=true WHERE id=%s", (admin_id,))
        await disabled_before(lambda conn: redeem_invitation(conn, token, "good-password"))
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
    asyncio.run(_scenario(check))


def test_issue_and_redeem_serialize_per_email_in_both_orders():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")

        async with pools.control.connection() as conn:
            old_token = await issue_invitation(conn, admin_user, "race@example.invalid")

        async with pools.control.connection() as issuer:
            async with pools.control.connection() as redeemer:
                async with owner.connection() as conn:
                    await conn.execute(
                        "UPDATE invitations SET created_at=created_at-interval '2 minutes', "
                        "expires_at=expires_at-interval '2 minutes' WHERE email='race@example.invalid'")
                pid = (await (await redeemer.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with issuer.transaction():
                    new_token = await issue_invitation(issuer, admin_user, "race@example.invalid")
                    task = asyncio.create_task(redeem_invitation(redeemer, old_token, "good-password"))
                    await _wait_for_lock(owner, pid, task)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL,consumed_at IS NULL FROM invitations "
                "WHERE token_digest=%s", (hashlib.sha256(old_token.encode()).hexdigest(),))).fetchone() == (True, True)

        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "winner@example.invalid")
        async with pools.control.connection() as redeemer:
            async with pools.control.connection() as issuer:
                pid = (await (await issuer.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with redeemer.transaction():
                    member_id = await redeem_invitation(redeemer, token, "good-password")
                    task = asyncio.create_task(issue_invitation(issuer, admin_user, "winner@example.invalid"))
                    await _wait_for_lock(owner, pid, task)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT email FROM accounts WHERE id=%s", (member_id,))).fetchone() == ("winner@example.invalid",)
            assert await (await conn.execute(
                "SELECT revoked_at,consumed_at IS NOT NULL FROM invitations "
                "WHERE token_digest=%s", (hashlib.sha256(token.encode()).hexdigest(),))).fetchone() == (None, True)
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (2,)
            assert new_token != old_token
    asyncio.run(_scenario(check))


def test_expiry_while_waiting_for_email_lock_refuses_redemption():
    async def check(owner, pools, admin_user):
        async with owner.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
        async with pools.control.connection() as conn:
            token = await issue_invitation(conn, admin_user, "soon@example.invalid")
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE invitations SET expires_at=pg_catalog.clock_timestamp()+interval '1 second' "
                "WHERE email='soon@example.invalid'")
        async with owner.connection() as blocker:
            async with pools.control.connection() as redeemer:
                pid = (await (await redeemer.execute("SELECT pg_backend_pid()")).fetchone())[0]
                async with blocker.transaction():
                    await blocker.execute(
                        "SELECT pg_advisory_xact_lock(901410,hashtext('soon@example.invalid'))")
                    task = asyncio.create_task(redeem_invitation(redeemer, token, "good-password"))
                    await _wait_for_lock(owner, pid, task)
                    await asyncio.sleep(1.1)
                with pytest.raises(InvitationUnavailable):
                    await task
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM accounts")).fetchone() == (1,)
            assert await (await conn.execute("SELECT consumed_at FROM invitations")).fetchone() == (None,)
    asyncio.run(_scenario(check))
