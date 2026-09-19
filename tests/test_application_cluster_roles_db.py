"""Fixed role identities cannot be reassigned by a second database."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from app import application_roles
from app.account_context import AccountPool, AccountPrincipal
from app.accounts import create_admin
from app.db import make_pool, run_migrations
from app.role_setup import ALL_ROLES, RoleSetupError
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


@asynccontextmanager
async def _two_databases():
    name = "odograph_role_guard_" + uuid4().hex
    first = make_pool(TEST_DB)
    second_url = make_conninfo(TEST_DB, dbname=name)
    second = make_pool(second_url)
    await first.open(wait=True)
    created = False
    try:
        await full_schema_reset(first)
        async with await AsyncConnection.connect(TEST_DB, autocommit=True) as admin:
            await admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            created = True
        await second.open(wait=True)
        # This unique database was just created above; migrate it without
        # broadening the reset helper's existing database-name allowlist.
        await run_migrations(second)
        yield first, second, second_url, name
    finally:
        await second.close()
        if created:
            async with await AsyncConnection.connect(TEST_DB, autocommit=True) as admin:
                await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        await full_schema_reset(first)
        await first.close()


async def _role_fingerprint(pool):
    async with pool.connection() as conn:
        roles = await (await conn.execute(
            "SELECT rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,"
            "rolreplication,rolinherit,rolvaliduntil,md5(COALESCE(rolpassword,'')) "
            "FROM pg_authid WHERE rolname=ANY(%s) ORDER BY rolname", (list(ALL_ROLES),),
        )).fetchall()
        memberships = await (await conn.execute(
            "SELECT roleid,member,admin_option FROM pg_auth_members WHERE roleid IN "
            "(SELECT oid FROM pg_roles WHERE rolname=ANY(%s)) OR member IN "
            "(SELECT oid FROM pg_roles WHERE rolname=ANY(%s)) ORDER BY roleid,member",
            (list(ALL_ROLES), list(ALL_ROLES)),
        )).fetchall()
        settings = await (await conn.execute(
            "SELECT setdatabase,setrole,setconfig FROM pg_db_role_setting WHERE setrole IN "
            "(SELECT oid FROM pg_roles WHERE rolname=ANY(%s)) ORDER BY setdatabase,setrole",
            (list(ALL_ROLES),),
        )).fetchall()
        return roles, memberships, settings


async def _unused_roles(pool):
    # Previous tests may leave fixed identities and current-database grants.
    # These databases are disposable; clear those grants to model genuinely
    # unused pre-existing identities without touching the P0 provisioning code.
    async with pool.connection() as conn:
        for role in ALL_ROLES:
            cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            if await cur.fetchone() is None:
                await conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            await conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                sql.Identifier(conn.info.dbname), sql.Identifier(role),
            ))


@pytest.mark.parametrize("operation", ["install", "prepare_restore", "finalize_restore"])
def test_second_install_or_restore_refuses_without_changing_first_credentials(operation):
    async def run():
        async with _two_databases() as (first, second, second_url, _name):
            state = await application_roles.prepare_application_roles(TEST_DB)
            async with application_roles.application_role_pools(TEST_DB) as pools:
                async with pools.control.connection() as conn:
                    account = await create_admin(conn, "first@example.invalid", "unused-test-hash")
            before = await _role_fingerprint(first)
            action = {
                "install": application_roles.prepare_application_roles,
                "prepare_restore": application_roles.prepare_application_restore,
                "finalize_restore": application_roles.finalize_application_restore,
            }[operation]
            with pytest.raises(RoleSetupError, match="used by another database"):
                await action(second_url)
            assert await _role_fingerprint(first) == before
            async with second.connection() as conn:
                assert (await (await conn.execute("SELECT to_regclass('odograph_service.managed_role_state')")).fetchone())[0] is None
            # New connections must still authenticate with the first saved
            # credentials, and its existing account ledger must remain usable.
            async with application_roles.application_role_pools(TEST_DB) as pools:
                bound = AccountPool(pools.runtime, AccountPrincipal(account["id"], True, 1))
                async with bound.connection() as conn:
                    assert (await (await conn.execute("SELECT name FROM vehicles WHERE account_id=%s", (account["id"],))).fetchone())[0] == "My Car"
            assert (await application_roles.prepare_application_roles(TEST_DB)).installation_id == state.installation_id
    asyncio.run(run())


@pytest.mark.parametrize("foreign_reference", ["object", "database_acl", "database_owner", "database_setting"])
def test_setup_and_quarantine_refuse_foreign_role_references_before_identity_changes(foreign_reference, monkeypatch):
    async def run():
        async with _two_databases() as (first, second, _url, name):
            await _unused_roles(first)
            async with second.connection() as conn:
                if foreign_reference == "object":
                    await conn.execute("CREATE TABLE role_guard_marker(id integer)")
                    await conn.execute("ALTER TABLE role_guard_marker OWNER TO odograph_migrate")
                elif foreign_reference == "database_acl":
                    await conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO odograph_runtime").format(sql.Identifier(name)))
                elif foreign_reference == "database_owner":
                    await conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO odograph_migrate").format(sql.Identifier(name)))
                else:
                    await conn.execute(sql.SQL("ALTER ROLE odograph_control IN DATABASE {} SET application_name='role-guard-test'").format(sql.Identifier(name)))
            before = await _role_fingerprint(first)

            async def unexpected(*args, **kwargs):
                pytest.fail("foreign references must be rejected before any identity change")

            monkeypatch.setattr(application_roles, "_identities", unexpected)
            for action in (application_roles.prepare_application_roles, application_roles.prepare_application_restore):
                with pytest.raises(RoleSetupError, match="used by another database"):
                    await action(TEST_DB)
            assert await _role_fingerprint(first) == before
    asyncio.run(run())


def test_concurrent_installs_recheck_after_shared_role_updates_serialize(monkeypatch):
    async def run():
        async with _two_databases() as (first, second, second_url, name):
            await _unused_roles(first)
            original = application_roles._identities
            locked, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            holder_pid = waiter_pid = None

            async def identities(conn, **kwargs):
                nonlocal holder_pid, waiter_pid
                pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                if conn.info.dbname != name:
                    await original(conn, **kwargs)
                    holder_pid = pid
                    locked.set()
                    await release.wait()
                else:
                    waiter_pid = pid
                    second_started.set()
                    await original(conn, **kwargs)

            monkeypatch.setattr(application_roles, "_identities", identities)
            installing = asyncio.create_task(application_roles.prepare_application_roles(TEST_DB))
            competing = None
            try:
                await asyncio.wait_for(locked.wait(), 5)
                competing = asyncio.create_task(application_roles.prepare_application_roles(second_url))
                await asyncio.wait_for(second_started.wait(), 5)
                async with asyncio.timeout(5):
                    while True:
                        async with first.connection() as conn:
                            blockers = (await (await conn.execute("SELECT pg_blocking_pids(%s)", (waiter_pid,))).fetchone())[0]
                        if holder_pid in blockers:
                            break
                        if competing.done():
                            await competing
                            pytest.fail("concurrent identity updates did not wait for the first transaction")
                        await asyncio.sleep(0)
                release.set()
                state = await asyncio.wait_for(installing, 5)
                with pytest.raises(RoleSetupError):
                    await asyncio.wait_for(competing, 5)
                async with second.connection() as conn:
                    assert (await (await conn.execute("SELECT to_regclass('odograph_service.managed_role_state')")).fetchone())[0] is None
                assert (await application_roles.prepare_application_roles(TEST_DB)).installation_id == state.installation_id
                async with application_roles.application_role_pools(TEST_DB):
                    pass
            finally:
                release.set()
                await asyncio.gather(*(task for task in (installing, competing) if task), return_exceptions=True)
    asyncio.run(run())


def test_foreign_grant_committed_after_preflight_is_caught_by_second_check(monkeypatch):
    async def run():
        async with _two_databases() as (first, second, _url, name):
            await _unused_roles(first)
            before = await _role_fingerprint(first)
            original = application_roles._check_role_database_ownership
            checks = 0

            async def guarded(conn):
                nonlocal checks
                checks += 1
                await original(conn)
                if checks == 1:
                    async with second.connection() as other:
                        await other.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO odograph_runtime").format(sql.Identifier(name)))

            monkeypatch.setattr(application_roles, "_check_role_database_ownership", guarded)
            with pytest.raises(RoleSetupError, match="used by another database"):
                await application_roles.prepare_application_roles(TEST_DB)
            assert checks == 2
            assert await _role_fingerprint(first) == before
            async with first.connection() as conn:
                assert (await (await conn.execute("SELECT to_regclass('odograph_service.managed_role_state')")).fetchone())[0] is None
    asyncio.run(run())
