"""Signed-in apps on the account security page against PostGIS: the active
list, owner-only revocation and the guarded list function."""
from __future__ import annotations

import asyncio
import html
import os
import re

import pytest
from psycopg import errors

from app.account_context import control_connection
from app.accounts import create_admin, get_account
from app.api_grants import create_grant, list_active_grants, token_digest
from app.db import make_pool
from conftest import provisioned_role_pools, reset_db
from test_mobile_signin_db import (
    LOCAL_EMAIL, _csrf, _login_local, _login_oidc, _role_pools, _site,
)

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

PAGE = "/settings/account"
REVOKE = "/settings/account/signed-in-apps/revoke"
NOTICE = "Signed-in apps updated."


def _labels(page) -> list[str]:
    return [html.unescape(label) for label in
            re.findall(r'class="signed-in-app-label">([^<]*)<', page.text)]


def _grant_ids(page) -> list[str]:
    return re.findall(r'name="grant_id" value="([^"]+)"', page.text)


def _bearer(tokens):
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def _revoke(client, grant_id, csrf):
    return await client.post(REVOKE, data={"grant_id": grant_id, "csrf_token": csrf})


def test_list_shows_exactly_the_signed_in_accounts_active_grants(monkeypatch):
    async def check(site):
        active = await site.mint(site.local["id"], "Active phone")
        used = await site.mint(site.local["id"], "Used phone")
        await site.mint(site.local["id"], "Revoked phone")
        await site.mint(site.local["id"], "Idle phone")
        await site.mint(site.local["id"], "Stale phone")
        await site.mint(site.member["id"], "Someone else's phone")
        await site.sql("UPDATE api_grants SET revoked_at=now() WHERE device_label='Revoked phone'")
        await site.sql("UPDATE api_grants SET created_at=now()-interval '61 days' "
                       "WHERE device_label='Idle phone'")
        await site.sql("UPDATE api_grants SET issued_auth_version=issued_auth_version+1 "
                       "WHERE device_label='Stale phone'")
        await site.sql("UPDATE api_grants SET last_used_at=now()-interval '3 days', "
                       "created_at=now()-interval '70 days' WHERE device_label='Used phone'")
        async with site.client() as client:
            await _login_local(client)
            page = await client.get(PAGE)
            assert page.status_code == 200
            assert _labels(page) == ["Active phone", "Used phone"]
            assert set(_grant_ids(page)) == {active.grant_id, used.grant_id}
            assert page.text.count("Never used") == 1 and page.text.count("Last used") == 1
            assert "Someone else" not in page.text
            assert (await site.sql("SELECT count(*) FROM api_grants")) == [(6,)]
    _site(monkeypatch, check)


def test_empty_state_and_list_with_mobile_signin_off(monkeypatch):
    async def check(site):
        async with site.client() as client:
            await _login_local(client)
            page = await client.get(PAGE)
            assert "No apps are signed in to this account." in page.text and not _labels(page)
            await site.mint(site.local["id"], "Phone")
            page = await client.get(PAGE)
            assert _labels(page) == ["Phone"] and 'action="' + REVOKE + '"' in page.text
    _site(monkeypatch, check, mobile_signin=False)


def test_revoking_through_the_page_ends_only_that_grant(monkeypatch):
    async def check(site):
        first = await site.mint(site.local["id"], "First")
        second = await site.mint(site.local["id"], "Second")
        async with site.client() as client:
            await _login_local(client)
            for tokens in (first, second):
                assert (await client.get("/api/v1/me", headers=_bearer(tokens))).status_code == 200
            page = await client.get(PAGE)
            done = await _revoke(client, first.grant_id, _csrf(page))
            assert done.status_code == 303 and done.headers["location"] == PAGE
            assert (await client.get("/api/v1/me", headers=_bearer(first))).status_code == 401
            assert (await client.get("/api/v1/me", headers=_bearer(second))).status_code == 200
            page = await client.get(PAGE)
            assert NOTICE in page.text and _labels(page) == ["Second"]
            assert (await site.sql("SELECT count(*) FROM api_grants WHERE revoked_at IS NOT NULL")) == [(1,)]
    _site(monkeypatch, check)


