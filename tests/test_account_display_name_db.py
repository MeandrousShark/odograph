"""DB-backed tests for the optional account display name: its schema
constraints, the Account Settings write route, and where it renders."""
from __future__ import annotations

import asyncio
import os
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from psycopg import errors
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from tests.auth_db_fixtures import auth_config, bind_auth_test_roles, seed_auth_account
from app.account_context import AccountPrincipal
from app.accounts import get_account
from app.admin import make_router as make_admin_router
from app.auth import AuthRedirect, make_router
from app.db import make_pool
from app.main import make_templates
from conftest import add_test_account, reset_account_db, reset_db
from prepared_report_support import report_response

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
HEADER_RE = re.compile(r'<span class="who account-name">([^<]*)</span>')
INITIALS_RE = re.compile(r'<span class="account-avatar" aria-hidden="true">([^<]*)</span>')


def _scenario(coro_factory) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await reset_db(pool)
            await bind_auth_test_roles(pool)
            await coro_factory(pool)
        finally:
            await pool.close()

    asyncio.run(run())


async def _seed(pool, account_id: int = 1) -> None:
    if account_id == 1:
        async with pool.connection() as conn:
            await seed_auth_account(conn, owner_id=1, email="admin1@example.com")
    else:
        await add_test_account(pool, account_id, email=f"member{account_id}@example.com")


async def _set_name(pool, account_id: int, name: str | None) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE accounts SET display_name=%s WHERE id=%s", (name, account_id))


async def _account(pool, account_id: int = 1) -> dict:
    async with pool.control_pool.connection() as conn:
        return await get_account(conn, account_id)


def _bare_app(pool, *, admin: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool.runtime_pool
    app.state.control_pool = pool.control_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.dev_principal = AccountPrincipal(1, True, 1)
    app.state.make_detector_runner = lambda bound: SimpleNamespace(pool=bound)
    app.state.config = auth_config(TEST_DB,
        dev_no_auth=False, initial_admin_signup=False, allowed_email="",
        oidc_issuer="https://idp.example.com",
    )
    app.state.oauth = None
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request: Request, exc: AuthRedirect):
        return RedirectResponse("/login", status_code=303)

    @app.get("/test/login-as/{account_id}/{auth_version}")
    async def login_as(request: Request, account_id: int, auth_version: int):
        request.session["account_id"] = account_id
        request.session["auth_version"] = auth_version
        return Response(status_code=204)

    app.include_router(make_router())
    if admin:
        app.include_router(make_admin_router())
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        follow_redirects=False,
    )


async def _login(client, account_id: int = 1) -> str:
    await client.get(f"/test/login-as/{account_id}/1")
    page = await client.get("/settings/account")
    assert page.status_code == 200
    return CSRF_RE.search(page.text).group(1)


async def _post_name(client, csrf: str, value: str):
    return await client.post(
        "/settings/account/display-name", data={"csrf_token": csrf, "display_name": value}
    )


def test_existing_account_reads_null_and_check_rejects_unnormalized_values():
    async def run(pool):
        await _seed(pool)
        assert (await _account(pool))["display_name"] is None
        for bad in (" Ada", "Ada ", "", "a" * 101):
            async with pool.connection() as conn:
                with pytest.raises(errors.CheckViolation):
                    await conn.execute("UPDATE accounts SET display_name=%s WHERE id=1", (bad,))
        await _set_name(pool, 1, "a" * 100)
        assert (await _account(pool))["display_name"] == "a" * 100
        await _set_name(pool, 1, None)
        assert (await _account(pool))["display_name"] is None

    _scenario(run)


def test_control_role_may_update_only_the_display_name_column():
    async def run(pool):
        await _seed(pool)
        async with pool.control_pool.connection() as conn:
            await conn.execute("UPDATE accounts SET display_name='Ada' WHERE id=1")
        async with pool.control_pool.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute("UPDATE accounts SET email='x@example.com' WHERE id=1")
        async with pool.runtime_pool.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute("UPDATE accounts SET display_name='Eve' WHERE id=1")
        assert (await _account(pool))["display_name"] == "Ada"

    _scenario(run)


