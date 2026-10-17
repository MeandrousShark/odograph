"""Optional storage limits, grant modes and the administrator exemption."""
import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest

from app.application_roles import _load_state, prepare_application_roles, validate_application_contract
from app.db import make_pool
from app.role_setup import RoleSetupError
from app.storage import enhancement_available, is_storage_capacity_error, storage_status
from conftest import add_test_account, reset_account_db


TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable TEST_DATABASE_URL")
GIB = 1024 ** 3
MIB = 1024 ** 2


def _run(body):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            await body(raw, pool)
        finally:
            await raw.close()
    asyncio.run(scenario())


def _limits(account=None, raw=None, enhancement=None, budget=12 * GIB, reserve=2 * GIB):
    return SimpleNamespace(
        storage_account_limit_bytes=account, storage_raw_limit_bytes=raw,
        storage_enhancement_limit_bytes=enhancement,
        storage_instance_budget_bytes=budget, storage_instance_reserve_bytes=reserve,
    )


async def _grant(conn, owner):
    cur = await conn.execute(
        "SELECT limit_mode,account_limit_bytes,raw_limit_bytes,enhancement_limit_bytes "
        "FROM storage_grants WHERE account_id=%s", (owner,))
    return await cur.fetchone()


async def _configure(conn, account, raw, enhancement, budget, reserve):
    await conn.execute("SELECT public.storage_configure_limits(%s,%s,%s,%s,%s)",
                       (account, raw, enhancement, budget, reserve))


def test_no_configured_limit_leaves_every_account_unlimited():
    async def body(raw, admin_pool):
        await prepare_application_roles(TEST_DB, storage_config=_limits())
        async with raw.connection() as conn:
            assert await (await conn.execute(
                "SELECT account_default_bytes,raw_default_bytes,enhancement_default_bytes "
                "FROM storage_policy")).fetchone() == (None, None, None)
            # Nothing is funded, so even an exhausted budget admits members.
            await conn.execute("UPDATE storage_policy SET instance_budget_bytes=instance_reserve_bytes+1")
        pool = await add_test_account(raw, 77)
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("default", None, None, None)
            assert await _grant(conn, admin_pool.principal.account_id) == ("unlimited", None, None, None)
            assert await (await conn.execute(
                "SELECT public.storage_geocode_room(77,%s)", (2 ** 62,))).fetchone() == (True,)
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(77,'{\"x\":1}')")
            status = await storage_status(conn)
            assert (status["account_limit_bytes"], status["raw_limit_bytes"],
                    status["enhancement_limit_bytes"]) == (None, None, None)
            assert not (status["warning"] or status["account_blocked"] or status["raw_blocked"]
                        or status["enhancement_paused"])
            assert status["recovery"] == ""
            assert await enhancement_available(conn, needed_bytes=2 ** 62)
            assert await (await conn.execute(
                "SELECT geocode_refresh_capacity_pause(77)")).fetchone() == (False,)
        async with raw.connection() as conn:
            assert await (await conn.execute(
                "SELECT total_grants_bytes,account_count,warning_count,blocked_count "
                "FROM public.storage_instance_status()")).fetchone() == (0, 0, 0, 0)
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()"))
                    .fetchone())[0]
    _run(body)


def test_unlimited_measures_are_excluded_from_ceilings_and_counts():
    async def body(raw, admin_pool):
        pool = await add_test_account(raw, 77)
        async with pool.connection() as conn:
            total = (await storage_status(conn))["total_bytes"]
        async with raw.connection() as conn:
            # A finite account limit with no sub-limits, at the account ceiling.
            await conn.execute(
                "UPDATE storage_grants SET limit_mode='custom',account_limit_bytes=%s,"
                "raw_limit_bytes=NULL,enhancement_limit_bytes=NULL WHERE account_id=77", (total,))
            assert await (await conn.execute(
                "SELECT total_grants_bytes,account_count,warning_count,blocked_count "
                "FROM public.storage_instance_status()")).fetchone() == (total, 1, 1, 1)
        async with pool.connection() as conn:
            status = await storage_status(conn)
            assert status["account_blocked"] and not status["raw_blocked"]
            assert status["raw_limit_bytes"] is None
            assert "ask an administrator" in status["recovery"]
            assert not await enhancement_available(conn)
            assert await (await conn.execute(
                "SELECT geocode_refresh_capacity_pause(77)")).fetchone() == (True,)
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(77,'{}')")
        assert is_storage_capacity_error(refused.value)
        assert "account allowance" in str(refused.value)
        async with raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET account_limit_bytes=%s WHERE account_id=77",
                               (total + 10 * MIB,))
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(77,'{}')")
            status = await storage_status(conn)
            assert not (status["account_blocked"] or status["raw_blocked"] or status["warning"])
            assert await enhancement_available(conn)
            assert await (await conn.execute(
                "SELECT geocode_refresh_capacity_pause(77)")).fetchone() == (False,)
    _run(body)


