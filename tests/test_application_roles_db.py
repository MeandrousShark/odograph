from __future__ import annotations

import asyncio
import os
import re
import traceback

import httpx

import pytest
from psycopg import errors

from app import application_roles
from app.account_context import AccountPool, AccountPrincipal, account_id
from app.accounts import create_admin
from app.application_roles import (
    OWNED_TABLES, application_role_pools, prepare_application_roles,
    validate_application_contract,
)
from app.db import make_pool
from app.role_setup import RoleSetupError
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
        state = await prepare_application_roles(TEST_DB)
        async with application_role_pools(TEST_DB) as pools:
            await callback(owner, pools, state)
    finally:
        await owner.close()


def test_live_pools_bootstrap_scoping_and_prepared_privileges():
    async def check(owner, pools, state):
        async with pools.control.connection() as conn:
            account = await create_admin(conn, "owner@example.invalid", "unused-test-hash", display_timezone="Asia/Tokyo")
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT * FROM trips")
        bound = AccountPool(pools.runtime, AccountPrincipal(account["id"], True, 1))
        async with bound.connection() as conn:
            cur = await conn.execute("SELECT name FROM vehicles WHERE account_id=%s", (account_id(conn),))
            assert await cur.fetchall() == [("My Car",)]
            for statement in ("SELECT password_hash FROM accounts", "SELECT secret_hash FROM ingest_credentials", "TRUNCATE trips", "CREATE TABLE forbidden(id int)"):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(statement)
        # Policies are intentionally disabled at this stage. Direct runtime SQL
        # does not claim missing-context denial until the activation package.
        async with pools.runtime.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 1
            assert (await (await conn.execute("SELECT NULLIF(current_setting('app.account_id',true),'')")).fetchone())[0] is None
        async with owner.connection() as conn:
            cur = await conn.execute("SELECT bool_or(relrowsecurity OR relforcerowsecurity) FROM pg_class WHERE relname=ANY(%s)", (list(OWNED_TABLES),))
            assert await cur.fetchone() == (False,)
    asyncio.run(_scenario(check))


def test_prepared_validator_rejects_policy_or_activation_drift():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            with pytest.raises(RoleSetupError, match="relation ownership or RLS flags"):
                async with conn.transaction(force_rollback=True):
                    await conn.execute("ALTER TABLE trips ENABLE ROW LEVEL SECURITY")
                    await validate_application_contract(conn, state)
            with pytest.raises(RoleSetupError, match="policy set"):
                async with conn.transaction(force_rollback=True):
                    await conn.execute("DROP POLICY account_isolation ON trips")
                    await validate_application_contract(conn, state)
            await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


def test_provisioning_failure_stays_generic_without_leaking_the_cause(monkeypatch):
    async def broken_provision(conn):
        raise RuntimeError("scram-secret-should-never-leak")

    monkeypatch.setattr(application_roles, "_provision", broken_provision)

    async def run():
        owner = make_pool(TEST_DB)
        await owner.open(wait=True)
        try:
            async with owner.connection() as conn:
                await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
            await full_schema_reset(owner)
            with pytest.raises(RoleSetupError) as exc_info:
                await prepare_application_roles(TEST_DB)
            error = exc_info.value
            assert str(error) == "application database setup failed"
            assert error.__cause__ is None
            rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            assert "scram-secret-should-never-leak" not in rendered
        finally:
            await owner.close()
    asyncio.run(run())


def test_normal_signup_and_personal_pages_use_restricted_pools(monkeypatch):
    from app.config import Config
    from app.main import create_app
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("SESSION_SECRET", "disposable-session-secret")
    monkeypatch.setenv("INITIAL_ADMIN_SIGNUP", "1")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    for name in ("OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "NTFY_URL", "SMTP_HOST", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    async def check(owner, pools, state):
        app = create_app(Config.from_env())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
                page = await client.get("/signup")
                assert page.status_code == 200
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                result = await client.post("/signup", data={"email": "owner@example.invalid",
                    "password": "test-password", "password_confirm": "test-password", "csrf_token": csrf,
                    "display_timezone": "Asia/Tokyo"})
                assert result.status_code == 303
                for path in ("/", "/trips", "/review", "/report", "/expenses", "/stats", "/settings", "/settings/account", "/settings/tracking", "/settings/export/data"):
                    result = await client.get(path, follow_redirects=True)
                    assert result.status_code == 200, (path, result.status_code)
                    assert result.headers["cache-control"] == "no-store, private"
                    assert result.headers["x-odograph-account"]
                page = await client.get("/settings/tracking")
                csrf = re.search(r'X-CSRF-Token": "([^"]+)"', page.text).group(1)
                result = await client.post("/settings/tracking/devices", data={"label": "Phone", "csrf_token": csrf})
                assert result.status_code == 200
                assert "Phone" in result.text
    asyncio.run(_scenario(check))
