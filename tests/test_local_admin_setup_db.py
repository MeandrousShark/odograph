"""DB-backed tests for migration 017 and the /setup create/reset/auto-consume
flow. Route handlers are invoked directly (same convention as
tests/test_odometer_db.py): `require_csrf` is a router-level dependency the
ASGI app enforces, not something calling the Python function exercises, so
these tests drive the manual `_check_form_csrf` path inside the handlers
via the `csrf_token` form field instead.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors
from psycopg.rows import dict_row

import app.auth as auth_module
from app.auth import make_router
from app.db import make_pool, run_migrations
from app.ingest import FailedAuthLimiter
from app.local_auth import verify_password
from app.main import make_templates

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("UTC")


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route missing")


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


def _request(pool, *, admin_token="setup-token", ip="203.0.113.5", limiter=None, session=None):
    cfg = SimpleNamespace(admin_token=admin_token, dev_no_auth=False, allowed_email="")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, config=cfg,
            templates=make_templates(SimpleNamespace(display_tz=TZ)),
            oauth=None,
            login_limiter=limiter or FailedAuthLimiter(3, 900.0),
        )),
        session=session if session is not None else {"csrf": "test-csrf"},
        client=SimpleNamespace(host=ip),
    )


async def _local_admin_row(pool):
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, email, password_hash, consumed_token_hash FROM local_admin WHERE id = 1"
        )
        row = await cur.fetchone()
    return row


def _setup_get(request):
    return _endpoint("/setup", "GET")(request)


def _setup_post(request, **overrides):
    fields = {
        "token": "setup-token", "email": "admin@example.com",
        "password": "correct horse battery", "password_confirm": "correct horse battery",
        "csrf_token": "test-csrf",
    }
    fields.update(overrides)
    return _endpoint("/setup", "POST")(request, **fields)


async def _migration_shape_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO local_admin (id, email, password_hash, consumed_token_hash) "
                "VALUES (1, 'a@example.com', 'hash', 'hash') RETURNING id"
            )
            assert (await cur.fetchone())[0] == 1

        # A second row (even a different id) is a database-level
        # impossibility, not just an app-level convention.
        async with pool.connection() as conn:
            with pytest.raises(errors.UniqueViolation):
                await conn.execute(
                    "INSERT INTO local_admin (id, email, password_hash, consumed_token_hash) "
                    "VALUES (1, 'b@example.com', 'hash', 'hash')"
                )
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO local_admin (id, email, password_hash, consumed_token_hash) "
                    "VALUES (2, 'c@example.com', 'hash', 'hash')"
                )
    finally:
        await pool.close()


def test_migration_017_enforces_single_row_invariant_at_the_db_level():
    asyncio.run(_migration_shape_scenario())


async def _create_then_reset_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        get_request = _request(pool)
        create_page = await _setup_get(get_request)
        assert create_page.status_code == 200
        assert b"Create administrator" in create_page.body

        post_request = _request(pool)
        response = await _setup_post(post_request)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

        admin = await _local_admin_row(pool)
        assert admin is not None
        assert admin["email"] == "admin@example.com"
        assert verify_password("correct horse battery", admin["password_hash"])

        # Second visit offers only reset, and can never create a second
        # admin -- proven by the single-row constraint test above plus this
        # route-level check that it renders the reset copy, not create.
        second_get_request = _request(pool)
        reset_page = await _setup_get(second_get_request)
        assert reset_page.status_code == 200
        assert b"Reset administrator password" in reset_page.body
        assert b"Create administrator" not in reset_page.body
    finally:
        await pool.close()


def test_setup_creates_sole_admin_then_offers_reset_only():
    asyncio.run(_create_then_reset_scenario())


async def _auto_consume_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        create_request = _request(pool, admin_token="token-A")
        response = await _setup_post(create_request, token="token-A")
        assert response.status_code == 303
        admin = await _local_admin_row(pool)
        original_hash = admin["password_hash"]

        # Reusing the just-consumed token is refused with the explicit
        # already-used error, not the generic wrong-token failure.
        reuse_request = _request(pool, admin_token="token-A")
        reuse_response = await _setup_post(
            reuse_request, token="token-A", password="new password one", password_confirm="new password one",
        )
        assert reuse_response.status_code == 400
        assert b"already used" in reuse_response.body
        admin_after_reuse = await _local_admin_row(pool)
        assert admin_after_reuse["password_hash"] == original_hash

        # A fresh token loaded after the operator recreates the app
        # succeeds and resets the password.
        fresh_request = _request(pool, admin_token="token-B")
        fresh_response = await _setup_post(
            fresh_request, token="token-B", password="new password two", password_confirm="new password two",
        )
        assert fresh_response.status_code == 303
        admin_after_reset = await _local_admin_row(pool)
        assert admin_after_reset["password_hash"] != original_hash
        assert verify_password("new password two", admin_after_reset["password_hash"])

        # Each successful reset consumes its own token -- reusing token-B
        # now fails the same way token-A did above.
        reuse_fresh_request = _request(pool, admin_token="token-B")
        reuse_fresh_response = await _setup_post(
            reuse_fresh_request, token="token-B", password="new password three", password_confirm="new password three",
        )
        assert reuse_fresh_response.status_code == 400
        assert b"already used" in reuse_fresh_response.body
    finally:
        await pool.close()


def test_setup_token_auto_consumes_and_each_reset_consumes_its_own_token():
    asyncio.run(_auto_consume_scenario())


async def _concurrent_reset_scenario(monkeypatch):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create_request = _request(pool, admin_token="token-A")
        assert (await _setup_post(create_request, token="token-A")).status_code == 303

        original_get_local_admin = auth_module._get_local_admin
        reads_ready = 0
        release_reads = asyncio.Event()

        async def synchronized_get_local_admin(conn):
            nonlocal reads_ready
            admin = await original_get_local_admin(conn)
            reads_ready += 1
            if reads_ready == 2:
                release_reads.set()
            await release_reads.wait()
            return admin

        monkeypatch.setattr(auth_module, "_get_local_admin", synchronized_get_local_admin)
        requests = [
            _request(pool, admin_token="token-B", ip=f"203.0.113.{index}")
            for index in (10, 11)
        ]
        responses = await asyncio.gather(
            _setup_post(
                requests[0], token="token-B", password="new password one",
                password_confirm="new password one",
            ),
            _setup_post(
                requests[1], token="token-B", password="new password two",
                password_confirm="new password two",
            ),
        )

        assert sorted(response.status_code for response in responses) == [303, 400]
        rejected = next(response for response in responses if response.status_code == 400)
        assert b"already used" in rejected.body

        admin = await _local_admin_row(pool)
        assert sum(
            verify_password(candidate, admin["password_hash"])
            for candidate in ("new password one", "new password two")
        ) == 1
    finally:
        await pool.close()


def test_concurrent_resets_cannot_consume_the_same_fresh_token_twice(monkeypatch):
    asyncio.run(_concurrent_reset_scenario(monkeypatch))


async def _wrong_token_and_mismatch_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        wrong_token_request = _request(pool, admin_token="the-real-token")
        wrong_response = await _setup_post(wrong_token_request, token="guess")
        assert wrong_response.status_code == 401
        assert b"Invalid setup token" in wrong_response.body
        assert await _local_admin_row(pool) is None

        mismatch_request = _request(pool, admin_token="the-real-token")
        mismatch_response = await _setup_post(
            mismatch_request, token="the-real-token",
            password="password one", password_confirm="password two",
        )
        assert mismatch_response.status_code == 400
        assert b"do not match" in mismatch_response.body
        assert await _local_admin_row(pool) is None
    finally:
        await pool.close()


def test_setup_rejects_wrong_token_and_mismatched_passwords_without_creating_admin():
    asyncio.run(_wrong_token_and_mismatch_scenario())


async def _limiter_throttling_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        limiter = FailedAuthLimiter(2, 900.0)

        for _ in range(2):
            request = _request(pool, admin_token="the-real-token", limiter=limiter)
            response = await _setup_post(request, token="wrong")
            assert response.status_code == 401

        blocked_request = _request(pool, admin_token="the-real-token", limiter=limiter)
        blocked_response = await _setup_post(blocked_request, token="wrong")
        assert blocked_response.status_code == 429

        # The correct token is throttled the same as a wrong one once
        # blocked -- the limiter counts *attempts* from the IP, matching
        # the ingest limiter's per-IP (not per-credential) behavior.
        correct_but_blocked_request = _request(pool, admin_token="the-real-token", limiter=limiter)
        still_blocked = await _setup_post(correct_but_blocked_request, token="the-real-token")
        assert still_blocked.status_code == 429
        assert await _local_admin_row(pool) is None
    finally:
        await pool.close()


def test_setup_limiter_throttles_repeated_wrong_tokens_from_one_ip():
    asyncio.run(_limiter_throttling_scenario())