def test_member_grant_funding_and_administrator_exemption():
    async def body(raw, admin_pool):
        async with raw.connection() as conn:
            # The policy is finite and the budget has no room for a member.
            await conn.execute("UPDATE storage_policy SET instance_budget_bytes=instance_reserve_bytes+1")
            await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) "
                               "VALUES(90,'second-admin@example.invalid','unused',true)")
            assert await _grant(conn, 90) == ("unlimited", None, None, None)
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with raw.connection() as conn:
                await conn.execute("INSERT INTO accounts(email,password_hash,is_admin) "
                                   "VALUES('member@example.invalid','unused',false)")
        assert is_storage_capacity_error(refused.value)
        async with raw.connection() as conn:
            await conn.execute("UPDATE storage_policy SET instance_budget_bytes=instance_reserve_bytes+account_default_bytes")
            await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) "
                               "VALUES(91,'member@example.invalid','unused',false)")
            assert await _grant(conn, 91) == ("default", 2 * GIB, 256 * MIB, 128 * MIB)
    _run(body)


def test_configure_limits_changes_default_grants_only_and_funds_finite_grants():
    async def body(raw, admin_pool):
        admin = admin_pool.principal.account_id
        await add_test_account(raw, 77)
        await add_test_account(raw, 78)
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET limit_mode='custom',account_limit_bytes=%s,"
                "raw_limit_bytes=%s,enhancement_limit_bytes=%s WHERE account_id=78",
                (GIB, 256 * MIB, 128 * MIB))
            custom = await _grant(conn, 78)
            await _configure(conn, 3 * GIB, MIB, MIB, 4 * GIB + GIB, GIB)
            assert await _grant(conn, 77) == ("default", 3 * GIB, MIB, MIB)
            assert await _grant(conn, 78) == custom
            assert await _grant(conn, admin) == ("unlimited", None, None, None)
        # The finite total after the change, 3 GiB + 1 byte + 1 GiB, exceeds 4 GiB.
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with raw.connection() as conn:
                await _configure(conn, 3 * GIB + 1, MIB, MIB, 4 * GIB + GIB, GIB)
        assert is_storage_capacity_error(refused.value)
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("default", 3 * GIB, MIB, MIB)
            # Without a default only the custom grant is funded.
            await _configure(conn, None, None, None, GIB + GIB, GIB)
            assert await _grant(conn, 77) == ("default", None, None, None)
            assert await _grant(conn, 78) == custom
            assert await (await conn.execute(
                "SELECT account_default_bytes,instance_budget_bytes FROM storage_policy")
            ).fetchone() == (None, 2 * GIB)
        for limits in ((None, MIB, None), (None, None, MIB), (0, None, None), (MIB, 2 * MIB, None)):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                async with raw.connection() as conn:
                    await _configure(conn, *limits, 12 * GIB, 2 * GIB)
    _run(body)


def test_startup_refuses_an_administrator_grant_that_is_not_unlimited():
    async def body(raw, admin_pool):
        async with raw.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET limit_mode='custom',account_limit_bytes=%s "
                "WHERE account_id=%s", (GIB, admin_pool.principal.account_id))
        for config in (None, _limits()):
            with pytest.raises(RoleSetupError, match="administrator storage grant"):
                await prepare_application_roles(TEST_DB, storage_config=config)
    _run(body)


def test_startup_applies_a_changed_default_before_checking_default_grants():
    async def body(raw, admin_pool):
        await add_test_account(raw, 77)
        async with raw.connection() as conn:
            await conn.execute("UPDATE storage_grants SET account_limit_bytes=%s WHERE account_id=77",
                               (GIB,))
        with pytest.raises(RoleSetupError, match="default storage grant policy"):
            await prepare_application_roles(TEST_DB)
        await prepare_application_roles(TEST_DB, storage_config=_limits(GIB, MIB, MIB))
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("default", GIB, MIB, MIB)
        await prepare_application_roles(TEST_DB, storage_config=_limits())
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("default", None, None, None)
        await prepare_application_roles(TEST_DB)
    _run(body)


@pytest.mark.parametrize("statement,cause", [
    ("ALTER TABLE storage_grants DROP CONSTRAINT storage_grants_mode_limit_check",
     "S2 constraint definition"),
    ("ALTER TABLE storage_grants DROP CONSTRAINT storage_grants_sublimit_check",
     "S2 constraint definition"),
    ("ALTER TABLE storage_policy DROP CONSTRAINT storage_policy_sublimit_check",
     "S2 constraint definition"),
    ("ALTER TABLE storage_grants ALTER COLUMN limit_mode DROP NOT NULL",
     "S2 column definition: storage_grants"),
    ("ALTER TABLE storage_grants ALTER COLUMN limit_mode SET DEFAULT 'unlimited'",
     "S2 column definition: storage_grants"),
])
def test_limit_mode_contract_drift_refuses_startup(statement, cause):
    async def body(raw, pool):
        async with raw.connection() as conn:
            state = await _load_state(conn)
            with pytest.raises(RoleSetupError, match=cause):
                async with conn.transaction(force_rollback=True):
                    await conn.execute(statement)
                    await validate_application_contract(conn, state)
    _run(body)
