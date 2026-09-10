"""DB-backed tests for account-aware local login."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.auth import make_router
from app.db import make_pool
from app.ingest import FailedAuthLimiter
from app.local_auth import hash_password
from app.main import make_templates
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("UTC")
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route missing")


async def _seed_admin(pool, *, email=ADMIN_EMAIL, password=ADMIN_PASSWORD, enabled=True):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO accounts (id, email, password_hash, is_enabled) "
            "VALUES (1, %s, %s, %s)",
            (email, hash_password(password), enabled),
        )


def _request(pool, *, ip="203.0.113.9", limiter=None, session=None):
    cfg = SimpleNamespace(
        initial_admin_signup=False, dev_no_auth=False, allowed_email=""
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=cfg,
            templates=make_templates(SimpleNamespace(display_tz=TZ, app_version="test")),
            oauth=None,
            login_limiter=limiter or FailedAuthLimiter(3, 900.0),
        )),
        session=session if session is not None else {"csrf": "pre-login-csrf", "junk": "left-over"},
        client=SimpleNamespace(host=ip),
    )


def _login_local(request, **overrides):
    fields = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": "pre-login-csrf"}
    fields.update(overrides)
    return _endpoint("/login/local", "POST")(request, **fields)


async def _success_and_fixation_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await _seed_admin(pool)

        request = _request(pool)
        response = await _login_local(request)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

        assert request.session["account_id"] == 1
        assert request.session["auth_version"] == 1
        # Fixation defense: the old pre-login session content is gone
        # (not just overwritten user/csrf keys), and the CSRF token issued
        # for the now-authenticated session differs from the pre-login one.
        assert "junk" not in request.session
        assert request.session["csrf"] != "pre-login-csrf"
    finally:
        await pool.close()


def test_login_success_sets_local_session_and_rotates_session_and_csrf():
    asyncio.run(_success_and_fixation_scenario())


async def _wrong_password_and_wrong_email_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await _seed_admin(pool)

        wrong_password_request = _request(pool)
        wrong_password_response = await _login_local(wrong_password_request, password="not it")
        assert wrong_password_response.status_code == 401
        assert b"Invalid email or password" in wrong_password_response.body
        assert "user" not in wrong_password_request.session

        wrong_email_request = _request(pool)
        wrong_email_response = await _login_local(wrong_email_request, email="nobody@example.com")
        assert wrong_email_response.status_code == 401
        # Same generic message for both failure reasons.
        assert wrong_email_response.body == wrong_password_response.body
    finally:
        await pool.close()


def test_login_failure_is_generic_for_wrong_password_or_wrong_email():
    asyncio.run(_wrong_password_and_wrong_email_scenario())


async def _bad_csrf_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await _seed_admin(pool)
        request = _request(pool)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await _login_local(request, csrf_token="wrong-csrf")
        assert exc_info.value.status_code == 403
    finally:
        await pool.close()


def test_login_local_rejects_mismatched_csrf_token():
    asyncio.run(_bad_csrf_scenario())


async def _limiter_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await _seed_admin(pool)
        limiter = FailedAuthLimiter(2, 900.0)

        for _ in range(2):
            request = _request(pool, limiter=limiter)
            response = await _login_local(request, password="wrong")
            assert response.status_code == 401

        blocked_request = _request(pool, limiter=limiter)
        blocked_response = await _login_local(blocked_request, password="wrong")
        assert blocked_response.status_code == 429

        # Blocked even with the correct password now -- per-IP, not
        # per-credential throttling.
        still_blocked_request = _request(pool, limiter=limiter)
        still_blocked_response = await _login_local(still_blocked_request)
        assert still_blocked_response.status_code == 429
        assert "user" not in still_blocked_request.session
    finally:
        await pool.close()


def test_login_local_limiter_throttles_repeated_failures_from_one_ip():
    asyncio.run(_limiter_scenario())


async def _non_ascii_email_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await _seed_admin(pool)
        limiter = FailedAuthLimiter(1, 900.0)

        # hmac.compare_digest raises TypeError on non-ASCII str operands --
        # this must land on the normal generic-failure path (401, limiter
        # charged), not crash into a 500 that skips the limiter entirely.
        request = _request(pool, limiter=limiter)
        response = await _login_local(request, email="évil@example.com")
        assert response.status_code == 401
        assert "user" not in request.session
        assert limiter.blocked(request.client.host)
    finally:
        await pool.close()


def test_login_local_rejects_non_ascii_email_with_401_and_charges_the_limiter():
    asyncio.run(_non_ascii_email_scenario())


async def _disabled_account_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        await _seed_admin(pool, enabled=False)
        request = _request(pool)
        response = await _login_local(request)
        assert response.status_code == 401
        assert "account_id" not in request.session
    finally:
        await pool.close()


def test_disabled_account_cannot_log_in():
    asyncio.run(_disabled_account_scenario())
