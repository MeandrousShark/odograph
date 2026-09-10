from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors
from psycopg.rows import dict_row

import app.auth as auth_module
from app.auth import AuthRedirect, make_router, require_admin, require_user
from app.db import MIGRATIONS_DIR, make_pool
from conftest import drop_and_recreate_schema, full_schema_reset, reset_db
from app.ingest import FailedAuthLimiter
from app.local_auth import hash_password, verify_password
from app.main import make_templates

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)
TZ = ZoneInfo("UTC")


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route missing")


def _request(
    pool, *, signup=True, session=None, ip="203.0.113.5", limiter=None
):
    cfg = SimpleNamespace(
        initial_admin_signup=signup,
        dev_no_auth=False,
        allowed_email="",
        oidc_issuer="",
    )
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                pool=pool,
                config=cfg,
                templates=make_templates(
                    SimpleNamespace(display_tz=TZ, app_version="test")
                ),
                oauth=None,
                login_limiter=limiter or FailedAuthLimiter(5, 900.0),
            )
        ),
        session=session if session is not None else {"csrf": "test-csrf"},
        client=SimpleNamespace(host=ip),
    )


async def _account(pool):
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT * FROM accounts")
        return await cur.fetchone()


async def _migration_shape_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts (email, password_hash) VALUES ('a@example.com', 'hash')"
            )
        async with pool.connection() as conn:
            with pytest.raises(errors.UniqueViolation):
                await conn.execute(
                    "INSERT INTO accounts (email, password_hash) "
                    "VALUES ('b@example.com', 'hash')"
                )
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO accounts (email, password_hash, is_admin) "
                    "VALUES ('c@example.com', 'hash', false)"
                )
    finally:
        await pool.close()


def test_accounts_schema_enforces_single_administrator():
    asyncio.run(_migration_shape_scenario())


async def _migration_preserves_local_admin_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    old_hash = hash_password("old password")
    try:
        await drop_and_recreate_schema(pool)
        try:
            paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
            async with pool.connection() as conn:
                for path in paths:
                    if int(path.name.split("_", 1)[0]) >= 20:
                        break
                    await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO local_admin "
                    "(id, email, password_hash, consumed_token_hash) "
                    "VALUES (1, 'admin@example.com', %s, 'obsolete')",
                    (old_hash,),
                )
                await conn.execute((MIGRATIONS_DIR / "020_accounts.sql").read_text())

            account = await _account(pool)
            assert account["id"] == 1
            assert account["email"] == "admin@example.com"
            assert account["password_hash"] == old_hash
            assert verify_password("old password", account["password_hash"])

            # The login endpoint below is HEAD application code, not the
            # historical migration 020 -- it queries whatever columns
            # get_sole_account currently selects, so the rest of the
            # migrations (021+) have to be applied too before it can run,
            # even though only 020's data-preserving behavior is under test
            # above.
            async with pool.connection() as conn:
                for path in paths:
                    if int(path.name.split("_", 1)[0]) < 21:
                        continue
                    await conn.execute(path.read_text())

            login_request = _request(pool, signup=False)
            login_response = await _endpoint("/login/local", "POST")(
                login_request,
                email="admin@example.com",
                password="old password",
                csrf_token="test-csrf",
            )
            assert login_response.status_code == 303
            assert login_request.session["account_id"] == 1
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT to_regclass('local_admin')")
                assert (await cur.fetchone())[0] is None
        finally:
            # The loops above apply every migration by executing the files
            # directly, which bypasses the runner: schema_migrations is
            # never populated. Correctness must not depend on collection
            # order, so restore canonical, fully-migrated state before any
            # other test can see this one, regardless of whether the
            # assertions above passed.
            await full_schema_reset(pool)
    finally:
        await pool.close()


def test_migration_preserves_existing_local_admin_credentials():
    asyncio.run(_migration_preserves_local_admin_scenario())


