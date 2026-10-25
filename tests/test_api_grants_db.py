"""API grants under the restricted control role: rotation, retry, reuse,
revocation, the active-grant limit and the bearer-only API routes."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets

import httpx
import pytest
from fastapi import FastAPI, Request
from psycopg import errors
from starlette.responses import Response

from app.accounts import create_admin, get_account, replace_password, sign_out_everywhere
from app.account_lifecycle import purge_account, request_account_deletion, set_account_enabled
from app.api_errors import ERRORS, register_api_errors
from app.api_grants import (
    ACCESS_TOKEN_SECONDS, ACTIVE_GRANT_LIMIT, DEFAULT_DEVICE_LABEL, GrantLimitReached,
    authenticate_access_token, check_access_token, create_grant, refresh_grant, revoke_grant,
    token_digest,
)
from app.api_v1 import WebSessionMiddleware, make_router
from app.db import make_pool
from app.detector.runner import DETECTOR_VERSION
from app.email_challenges import PURPOSE_CHANGE, consume_email_challenge, issue_email_challenge
from app.ingest import FailedAuthLimiter
from app.oidc_identities import create_identity_link, unlink_identity
from app.password_reset import INITIATOR_PUBLIC, consume_password_reset, issue_password_reset
from tests.auth_db_fixtures import auth_config
from conftest import provisioned_role_pools, reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
ADMIN_EMAIL = "admin@example.invalid"
MEMBER_EMAIL = "member@example.invalid"
ISSUER = "https://idp.example/"


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await reset_db(owner)
        async with provisioned_role_pools(owner) as pools:
            async with pools.control.connection() as conn:
                account = await create_admin(conn, ADMIN_EMAIL, "hash-a")
            await callback(owner, pools, account)
    finally:
        await owner.close()


def _run(callback):
    asyncio.run(_scenario(callback))


async def _add_member(owner, email=MEMBER_EMAIL) -> int:
    async with owner.connection() as conn:
        member = (await (await conn.execute(
            "INSERT INTO accounts(email,password_hash,is_admin) VALUES (%s,'hash-b',false) RETURNING id",
            (email,))).fetchone())[0]
        await conn.execute("INSERT INTO account_settings(account_id) VALUES (%s)", (member,))
    return member


def _app(pools, limiter=None):
    app = FastAPI()
    app.add_middleware(WebSessionMiddleware, secret_key="disposable-secret", https_only=False)
    app.state.config = auth_config(TEST_DB)
    app.state.control_pool = pools.control
    app.state.runtime_pool = pools.runtime
    app.state.make_detector_runner = lambda pool: None
    app.state.api_auth_limiter = limiter or FailedAuthLimiter(1000, 900)

    @app.post("/test/session/{account_id}/{version}")
    async def session(request: Request, account_id: int, version: int):
        request.session.update(account_id=account_id, auth_version=version, csrf="csrf")
        return Response(status_code=204)

    register_api_errors(app)
    app.include_router(make_router())
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _mint(pools, account_id, auth_version=1, label="Phone", replaces=None):
    async with pools.control.connection() as conn:
        return await create_grant(conn, account_id, auth_version, label, replaces=replaces)


async def _refresh(pools, token):
    async with pools.control.connection() as conn:
        return await refresh_grant(conn, token)


async def _authenticate(pools, token):
    async with pools.control.connection() as conn:
        return await authenticate_access_token(conn, token)


async def _grant(owner, public_id):
    async with owner.connection() as conn:
        cur = await conn.execute(
            "SELECT revoked_at, generation, current_seen, rotated_at, last_used_at, device_label, "
            "access_expires_at, previous_access_expires_at FROM api_grants WHERE public_id=%s",
            (public_id,))
        return await cur.fetchone()


async def _sql(owner, statement, params=()):
    async with owner.connection() as conn:
        await conn.execute(statement, params)


async def _audit_rows(owner):
    async with owner.connection() as conn:
        return await (await conn.execute(
            "SELECT actor_account_id, target_account_id, action, outcome FROM account_security_audit"
        )).fetchall()


def test_rotation_returns_a_new_pair_and_previous_access_works_until_its_expiry():
    async def check(owner, pools, account):
        first = await _mint(pools, account["id"])
        assert (await _authenticate(pools, first.access_token)).grant_id == first.grant_id
        rotated = (await _refresh(pools, first.refresh_token)).tokens
        assert rotated.grant_id == first.grant_id
        assert {rotated.access_token, rotated.refresh_token}.isdisjoint(
            {first.access_token, first.refresh_token})
        row = await _grant(owner, first.grant_id)
        assert row[0] is None and row[1] == 2 and row[2] is False and row[3] is not None
        assert row[6] == rotated.access_expires_at
        assert (await _authenticate(pools, first.access_token)).grant_id == first.grant_id
        assert (await _authenticate(pools, rotated.access_token)).grant_id == first.grant_id
        await _sql(owner, "UPDATE api_grants SET previous_access_expires_at=now()-interval '1 second'")
        assert await _authenticate(pools, first.access_token) is None
        assert await _authenticate(pools, rotated.access_token) is not None
        await _sql(owner, "UPDATE api_grants SET access_expires_at=now()-interval '1 second'")
        assert await _authenticate(pools, rotated.access_token) is None
        # An expired access token says nothing about the grant: refresh still works.
        assert (await _refresh(pools, rotated.refresh_token)).tokens is not None
    _run(check)


def test_lifetimes_are_fifteen_minutes_for_access():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        async with owner.connection() as conn:
            created, expires = await (await conn.execute(
                "SELECT created_at, access_expires_at FROM api_grants")).fetchone()
        assert (expires - created).total_seconds() == ACCESS_TOKEN_SECONDS
        assert issued.access_expires_at == expires
    _run(check)


async def _wait_for_lock_waiters(owner, pids):
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        async with owner.connection() as conn:
            rows = await (await conn.execute(
                "SELECT pid FROM pg_stat_activity WHERE pid = ANY(%s) AND wait_event_type = 'Lock'",
                (list(pids),))).fetchall()
        if {row[0] for row in rows} == set(pids):
            return
        await asyncio.sleep(0.05)
    pytest.fail("refreshes did not both wait on the grant row lock")


def test_concurrent_refreshes_with_one_token_leave_one_live_unrevoked_grant():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        pids = []

        async def refresh():
            async with pools.control.connection() as conn:
                pids.append(conn.info.backend_pid)
                return await refresh_grant(conn, issued.refresh_token)

        async with owner.connection() as holder:
            await holder.execute("SELECT 1 FROM api_grants WHERE public_id=%s FOR UPDATE", (issued.grant_id,))
            tasks = [asyncio.create_task(refresh()) for _ in range(2)]
            while len(pids) < 2:
                await asyncio.sleep(0.01)
            await _wait_for_lock_waiters(owner, pids)
        outcomes = await asyncio.gather(*tasks)
        assert all(outcome.tokens is not None and not outcome.reuse_detected for outcome in outcomes)
        row = await _grant(owner, issued.grant_id)
        assert row[0] is None and row[1] == 2
        live = [outcome for outcome in outcomes
                if await _authenticate(pools, outcome.tokens.access_token) is not None]
        assert len(live) == 1
        assert (await _refresh(pools, live[0].tokens.refresh_token)).tokens is not None
        assert await _audit_rows(owner) == []
    _run(check)


def test_retry_inside_the_window_replaces_the_unseen_pair_without_extending_it():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        lost = (await _refresh(pools, issued.refresh_token)).tokens
        rotated_at = (await _grant(owner, issued.grant_id))[3]
        retried = (await _refresh(pools, issued.refresh_token)).tokens
        assert retried is not None and retried.access_token != lost.access_token
        row = await _grant(owner, issued.grant_id)
        assert row[0] is None and row[1] == 2 and row[3] == rotated_at
        # The discarded pair is gone; the previous pair still works.
        assert await _authenticate(pools, lost.access_token) is None
        assert (await _refresh(pools, lost.refresh_token)).tokens is None
        assert await _authenticate(pools, issued.access_token) is not None
        # Retries never move the window: 61 seconds after the first rotation
        # the previous token is refused, without revoking.
        await _sql(owner, "UPDATE api_grants SET rotated_at=rotated_at-interval '50 seconds'")
        again = (await _refresh(pools, issued.refresh_token)).tokens
        assert again is not None
        await _sql(owner, "UPDATE api_grants SET rotated_at=rotated_at-interval '11 seconds'")
        late = await _refresh(pools, issued.refresh_token)
        assert late.tokens is None and not late.reuse_detected
        assert (await _grant(owner, issued.grant_id))[0] is None
        assert (await _refresh(pools, again.refresh_token)).tokens is not None
        assert await _audit_rows(owner) == []
    _run(check)


def test_previous_refresh_after_the_new_pair_is_used_revokes_and_audits_once():
    async def check(owner, pools, account):
        other = await _mint(pools, account["id"], label="Tablet")
        issued = await _mint(pools, account["id"])
        rotated = (await _refresh(pools, issued.refresh_token)).tokens
        assert await _authenticate(pools, rotated.access_token) is not None
        assert (await _grant(owner, issued.grant_id))[2] is True
        reuse = await _refresh(pools, issued.refresh_token)
        assert reuse.tokens is None and reuse.reuse_detected
        assert (await _grant(owner, issued.grant_id))[0] is not None
        assert await _audit_rows(owner) == [
            (account["id"], account["id"], "api_grant_refresh_reuse", "revoked")]
        for token in (rotated.access_token, issued.access_token):
            assert await _authenticate(pools, token) is None
        for token in (rotated.refresh_token, issued.refresh_token):
            assert (await _refresh(pools, token)).tokens is None
        assert len(await _audit_rows(owner)) == 1
        assert await _authenticate(pools, other.access_token) is not None
    _run(check)


def test_stale_and_two_generations_old_tokens_are_refused_without_revoking():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        second = (await _refresh(pools, issued.refresh_token)).tokens
        await _sql(owner, "UPDATE api_grants SET rotated_at=rotated_at-interval '61 seconds'")
        stale = await _refresh(pools, issued.refresh_token)
        assert stale.tokens is None and not stale.reuse_detected
        third = (await _refresh(pools, second.refresh_token)).tokens
        old = await _refresh(pools, issued.refresh_token)
        assert old.tokens is None and not old.reuse_detected
        assert await _authenticate(pools, issued.access_token) is None
        row = await _grant(owner, issued.grant_id)
        assert row[0] is None and row[1] == 3
        assert (await _refresh(pools, third.refresh_token)).tokens is not None
        assert (await _refresh(pools, secrets.token_urlsafe(32))).tokens is None
        assert await _audit_rows(owner) == []
    _run(check)


def test_idle_grants_expire_and_are_deleted_at_the_next_creation():
    async def check(owner, pools, account):
        idle = await _mint(pools, account["id"])
        used = await _mint(pools, account["id"])
        revoked = await _mint(pools, account["id"])
        async with pools.control.connection() as conn:
            assert await revoke_grant(conn, account["id"], revoked.grant_id)
        await _sql(owner, "UPDATE api_grants SET created_at=now()-interval '61 days', "
                          "last_used_at=now()-interval '61 days' WHERE public_id=%s", (idle.grant_id,))
        await _sql(owner, "UPDATE api_grants SET created_at=now()-interval '90 days', "
                          "last_used_at=now()-interval '59 days' WHERE public_id=%s", (used.grant_id,))
        assert await _authenticate(pools, idle.access_token) is None
        assert (await _refresh(pools, idle.refresh_token)).tokens is None
        assert (await _refresh(pools, used.refresh_token)).tokens is not None
        await _mint(pools, account["id"])
        async with owner.connection() as conn:
            remaining = {row[0] for row in await (await conn.execute(
                "SELECT public_id FROM api_grants")).fetchall()}
        assert idle.grant_id not in remaining and revoked.grant_id not in remaining
        assert used.grant_id in remaining and len(remaining) == 2
    _run(check)


def test_last_use_is_written_at_most_once_a_minute():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        assert (await _grant(owner, issued.grant_id))[4] is None
        await _authenticate(pools, issued.access_token)
        first = (await _grant(owner, issued.grant_id))[4]
        await _authenticate(pools, issued.access_token)
        assert (await _grant(owner, issued.grant_id))[4] == first
        await _sql(owner, "UPDATE api_grants SET last_used_at=last_used_at-interval '61 seconds'")
        await _authenticate(pools, issued.access_token)
        assert (await _grant(owner, issued.grant_id))[4] > first
    _run(check)


def test_revoking_one_grant_leaves_the_others_working():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        first = await _mint(pools, account["id"])
        second = await _mint(pools, account["id"])
        theirs = await _mint(pools, member)
        async with pools.control.connection() as conn:
            assert not await revoke_grant(conn, member, first.grant_id)
            assert await revoke_grant(conn, account["id"], first.grant_id)
            assert not await revoke_grant(conn, account["id"], first.grant_id)
            assert not await revoke_grant(conn, account["id"], "not a public id")
        revoked_at = (await _grant(owner, first.grant_id))[0]
        assert revoked_at is not None
        async with pools.control.connection() as conn:
            await revoke_grant(conn, account["id"], first.grant_id)
        assert (await _grant(owner, first.grant_id))[0] == revoked_at
        assert await _authenticate(pools, first.access_token) is None
        assert (await _refresh(pools, first.refresh_token)).tokens is None
        assert await _authenticate(pools, second.access_token) is not None
        assert await _authenticate(pools, theirs.access_token) is not None
    _run(check)


def test_grant_limit_refuses_without_revoking_and_replaces_is_net_zero():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        theirs = await _mint(pools, member)
        grants = [await _mint(pools, account["id"], label=f"Phone {n}") for n in range(ACTIVE_GRANT_LIMIT)]
        with pytest.raises(GrantLimitReached):
            await _mint(pools, account["id"])
        # Another account's grant is not this account's to replace.
        with pytest.raises(GrantLimitReached):
            await _mint(pools, account["id"], replaces=theirs.grant_id)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM api_grants WHERE account_id=%s AND revoked_at IS NULL",
                (account["id"],))).fetchone() == (ACTIVE_GRANT_LIMIT,)
        replacement = await _mint(pools, account["id"], replaces=grants[3].grant_id)
        assert replacement is not None
        assert (await _grant(owner, grants[3].grant_id))[0] is not None
        for issued in grants[:3] + grants[4:] + [theirs, replacement]:
            assert (await _grant(owner, issued.grant_id))[0] is None
            assert await _authenticate(pools, issued.access_token) is not None
        # A replaces id that names nothing active does not make room.
        with pytest.raises(GrantLimitReached):
            await _mint(pools, account["id"], replaces=grants[3].grant_id)
        # A stale auth_version cannot mint.
        assert await _mint(pools, member, auth_version=2) is None
    _run(check)


def test_grants_are_uncharged_and_an_over_allowance_account_can_still_sign_in():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        async with owner.connection() as conn:
            before = await (await conn.execute(
                "SELECT account_id, actual_bytes, reserved_bytes, raw_bytes, enhancement_bytes "
                "FROM account_usage ORDER BY account_id")).fetchall()
            await conn.execute(
                "UPDATE storage_grants SET limit_mode='custom', account_limit_bytes=1, "
                "raw_limit_bytes=1, enhancement_limit_bytes=1 WHERE account_id=%s", (member,))
            usage = (await (await conn.execute(
                "SELECT actual_bytes FROM account_usage WHERE account_id=%s", (member,))).fetchone())[0]
            assert usage > 1
        issued = await _mint(pools, member)
        rotated = (await _refresh(pools, issued.refresh_token)).tokens
        assert await _authenticate(pools, rotated.access_token) is not None
        await _mint(pools, account["id"])
        async with pools.control.connection() as conn:
            await revoke_grant(conn, member, issued.grant_id)
        async with owner.connection() as conn:
            after = await (await conn.execute(
                "SELECT account_id, actual_bytes, reserved_bytes, raw_bytes, enhancement_bytes "
                "FROM account_usage ORDER BY account_id")).fetchall()
            assert after == before
            assert await (await conn.execute("SELECT public.storage_usage_consistent()")).fetchone() == (True,)
            assert await (await conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid='public.api_grants'::regclass "
                "AND NOT tgisinternal")).fetchone() == (0,)
    _run(check)


def test_device_labels_are_stored_normalized():
    async def check(owner, pools, account):
        cases = {
            "<script>alert(1)</script>": "<script>alert(1)</script>",
            "Kid\u0000's\u200b phone\u202e\n": "Kid's phone",
            "Cafe\u0301": "Caf\u00e9",
            "p" * 200: "p" * 64,
            " \u2028\u0007 ": DEFAULT_DEVICE_LABEL,
        }
        for raw, stored in cases.items():
            issued = await _mint(pools, account["id"], label=raw)
            assert (await _grant(owner, issued.grant_id))[5] == stored
    _run(check)


def test_runtime_cannot_read_grants_and_account_deletion_cascades():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        await _mint(pools, member)
        await _mint(pools, account["id"])
        async with pools.runtime.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute("SELECT * FROM api_grants")
        async with pools.control.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute("SELECT * FROM api_grants")
        async with owner.connection() as conn:
            await conn.execute("DELETE FROM account_settings WHERE account_id=%s", (member,))
            await conn.execute("DELETE FROM accounts WHERE id=%s", (member,))
            assert await (await conn.execute(
                "SELECT account_id FROM api_grants")).fetchall() == [(account["id"],)]
    _run(check)


def test_me_and_token_routes_end_to_end():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        async with _client(_app(pools)) as client:
            me = await client.get("/api/v1/me", headers=_bearer(issued.access_token))
            assert me.status_code == 200
            assert me.json() == {
                "account_id": account["id"], "display_name": None, "display_time_zone": "UTC",
                "server_version": auth_config(TEST_DB).app_version, "detector_version": DETECTOR_VERSION,
                "grant_id": issued.grant_id,
            }
            assert "set-cookie" not in me.headers
            refreshed = await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": issued.refresh_token})
            assert refreshed.status_code == 200
            body = refreshed.json()
            assert set(body) == {"access_token", "refresh_token", "token_type", "access_expires_at", "expires_in"}
            assert body["token_type"] == "Bearer" and body["expires_in"] == ACCESS_TOKEN_SECONDS
            assert body["access_expires_at"].endswith("Z") and "." in body["access_expires_at"]
            assert refreshed.headers["cache-control"] == "no-store"
            assert token_digest(body["access_token"]) and token_digest(body["refresh_token"])
            again = await client.get("/api/v1/me", headers=_bearer(body["access_token"]))
            assert again.json()["grant_id"] == issued.grant_id
            refused = await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": secrets.token_urlsafe(32)})
            assert refused.status_code == 400
            assert refused.json() == {"error": {"code": "invalid_grant",
                                                "message": ERRORS["invalid_grant"][1], "fields": None}}
            # A browser session for the same account never authenticates the API.
            await client.post(f"/test/session/{account['id']}/1")
            assert (await client.get("/api/v1/me")).status_code == 401
    _run(check)


def test_outdated_grant_tokens_get_the_uniform_401_without_counting_as_guesses():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        expired = await _mint(pools, account["id"])
        revoked = await _mint(pools, account["id"])
        idle = await _mint(pools, account["id"])
        disabled = await _mint(pools, member)
        rotated = await _mint(pools, account["id"])
        await _sql(owner, "UPDATE api_grants SET access_expires_at=now()-interval '1 second' "
                          "WHERE public_id=%s", (expired.grant_id,))
        async with pools.control.connection() as conn:
            await revoke_grant(conn, account["id"], revoked.grant_id)
        await _sql(owner, "UPDATE api_grants SET created_at=now()-interval '61 days' "
                          "WHERE public_id=%s", (idle.grant_id,))
        await _sql(owner, "UPDATE accounts SET is_enabled=false WHERE id=%s", (member,))
        # A previous-generation access token past its own expiry.
        (await _refresh(pools, rotated.refresh_token)).tokens
        await _sql(owner, "UPDATE api_grants SET previous_access_expires_at=now()-interval '1 second' "
                          "WHERE public_id=%s", (rotated.grant_id,))
        outdated = [expired.access_token, revoked.access_token, idle.access_token,
                    disabled.access_token, rotated.access_token]
        limiter = FailedAuthLimiter(3, 900)
        async with _client(_app(pools, limiter)) as client:
            responses = []
            for _ in range(4):
                for token in outdated:
                    responses.append(await client.get("/api/v1/me", headers=_bearer(token)))
            assert {response.status_code for response in responses} == {401}
            refreshed = await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": expired.refresh_token})
            assert refreshed.status_code == 200
            responses.append(await client.get("/api/v1/me", headers=_bearer(secrets.token_urlsafe(32))))
            responses.append(await client.get("/api/v1/me"))
            for response in responses:
                assert response.status_code == 401
                assert response.content == responses[0].content
                assert response.headers["www-authenticate"] == "Bearer"
                assert "retry-after" not in response.headers
    _run(check)


def test_stale_version_tokens_do_not_count_as_guesses():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        async with pools.control.connection() as conn:
            assert await sign_out_everywhere(conn, account["id"], expected_auth_version=1)
        async with _client(_app(pools, FailedAuthLimiter(2, 900))) as client:
            for _ in range(5):
                response = await client.get("/api/v1/me", headers=_bearer(issued.access_token))
                assert response.status_code == 401
    _run(check)


def test_unknown_tokens_are_rate_limited_with_retry_after():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        async with _client(_app(pools, FailedAuthLimiter(3, 900))) as client:
            failures = [await client.get("/api/v1/me", headers=_bearer(secrets.token_urlsafe(32)))
                        for _ in range(3)]
            assert all(response.status_code == 401 for response in failures)
            assert len({response.content for response in failures}) == 1
            limited = await client.get("/api/v1/me", headers=_bearer(issued.access_token))
            assert limited.status_code == 429
            assert limited.headers["retry-after"] == "900"
            assert limited.json()["error"]["code"] == "rate_limited"
            token = await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": issued.refresh_token})
            assert token.status_code == 429 and token.headers["retry-after"] == "900"
        async with _client(_app(pools, FailedAuthLimiter(2, 900))) as client:
            # Refresh invalid_grant still counts.
            for _ in range(2):
                assert (await client.post("/api/v1/auth/token", json={
                    "grant_type": "refresh_token", "refresh_token": secrets.token_urlsafe(32)})).status_code == 400
            assert (await client.get("/api/v1/me", headers=_bearer(issued.access_token))).status_code == 429
        # Rate limiting revoked nothing.
        assert (await _grant(owner, issued.grant_id))[0] is None
    _run(check)


def test_access_check_tells_outdated_tokens_from_unknown_ones():
    async def check(owner, pools, account):
        issued = await _mint(pools, account["id"])
        async with pools.control.connection() as conn:
            assert (await check_access_token(conn, issued.access_token)).principal is not None
            unknown = await check_access_token(conn, secrets.token_urlsafe(32))
            assert unknown.principal is None and not unknown.known
            malformed = await check_access_token(conn, "short")
            assert malformed.principal is None and not malformed.known
            await revoke_grant(conn, account["id"], issued.grant_id)
            revoked = await check_access_token(conn, issued.access_token)
            assert revoked.principal is None and revoked.known
    _run(check)


def test_auth_version_bump_frees_the_grant_limit_and_stale_grants_are_deleted():
    async def check(owner, pools, account):
        stale = [await _mint(pools, account["id"]) for _ in range(ACTIVE_GRANT_LIMIT)]
        async with pools.control.connection() as conn:
            assert await sign_out_everywhere(conn, account["id"], expected_auth_version=1)
        fresh = [await _mint(pools, account["id"], 2) for _ in range(ACTIVE_GRANT_LIMIT)]
        assert all(fresh)
        async with owner.connection() as conn:
            remaining = {row[0] for row in await (await conn.execute(
                "SELECT public_id FROM api_grants")).fetchall()}
        assert remaining == {issued.grant_id for issued in fresh}
        # A replaces id naming a stale grant is not one of this account's
        # active grants, so it makes no room.
        with pytest.raises(GrantLimitReached):
            await _mint(pools, account["id"], 2, replaces=stale[0].grant_id)
        for issued in fresh:
            assert await _authenticate(pools, issued.access_token) is not None
    _run(check)


def test_replaces_naming_a_stale_version_grant_is_ignored_below_the_limit():
    async def check(owner, pools, account):
        stale = await _mint(pools, account["id"])
        async with pools.control.connection() as conn:
            assert await sign_out_everywhere(conn, account["id"], expected_auth_version=1)
        kept = await _mint(pools, account["id"], 2)
        issued = await _mint(pools, account["id"], 2, replaces=stale.grant_id)
        assert issued is not None
        assert (await _grant(owner, kept.grant_id))[0] is None
        assert await _grant(owner, stale.grant_id) is None
    _run(check)


def test_null_account_or_version_never_mints():
    async def check(owner, pools, account):
        digest_a, digest_b = token_digest(secrets.token_urlsafe(32)), token_digest(secrets.token_urlsafe(32))
        async with pools.control.connection() as conn:
            for account_id, version in ((account["id"], None), (None, 1), (None, None)):
                row = await (await conn.execute(
                    "SELECT result FROM public.api_grant_create(%s,%s,%s,%s,%s,%s,%s)",
                    (account_id, version, secrets.token_urlsafe(16), "Phone", digest_a, digest_b, None),
                )).fetchone()
                assert row == ("unavailable",)
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM api_grants")).fetchone() == (0,)
    _run(check)


def test_admin_purge_removes_the_accounts_grants():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        reused = await _mint(pools, member)
        kept = await _mint(pools, account["id"])
        rotated = (await _refresh(pools, reused.refresh_token)).tokens
        assert await _authenticate(pools, rotated.access_token) is not None
        assert (await _refresh(pools, reused.refresh_token)).reuse_detected
        await _mint(pools, member, label="Tablet")
        async with pools.control.connection() as conn:
            actor = dict(await get_account(conn, account["id"]))
            assert await request_account_deletion(
                conn, actor, member, email=MEMBER_EMAIL, acknowledge=True) == "scheduled"
        await _sql(owner, "UPDATE accounts SET deletion_deadline=clock_timestamp()-interval '1 second' "
                          "WHERE id=%s", (member,))
        async with pools.control.connection() as conn:
            actor = dict(await get_account(conn, account["id"]))
            assert await purge_account(conn, actor, member, email=MEMBER_EMAIL, confirm=True,
                                       verified_password_hash=actor["password_hash"]) == "purged"
        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT id FROM accounts WHERE id=%s", (member,))).fetchone() is None
            assert await (await conn.execute(
                "SELECT public_id FROM api_grants")).fetchall() == [(kept.grant_id,)]
            actions = [row[0] for row in await (await conn.execute(
                "SELECT action FROM account_security_audit WHERE target_account_id=%s ORDER BY id",
                (member,))).fetchall()]
        assert actions[0] == "api_grant_refresh_reuse" and actions[-1] == "purge_account"
        assert await _authenticate(pools, kept.access_token) is not None
    _run(check)


def test_every_auth_version_change_ends_api_access():
    async def check(owner, pools, account):
        member = await _add_member(owner)
        account_id = account["id"]
        await _sql(owner, "UPDATE accounts SET email_verified_at=now() WHERE id=%s", (account_id,))

        async def age_challenges():
            await _sql(owner, "UPDATE email_challenges SET created_at=created_at-interval '2 minutes'")

        async def sign_out(conn, version):
            assert await sign_out_everywhere(conn, account_id, expected_auth_version=version)

        async def change_password(conn, version):
            assert await replace_password(conn, account_id, "hash-changed", expected_auth_version=version)

        async def change_email(conn, version):
            await age_challenges()
            token = await issue_email_challenge(conn, account_id, version, PURPOSE_CHANGE, "changed@example.invalid")
            assert token and await consume_email_challenge(conn, account_id, version, PURPOSE_CHANGE, token)

        async def reset_password(conn, version):
            await age_challenges()
            await _sql(owner, "UPDATE accounts SET email_verified_at=now() WHERE id=%s", (account_id,))
            current = await get_account(conn, account_id)
            token = secrets.token_urlsafe(32)
            assert await issue_password_reset(conn, token, initiator=INITIATOR_PUBLIC, email=current["email"])
            assert await consume_password_reset(conn, token, "hash-reset") == account_id

        async def link(conn, version):
            assert await create_identity_link(conn, account_id, ISSUER, "subject-1",
                                              expected_auth_version=version)

        async def unlink(conn, version):
            assert await unlink_identity(conn, account_id, ISSUER, "subject-1", expected_auth_version=version)

        app = _app(pools)
        async with _client(app) as client:
            for bump in (sign_out, change_password, change_email, reset_password, link, unlink):
                async with pools.control.connection() as conn:
                    version = (await get_account(conn, account_id))["auth_version"]
                other = await _mint(pools, account_id, version, label="Other")
                issued = await _mint(pools, account_id, version)
                assert (await client.get("/api/v1/me", headers=_bearer(issued.access_token))).status_code == 200
                async with pools.control.connection() as conn:
                    await bump(conn, version)
                    assert (await get_account(conn, account_id))["auth_version"] == version + 1, bump.__name__
                for grant in (issued, other):
                    response = await client.get("/api/v1/me", headers=_bearer(grant.access_token))
                    assert response.status_code == 401, bump.__name__
                    assert (await _refresh(pools, grant.refresh_token)).tokens is None
                async with pools.control.connection() as conn:
                    await revoke_grant(conn, account_id, issued.grant_id)
                    await revoke_grant(conn, account_id, other.grant_id)
            issued = await _mint(pools, member)
            assert (await client.get("/api/v1/me", headers=_bearer(issued.access_token))).status_code == 200
            async with pools.control.connection() as conn:
                admin = dict(await get_account(conn, account_id))
                assert await set_account_enabled(conn, admin, member, enable=False) == "disabled"
            assert (await client.get("/api/v1/me", headers=_bearer(issued.access_token))).status_code == 401
            assert (await _refresh(pools, issued.refresh_token)).tokens is None
    _run(check)


def test_logs_responses_and_audit_rows_never_contain_secrets(caplog):
    caplog.set_level(logging.DEBUG)

    async def check(owner, pools, account):
        secrets_seen = []

        def remember(issued):
            secrets_seen.extend([issued.access_token, issued.refresh_token,
                                 token_digest(issued.access_token), token_digest(issued.refresh_token)])
            return issued

        issued = remember(await _mint(pools, account["id"]))
        bodies = []
        async with _client(_app(pools)) as client:
            bodies.append(await client.get("/api/v1/me", headers=_bearer(issued.access_token)))
            refreshed = await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": issued.refresh_token})
            new = refreshed.json()
            secrets_seen.extend([new["access_token"], new["refresh_token"],
                                 token_digest(new["access_token"]), token_digest(new["refresh_token"])])
            bodies.append(await client.get("/api/v1/me", headers=_bearer(new["access_token"])))
            # Reuse: the previous refresh token after the new pair was used.
            bodies.append(await client.post("/api/v1/auth/token", json={
                "grant_type": "refresh_token", "refresh_token": issued.refresh_token}))
            bodies.append(await client.get("/api/v1/me", headers=_bearer(new["access_token"])))
            bodies.append(await client.post("/api/v1/auth/token", json={
                "grant_type": "password", "refresh_token": issued.refresh_token}))
        assert [response.status_code for response in bodies] == [200, 200, 400, 401, 422]
        audit = await _audit_rows(owner)
        assert audit == [(account["id"], account["id"], "api_grant_refresh_reuse", "revoked")]
        haystacks = [caplog.text, repr(audit)] + [response.text + repr(dict(response.headers))
                                                   for response in bodies]
        for secret in secrets_seen:
            for haystack in haystacks:
                assert secret not in haystack
        assert f"api grant {issued.grant_id} revoked: refresh token reuse" in caplog.text
    _run(check)
