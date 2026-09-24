from __future__ import annotations

import asyncio
import os
import re
import traceback

import httpx

import pytest
from psycopg import errors, sql

from app import application_roles
from app.account_context import AccountPool, AccountPrincipal, account_id
from app.accounts import create_admin
from app.application_roles import (
    CONTROL_TABLES, OWNED_TABLES, PROTECTED_TABLES, REFERENCE_TABLES, TABLES, application_role_pools,
    prepare_application_roles, validate_application_contract,
)
from app.db import make_pool
from app.role_setup import RoleSetupError
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
RLS_TABLES = OWNED_TABLES + PROTECTED_TABLES


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
        # Direct runtime SQL without an account context sees no owned rows.
        async with pools.runtime.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 0
            assert (await (await conn.execute("SELECT NULLIF(current_setting('app.account_id',true),'')")).fetchone())[0] is None
        async with owner.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM vehicles")).fetchone())[0] == 1
            cur = await conn.execute("SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class "
                                     "WHERE relnamespace='public'::regnamespace AND relname=ANY(%s)", (list(RLS_TABLES),))
            assert await cur.fetchone() == (True,)
    asyncio.run(_scenario(check))


def test_activated_validator_rejects_policy_or_activation_drift():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            with pytest.raises(RoleSetupError, match="relation ownership or RLS flags"):
                async with conn.transaction(force_rollback=True):
                    await conn.execute("ALTER TABLE trips DISABLE ROW LEVEL SECURITY")
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


def test_activated_flags_are_exactly_forced_rls_on_rls_tables():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            cur = await conn.execute(
                "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
                "WHERE relnamespace='public'::regnamespace AND relname=ANY(%s)", (list(TABLES),))
            flags = {name: (enabled, forced) for name, enabled, forced in await cur.fetchall()}
            assert flags == {table: (table in RLS_TABLES,) * 2 for table in TABLES}
            cur = await conn.execute("SELECT security_contract_version FROM instance_state")
            assert await cur.fetchall() == [("ownership-activated-v1",)]
            for table in ("managed_role_state", "recovery_metadata"):
                cur = await conn.execute(sql.SQL("SELECT contract_version FROM {}").format(
                    sql.Identifier("odograph_service", table)))
                assert await cur.fetchall() == [("ownership-activated-v1",)]
    asyncio.run(_scenario(check))


def test_validator_names_each_rls_table_missing_enable_or_force():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            for table in RLS_TABLES:
                for change in ("DISABLE ROW LEVEL SECURITY", "NO FORCE ROW LEVEL SECURITY"):
                    with pytest.raises(RoleSetupError, match=rf"relation ownership or RLS flags: \['{table}'\]$"):
                        async with conn.transaction(force_rollback=True):
                            await conn.execute(sql.SQL("ALTER TABLE {} " + change).format(sql.Identifier(table)))
                            await validate_application_contract(conn, state)
            await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


def test_validator_names_rls_on_each_control_or_reference_table():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            for table in CONTROL_TABLES + REFERENCE_TABLES:
                for change in ("ENABLE ROW LEVEL SECURITY", "FORCE ROW LEVEL SECURITY"):
                    with pytest.raises(RoleSetupError, match=rf"relation ownership or RLS flags: \['{table}'\]$"):
                        async with conn.transaction(force_rollback=True):
                            await conn.execute(sql.SQL("ALTER TABLE {} " + change).format(sql.Identifier(table)))
                            await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


POLICY_DRIFT = (
    ("DROP POLICY account_isolation ON vehicles", r"policy set: unexpected \[\] missing \[\('vehicles', 'account_isolation'\)\]$"),
    ("DROP POLICY control_lookup ON tracking_devices", r"missing \[\('tracking_devices', 'control_lookup'\)\]$"),
    ("ALTER POLICY account_isolation ON trips USING (true)", "policy set: trips.account_isolation using expression$"),
    ("ALTER POLICY account_isolation ON trips WITH CHECK (account_id > 0)", "policy set: trips.account_isolation check expression$"),
    ("ALTER POLICY account_isolation ON points TO odograph_control", "policy set: points.account_isolation$"),
    ("CREATE POLICY extra ON stays FOR SELECT TO odograph_runtime USING (true)", r"unexpected \[\('stays', 'extra'\)\]"),
)


def test_validator_names_a_missing_or_altered_policy():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            for statement, cause in POLICY_DRIFT:
                with pytest.raises(RoleSetupError, match=cause):
                    async with conn.transaction(force_rollback=True):
                        await conn.execute(statement)
                        await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


VERSION_DRIFT = (
    ("ALTER TABLE instance_state DROP CONSTRAINT instance_state_security_contract_version_check;"
     "UPDATE instance_state SET security_contract_version='ownership-prepared-v1'",
     "UPDATE instance_state SET security_contract_version='ownership-activated-v1';"
     "ALTER TABLE instance_state ADD CONSTRAINT instance_state_security_contract_version_check "
     "CHECK (security_contract_version='ownership-activated-v1')",
     "security contract version"),
    ("UPDATE odograph_service.managed_role_state SET contract_version='ownership-prepared-v1'",
     "UPDATE odograph_service.managed_role_state SET contract_version='ownership-activated-v1'",
     "managed role state version or owner"),
    ("UPDATE odograph_service.recovery_metadata SET contract_version='ownership-prepared-v1'",
     "UPDATE odograph_service.recovery_metadata SET contract_version='ownership-activated-v1'",
     "recovery metadata"),
)


def test_startup_refuses_a_security_contract_version_mismatch():
    async def check(owner, pools, state):
        for statement, restore, cause in VERSION_DRIFT:
            async with owner.connection() as conn:
                await conn.execute(statement)
            try:
                with pytest.raises(RoleSetupError, match=f"security contract mismatch: {cause}$"):
                    await prepare_application_roles(TEST_DB)
                with pytest.raises(RoleSetupError, match=f"security contract mismatch: {cause}$"):
                    async with application_role_pools(TEST_DB):
                        pass
            finally:
                async with owner.connection() as conn:
                    await conn.execute(restore)
            await prepare_application_roles(TEST_DB)
    asyncio.run(_scenario(check))