async def _signup_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        get_signup = _endpoint("/signup", "GET")
        post_signup = _endpoint("/signup", "POST")
        assert (await get_signup(_request(pool))).status_code == 200

        request = _request(pool)
        response = await post_signup(
            request,
            email=" Admin@Example.COM ",
            password="correct horse battery",
            password_confirm="correct horse battery",
            csrf_token="test-csrf",
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert request.session["account_id"] == 1
        account = await _account(pool)
        assert account["email"] == "admin@example.com"

        with pytest.raises(Exception) as get_closed:
            await get_signup(_request(pool))
        assert get_closed.value.status_code == 404
        with pytest.raises(Exception) as post_closed:
            await post_signup(
                _request(pool),
                email="other@example.com",
                password="another password",
                password_confirm="another password",
                csrf_token="test-csrf",
            )
        assert post_closed.value.status_code == 404
    finally:
        await pool.close()


def test_signup_creates_account_and_permanently_closes_both_routes():
    asyncio.run(_signup_scenario())


async def _signup_disabled_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        for method in ("GET", "POST"):
            endpoint = _endpoint("/signup", method)
            with pytest.raises(Exception) as exc_info:
                if method == "GET":
                    await endpoint(_request(pool, signup=False))
                else:
                    await endpoint(
                        _request(pool, signup=False),
                        email="admin@example.com",
                        password="password one",
                        password_confirm="password one",
                        csrf_token="test-csrf",
                    )
            assert exc_info.value.status_code == 404
    finally:
        await pool.close()


def test_signup_is_fail_closed_when_setting_is_disabled():
    asyncio.run(_signup_disabled_scenario())


async def _signup_csrf_and_limiter_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        endpoint = _endpoint("/signup", "POST")
        with pytest.raises(Exception) as csrf_error:
            await endpoint(
                _request(pool),
                email="admin@example.com",
                password="password one",
                password_confirm="password one",
                csrf_token="wrong",
            )
        assert csrf_error.value.status_code == 403

        limiter = FailedAuthLimiter(1, 900.0)
        request = _request(pool, limiter=limiter)
        invalid = await endpoint(
            request,
            email="admin@example.com",
            password="password one",
            password_confirm="password two",
            csrf_token="test-csrf",
        )
        assert invalid.status_code == 400
        assert limiter.blocked(request.client.host)

        blocked = await endpoint(
            _request(pool, limiter=limiter),
            email="admin@example.com",
            password="password one",
            password_confirm="password one",
            csrf_token="test-csrf",
        )
        assert blocked.status_code == 429
        assert await _account(pool) is None
    finally:
        await pool.close()


def test_signup_requires_csrf_and_uses_the_login_failure_limiter():
    asyncio.run(_signup_csrf_and_limiter_scenario())


async def _signup_invalid_email_scenario(email):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        response = await _endpoint("/signup", "POST")(
            _request(pool),
            email=email,
            password="password one",
            password_confirm="password one",
            csrf_token="test-csrf",
        )
        assert response.status_code == 400
        assert b"valid ASCII email address" in response.body
        assert await _account(pool) is None
    finally:
        await pool.close()


@pytest.mark.parametrize("email", ["", "   ", "adm\u00edn@example.com"])
def test_signup_rejects_empty_and_non_ascii_email(email):
    asyncio.run(_signup_invalid_email_scenario(email))


async def _concurrent_signup_scenario(monkeypatch):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        original = auth_module._signup_available
        ready = 0
        release = asyncio.Event()

        async def synchronized(request):
            nonlocal ready
            available = await original(request)
            ready += 1
            if ready == 2:
                release.set()
            await release.wait()
            return available

        monkeypatch.setattr(auth_module, "_signup_available", synchronized)
        endpoint = _endpoint("/signup", "POST")
        requests = [_request(pool, ip=f"203.0.113.{i}") for i in (10, 11)]
        responses = await asyncio.gather(
            endpoint(
                requests[0],
                email="one@example.com",
                password="password one",
                password_confirm="password one",
                csrf_token="test-csrf",
            ),
            endpoint(
                requests[1],
                email="two@example.com",
                password="password two",
                password_confirm="password two",
                csrf_token="test-csrf",
            ),
        )
        assert sorted(response.status_code for response in responses) == [303, 409]
        loser = next(response for response in responses if response.status_code == 409)
        assert b"administrator account already exists" in loser.body
        assert b"Sign in" in loser.body
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM accounts")
            assert (await cur.fetchone())[0] == 1
    finally:
        await pool.close()


def test_concurrent_signup_creates_exactly_one_account(monkeypatch):
    asyncio.run(_concurrent_signup_scenario(monkeypatch))


async def _password_change_revokes_sessions_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        old_hash = hash_password("old password")
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts (id, email, password_hash) "
                "VALUES (1, 'admin@example.com', %s)",
                (old_hash,),
            )

        old_session = {"account_id": 1, "auth_version": 1, "csrf": "old-csrf"}
        current = _request(pool, session=dict(old_session))
        other = _request(pool, session=dict(old_session))
        endpoint = _endpoint("/settings/account/password", "POST")
        response = await endpoint(
            current,
            current_password="old password",
            password="new password",
            password_confirm="new password",
            csrf_token="old-csrf",
            user={"id": 1, "name": "admin", "is_admin": True},
        )
        assert response.status_code == 200
        assert current.session["auth_version"] == 2
        assert (await require_user(current))["id"] == 1
        with pytest.raises(AuthRedirect):
            await require_user(other)
        assert other.session == {}
        account = await _account(pool)
        assert verify_password("new password", account["password_hash"])
    finally:
        await pool.close()