def test_set_trim_clear_and_session_state():
    async def run(pool):
        await _seed(pool)
        before = await _account(pool)
        app = _bare_app(pool)
        async with _client(app) as other, _client(app) as client:
            await _login(other)
            csrf = await _login(client)

            response = await _post_name(client, csrf, "  Ada Lovelace \t")
            assert response.status_code == 303
            assert response.headers["location"] == "/settings/account"
            assert (await _account(pool))["display_name"] == "Ada Lovelace"
            page = await client.get("/settings/account")
            assert 'role="status">Display name updated.</div>' in page.text
            assert HEADER_RE.search(page.text).group(1) == "Ada Lovelace"
            assert 'aria-label="Account Settings, Ada Lovelace"' in page.text
            assert INITIALS_RE.search(page.text).group(1) == "AL"
            assert '<span class="account-avatar account-security-avatar-placeholder" aria-hidden="true">AL</span>' in page.text
            assert 'name="display_name" value="Ada Lovelace" maxlength="100" autocomplete="name"' in page.text

            # Presentation metadata is not an authentication change.
            after = await _account(pool)
            assert after["auth_version"] == before["auth_version"]
            assert after["email"] == before["email"]
            assert after["password_hash"] == before["password_hash"]
            still_signed_in = await other.get("/settings/account")
            assert still_signed_in.status_code == 200
            assert HEADER_RE.search(still_signed_in.text).group(1) == "Ada Lovelace"

            for blank in ("", "   \t "):
                await _set_name(pool, 1, "Someone")
                response = await _post_name(client, csrf, blank)
                assert response.status_code == 303
                assert (await _account(pool))["display_name"] is None
            page = await client.get("/settings/account")
            assert 'role="status">Display name cleared.</div>' in page.text
            assert HEADER_RE.search(page.text).group(1) == "admin1"
            assert INITIALS_RE.search(page.text).group(1) == "A"
            assert 'name="display_name" value="" maxlength="100"' in page.text
            assert (await _account(pool))["auth_version"] == before["auth_version"]

    _scenario(run)


def test_length_limit_and_disallowed_characters_rerender_without_change():
    async def run(pool):
        await _seed(pool)
        await _set_name(pool, 1, "Keep Me")
        app = _bare_app(pool)
        async with _client(app) as client:
            csrf = await _login(client)
            response = await _post_name(client, csrf, " " + "b" * 100 + " ")
            assert response.status_code == 303
            assert (await _account(pool))["display_name"] == "b" * 100
            await _set_name(pool, 1, "Keep Me")

            response = await _post_name(client, csrf, "c" * 101)
            assert response.status_code == 422
            assert 'id="account-display-name-error">Keep it to 100 characters or fewer.</span>' in response.text
            assert 'aria-invalid="true"' in response.text
            assert (await _account(pool))["display_name"] == "Keep Me"

            for bad in ("Ada\x07", "Ada​Lovelace", "Ada‮ecalevol", "Ada", "A\x00b"):
                response = await _post_name(client, csrf, bad)
                assert response.status_code == 422, repr(bad)
                assert 'id="account-display-name-error">Remove control or invisible formatting characters.</span>' in response.text
                assert (await _account(pool))["display_name"] == "Keep Me"
            # The header still shows the stored name, not the rejected input.
            assert HEADER_RE.search(response.text).group(1) == "Keep Me"

    _scenario(run)


def test_unicode_names_are_stored_and_html_is_escaped():
    async def run(pool):
        await _seed(pool)
        app = _bare_app(pool)
        async with _client(app) as client:
            csrf = await _login(client)
            for value, label, initials in (
                ("Zoë Ångström", "Zoë Ångström", "ZÅ"),
                ("山田 太郎", "山田 太郎", "山太"),
                ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;", "&lt;"),
                ("ops@home", "ops@home", "O"),
                ("Ada @ Work", "Ada @ Work", "AW"),
                ("Cher", "Cher", "C"),
            ):
                response = await _post_name(client, csrf, value)
                assert response.status_code == 303
                assert (await _account(pool))["display_name"] == value
                page = await client.get("/settings/account")
                assert HEADER_RE.search(page.text).group(1) == label
                assert INITIALS_RE.search(page.text).group(1) == initials
                assert "<script>alert(1)" not in page.text

    _scenario(run)