def test_foreign_revoked_and_malformed_ids_change_nothing_and_look_alike(monkeypatch):
    async def check(site):
        mine = await site.mint(site.local["id"], "Mine")
        gone = await site.mint(site.local["id"], "Gone")
        foreign = await site.mint(site.member["id"], "Foreign")
        await site.sql("UPDATE api_grants SET revoked_at=now() WHERE public_id=%s", (gone.grant_id,))
        before = await site.sql("SELECT public_id, revoked_at FROM api_grants ORDER BY id")
        async with site.client() as client:
            await _login_local(client)
            replies = []
            for value in (foreign.grant_id, gone.grant_id, "short", "!" * 22, "", "A" * 22, "x" * 500):
                page = await client.get(PAGE)
                reply = await _revoke(client, value, _csrf(page))
                assert reply.status_code == 303 and reply.headers["location"] == PAGE
                replies.append((reply.status_code, reply.headers["location"], reply.text))
                assert NOTICE in (await client.get(PAGE)).text
            assert len(set(replies)) == 1
            missing = await client.post(REVOKE, data={"csrf_token": _csrf(await client.get(PAGE))})
            assert missing.status_code == 303
            assert (await site.sql("SELECT public_id, revoked_at FROM api_grants ORDER BY id")) == before
            assert (await client.get("/api/v1/me", headers=_bearer(mine))).status_code == 200
            assert (await client.get("/api/v1/me", headers=_bearer(foreign))).status_code == 200
    _site(monkeypatch, check)


def test_revoking_requires_a_session_and_csrf(monkeypatch):
    async def check(site):
        tokens = await site.mint(site.local["id"], "Phone")
        async with site.client() as client:
            login = await client.get("/login")
            anonymous = await _revoke(client, tokens.grant_id, _csrf(login))
            assert anonymous.status_code == 303 and anonymous.headers["location"] == "/login"
            await _login_local(client)
            page = await client.get(PAGE)
            forged = await _revoke(client, tokens.grant_id, "forged")
            assert forged.status_code == 403
            absent = await client.post(REVOKE, data={"grant_id": tokens.grant_id})
            assert absent.status_code in (403, 422)
            assert (await client.get("/api/v1/me", headers=_bearer(tokens))).status_code == 200
            assert (await site.sql("SELECT count(revoked_at) FROM api_grants")) == [(0,)]
            assert _csrf(page)
    _site(monkeypatch, check)


def test_another_account_cannot_revoke_through_its_own_session(monkeypatch):
    async def check(site):
        tokens = await site.mint(site.local["id"], "Owner phone")
        async with site.client() as client:
            await _login_oidc(site, client)
            page = await client.get(PAGE)
            assert not _labels(page)
            reply = await _revoke(client, tokens.grant_id, _csrf(page))
            assert reply.status_code == 303
            assert (await client.get("/api/v1/me", headers=_bearer(tokens))).status_code == 200
    _site(monkeypatch, check)


def test_labels_with_markup_render_escaped_and_no_digest_reaches_the_page(monkeypatch):
    async def check(site):
        tokens = await site.mint(site.local["id"], "<img src=x onerror=alert(1)>")
        async with site.client() as client:
            await _login_local(client)
            page = await client.get(PAGE)
            assert "<img src=x" not in page.text
            assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
            for secret in (tokens.access_token, tokens.refresh_token,
                           token_digest(tokens.access_token), token_digest(tokens.refresh_token)):
                assert secret not in page.text
    _site(monkeypatch, check)


def test_an_auth_version_change_empties_the_list(monkeypatch):
    async def check(site):
        await site.mint(site.local["id"], "Phone")
        async with site.client() as client:
            await _login_local(client)
            assert _labels(await client.get(PAGE)) == ["Phone"]
            await site.sql("UPDATE accounts SET auth_version=auth_version+1 WHERE id=%s", (site.local["id"],))
            async with control_connection(site.app.state.control_pool) as conn:
                assert await list_active_grants(conn, site.local["id"]) == []
    _site(monkeypatch, check)


def test_list_function_returns_display_fields_only_and_only_through_control():
    async def scenario():
        async with _role_pools() as (owner, pools):
            async with pools.control.connection() as conn:
                account_id = (await create_admin(conn, LOCAL_EMAIL, "unused"))["id"]
                version = (await get_account(conn, account_id))["auth_version"]
                issued = await create_grant(conn, account_id, version, "Phone")
                apps = await list_active_grants(conn, account_id)
                assert [(a.public_id, a.device_label, a.last_used_at) for a in apps] == [
                    (issued.grant_id, "Phone", None)]
                assert await list_active_grants(conn, account_id + 1000) == []
                cur = await conn.execute("SELECT * FROM public.api_grant_list(%s)", (account_id,))
                assert [column.name for column in cur.description] == [
                    "result_public_id", "result_device_label", "result_created_at", "result_last_used_at"]
            async with owner.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT prosecdef, pg_get_userbyid(proowner) FROM pg_proc "
                    "WHERE oid='public.api_grant_list(bigint)'::regprocedure")).fetchone()) == (
                        True, "odograph_migrate")
            async with pools.runtime.connection() as conn:
                with pytest.raises(errors.InsufficientPrivilege):
                    await conn.execute("SELECT * FROM public.api_grant_list(%s)", (account_id,))
    asyncio.run(scenario())