def test_password_change_revokes_old_sessions_and_reissues_current_session():
    asyncio.run(_password_change_revokes_sessions_scenario())


async def _password_change_failure_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        password_hash = hash_password("old password")
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts (id, email, password_hash) "
                "VALUES (1, 'admin@example.com', %s)",
                (password_hash,),
            )
        endpoint = _endpoint("/settings/account/password", "POST")
        user = {"id": 1, "name": "admin", "is_admin": True}

        with pytest.raises(Exception) as csrf_error:
            await endpoint(
                _request(pool, session={"csrf": "real-csrf"}),
                current_password="old password",
                password="new password",
                password_confirm="new password",
                csrf_token="wrong",
                user=user,
            )
        assert csrf_error.value.status_code == 403

        limiter = FailedAuthLimiter(1, 900.0)
        request = _request(
            pool,
            limiter=limiter,
            session={"account_id": 1, "auth_version": 1, "csrf": "test-csrf"},
        )
        wrong = await endpoint(
            request,
            current_password="wrong password",
            password="new password",
            password_confirm="new password",
            csrf_token="test-csrf",
            user=user,
        )
        assert wrong.status_code == 401
        assert b"Unable to change password" in wrong.body
        assert password_hash.encode() not in wrong.body
        assert limiter.blocked(request.client.host)

        blocked = await endpoint(
            _request(
                pool,
                limiter=limiter,
                session={"account_id": 1, "auth_version": 1, "csrf": "test-csrf"},
            ),
            current_password="old password",
            password="new password",
            password_confirm="new password",
            csrf_token="test-csrf",
            user=user,
        )
        assert blocked.status_code == 429
        account = await _account(pool)
        assert account["auth_version"] == 1
        assert account["password_hash"] == password_hash
    finally:
        await pool.close()


def test_password_change_requires_csrf_and_limits_wrong_current_password():
    asyncio.run(_password_change_failure_scenario())


async def _legacy_session_boundary_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        session = {
            "legacy_oidc": {
                "issuer": "https://idp.example.com",
                "subject": "subject-1",
                "name": "Legacy Admin",
                "email": "admin@example.com",
            },
            "csrf": "test-csrf",
        }
        request = _request(pool, signup=False, session=session)
        request.app.state.oauth = object()
        request.app.state.config.oidc_issuer = "https://idp.example.com"

        legacy_user = await require_user(request)
        assert legacy_user["legacy_oidc"] is True
        with pytest.raises(Exception) as admin_denied:
            await require_admin(request)
        assert admin_denied.value.status_code == 403

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts (id, email, password_hash) "
                "VALUES (1, 'admin@example.com', 'hash')"
            )
        with pytest.raises(AuthRedirect):
            await require_user(request)
        assert request.session == {}
    finally:
        await pool.close()


def test_legacy_oidc_session_is_non_admin_and_closes_when_account_exists():
    asyncio.run(_legacy_session_boundary_scenario())


async def _pre_account_session_bridge_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        request = _request(
            pool,
            signup=False,
            session={
                "user": {
                    "sub": "subject-1",
                    "name": "Existing OIDC Admin",
                    "email": "admin@example.com",
                },
                "csrf": "old-csrf",
            },
        )
        request.app.state.oauth = object()
        request.app.state.config.oidc_issuer = "https://idp.example.com/"

        user = await require_user(request)
        assert user["legacy_oidc"] is True
        assert "user" not in request.session
        assert request.session["legacy_oidc"] == {
            "issuer": "https://idp.example.com",
            "subject": "subject-1",
            "name": "Existing OIDC Admin",
            "email": "admin@example.com",
        }
        assert request.session["csrf"] != "old-csrf"
    finally:
        await pool.close()


def test_pre_account_oidc_session_is_bridged_to_explicit_legacy_shape():
    asyncio.run(_pre_account_session_bridge_scenario())