def test_csrf_is_required_without_mutation():
    async def run(pool):
        await _seed(pool)
        app = _bare_app(pool)
        async with _client(app) as client:
            await _login(client)
            wrong = await _post_name(client, "wrong-token", "Mallory")
            assert wrong.status_code == 403
            missing = await client.post("/settings/account/display-name", data={"display_name": "Mallory"})
            assert missing.status_code in (403, 422)
            assert (await _account(pool))["display_name"] is None

        async with _client(app) as anonymous:
            response = await anonymous.post(
                "/settings/account/display-name", data={"csrf_token": "x", "display_name": "Mallory"})
            assert response.status_code in (303, 403)
            assert (await _account(pool))["display_name"] is None

    _scenario(run)


def test_one_accounts_name_never_renders_for_another_account():
    async def run(pool):
        await _seed(pool)
        await _seed(pool, 2)
        await _set_name(pool, 1, "Alice Admin")
        app = _bare_app(pool, admin=True)
        async with _client(app) as member, _client(app) as admin:
            await _login(admin, 1)
            csrf = await _login(member, 2)
            page = await member.get("/settings/account")
            assert "Alice Admin" not in page.text
            assert HEADER_RE.search(page.text).group(1) == "member2"

            assert (await _post_name(member, csrf, "Bob Member")).status_code == 303
            assert (await _account(pool, 1))["display_name"] == "Alice Admin"
            assert (await _account(pool, 2))["display_name"] == "Bob Member"
            member_page = await member.get("/settings/account")
            admin_page = await admin.get("/settings/account")
            assert "Alice Admin" not in member_page.text
            assert "Bob Member" not in admin_page.text
            assert HEADER_RE.search(member_page.text).group(1) == "Bob Member"
            assert HEADER_RE.search(admin_page.text).group(1) == "Alice Admin"

    _scenario(run)


def test_admin_list_shows_escaped_display_name_column():
    async def run(pool):
        await _seed(pool)
        await _seed(pool, 2)
        await _set_name(pool, 2, "<b>Bob</b>")
        app = _bare_app(pool, admin=True)
        async with _client(app) as admin:
            await _login(admin, 1)
            page = await admin.get("/admin/accounts")
            assert page.status_code == 200
            assert '<th scope="col" role="columnheader">Display name</th>' in page.text
            rows = page.text.split('<td role="cell" class="num" data-label="ID">')[1:]
            assert '<td role="cell" data-label="Display name"></td>' in rows[0]
            assert '<td role="cell" data-label="Display name">&lt;b&gt;Bob&lt;/b&gt;</td>' in rows[1]
            assert "<b>Bob</b>" not in page.text

    _scenario(run)


@pytest.mark.parametrize("path", ["/report/2026", "/report/range?from=2026-04-01&to=2026-06-30"])
def test_report_pages_use_the_display_name_with_email_fallback(path):
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool, email="reporter@example.invalid")
            other = await add_test_account(raw_pool, 42, email="other@example.invalid")
            async with raw_pool.connection() as conn:
                await conn.execute("UPDATE accounts SET display_name='Other Person' WHERE id=%s",
                                   (other.principal.account_id,))
            config = SimpleNamespace(display_tz=ZoneInfo("UTC"), app_version="test")

            fallback = (await report_response(pool, path, config)).body.decode()
            assert HEADER_RE.search(fallback).group(1) == "reporter"
            assert INITIALS_RE.search(fallback).group(1) == "R"
            assert "Other Person" not in fallback

            for value, label, initials in (
                ("Ada @ Work <x>", "Ada @ Work &lt;x&gt;", "A&lt;"),
                ("ops@home", "ops@home", "O"),
                ("山田 太郎", "山田 太郎", "山太"),
            ):
                async with raw_pool.connection() as conn:
                    await conn.execute("UPDATE accounts SET display_name=%s WHERE id=%s",
                                       (value, pool.principal.account_id))
                body = (await report_response(pool, path, config)).body.decode()
                assert HEADER_RE.search(body).group(1) == label
                assert f'aria-label="Account Settings, {label}"' in body
                assert INITIALS_RE.search(body).group(1) == initials
                assert "Other Person" not in body
        finally:
            await raw_pool.close()

    asyncio.run(run())
