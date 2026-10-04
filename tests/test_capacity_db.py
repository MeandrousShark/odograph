"""Restricted database transactions retain their serving resource owners."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from psycopg import errors

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_work import external_account_work
from app.capacity import AdmissionManager, CapacityBusy, CapacityContractError
from app.db import make_pool
from conftest import add_test_account, reset_account_db, reset_db, restricted_role_pools

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.capacity_contract,
              pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")]


@asynccontextmanager
async def _scenario(**settings):
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    manager = AdmissionManager(SimpleNamespace(**settings))
    try:
        first = await reset_account_db(raw)
        second = await add_test_account(raw, 99)
        roles = await restricted_role_pools(raw)
        runtime = manager.manage_pool(roles.runtime, "runtime")
        control = manager.manage_pool(roles.control, "control")
        yield raw, manager, runtime, control, (
            AccountPool(runtime, first.principal), AccountPool(runtime, second.principal))
    finally:
        await manager.shutdown()
        await reset_db(raw)
        await raw.close()


def test_statement_deadline_rolls_back_changes_and_connection_is_reusable():
    async def run():
        async with _scenario(capacity_routine_sql_timeout_s=.025) as (raw, manager, runtime, control, accounts):
            first, second = accounts
            async with first.connection() as conn:
                original = (await (await conn.execute("SELECT name FROM vehicles")).fetchone())[0]
            with pytest.raises(CapacityBusy):
                async with first.connection() as conn:
                    await conn.execute("UPDATE vehicles SET name='uncommitted deadline change'")
                    await conn.execute("SELECT pg_sleep(.2)")
            assert manager.snapshot()["routine"] == {"active": 0, "pending": 0}
            async with first.connection() as conn:
                assert (await (await conn.execute("SELECT name FROM vehicles")).fetchone())[0] == original
                assert (await (await conn.execute("SELECT current_setting('statement_timeout')")).fetchone()) == ("25ms",)
            async with second.connection() as conn:
                assert (await (await conn.execute("SELECT DISTINCT account_id FROM vehicles")).fetchall()) == [(99,)]
    asyncio.run(run())


def test_locked_account_uses_one_routine_slot_and_other_account_remains_accessible():
    async def run():
        async with _scenario(capacity_lock_timeout_s=.3) as (raw, manager, runtime, control, accounts):
            first, second = accounts
            async with raw.connection() as blocker:
                await blocker.execute("SELECT id FROM vehicles WHERE account_id=41 FOR UPDATE")

                async def blocked_update():
                    async with first.connection() as conn:
                        await conn.execute("UPDATE vehicles SET name='blocked change'")

                task = asyncio.create_task(blocked_update())
                try:
                    async def wait_for_database_lock():
                        while True:
                            async with raw.connection() as probe:
                                row = await (await probe.execute(
                                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='odograph_runtime' "
                                    "AND wait_event_type='Lock')")).fetchone()
                            if row == (True,):
                                return
                            await asyncio.sleep(.005)
                    await asyncio.wait_for(wait_for_database_lock(), 2)
                    with pytest.raises(CapacityBusy):
                        async with first.connection():
                            pass
                    assert manager.snapshot()["routine"]["active"] == 1
                    async with second.connection() as conn:
                        assert await (await conn.execute("SELECT DISTINCT account_id FROM vehicles")).fetchall() == [(99,)]
                    with pytest.raises(CapacityBusy):
                        await asyncio.wait_for(task, 2)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            async with first.connection() as conn:
                assert (await (await conn.execute("SELECT name FROM vehicles")).fetchone()) == ("My Car",)
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["disable", "version"])
def test_waiting_operation_revalidates_lifecycle_and_forced_rls_after_admission(mutation):
    async def run():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            first, second = accounts
            release = asyncio.Event()
            entered = asyncio.Event()

            async def active():
                async with manager.operation("foreground", first.principal):
                    entered.set()
                    await release.wait()

            async def waiting():
                async with manager.operation("foreground", second.principal):
                    async with second.connection() as conn:
                        await conn.execute("SELECT * FROM vehicles")

            owner = asyncio.create_task(active())
            await asyncio.wait_for(entered.wait(), 2)
            queued = asyncio.create_task(waiting())
            try:
                async def pending():
                    while manager.snapshot()["foreground"]["pending"] != 1:
                        await asyncio.sleep(0)
                await asyncio.wait_for(pending(), 2)
                async with raw.connection() as conn:
                    await conn.execute("UPDATE accounts SET " + (
                        "is_enabled=false" if mutation == "disable" else "auth_version=2") + " WHERE id=99")
                release.set()
                await owner
                with pytest.raises(errors.InsufficientPrivilege):
                    await queued
                assert manager.snapshot()["foreground"] == {"active": 0, "pending": 0}
                async with raw.connection() as conn:
                    await conn.execute("UPDATE accounts SET is_enabled=true WHERE id=99")
                    version = 1 if mutation == "disable" else 2
                    assert await (await conn.execute(
                        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='vehicles'::regclass"
                    )).fetchone() == (True, True)
                refreshed = AccountPool(runtime, AccountPrincipal(99, True, version))
                async with manager.operation("foreground", refreshed.principal):
                    async with refreshed.connection() as conn:
                        assert await (await conn.execute("SELECT DISTINCT account_id FROM vehicles")).fetchall() == [(99,)]
            finally:
                release.set()
                for task in (owner, queued):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(owner, queued, return_exceptions=True)
    asyncio.run(run())


def test_reserved_runtime_connections_and_missing_raw_owner():
    async def run():
        async with _scenario(capacity_routine_pending=0) as (raw, manager, runtime, control, accounts):
            first, second = accounts
            third = await add_test_account(raw, 100)
            third = AccountPool(runtime, third.principal)
            with pytest.raises(CapacityContractError):
                async with runtime.connection():
                    pass
            with pytest.raises(CapacityContractError):
                async with control.connection():
                    pass
            release = asyncio.Event()
            entered = asyncio.Event()
            count = 0
            pids = []

            async def routine(account):
                nonlocal count
                async with account.connection() as conn:
                    pids.append((await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0])
                    count += 1
                    if count == 2:
                        entered.set()
                    await release.wait()
            tasks = [asyncio.create_task(routine(account)) for account in accounts]
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with pytest.raises(CapacityBusy):
                    async with third.connection():
                        pass
                async with manager.operation("foreground", third.principal):
                    async with third.connection() as conn:
                        pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                        assert pid not in pids
                        assert await (await conn.execute("SELECT DISTINCT account_id FROM vehicles")).fetchall() == [(100,)]
                assert manager.snapshot()["routine"]["active"] == 2
            finally:
                release.set()
                await asyncio.gather(*tasks)
    asyncio.run(run())


def test_four_independent_leases_allow_disable_but_block_purge_until_release():
    async def run():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            first, second = accounts
            third = await add_test_account(raw, 100)
            fourth = await add_test_account(raw, 101)
            release, entered = asyncio.Event(), asyncio.Event()
            count = 0

            async def lease(lane, principal):
                nonlocal count
                async with manager.operation(lane, principal):
                    async with external_account_work(control, principal.account_id):
                        count += 1
                        if count == 4:
                            entered.set()
                        await release.wait()
            tasks = [asyncio.create_task(lease(lane, principal)) for lane, principal in (
                ("foreground", second.principal), ("background", first.principal),
                ("mail", third.principal), ("mail", fourth.principal))]
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert manager.snapshot()["leases"] == 4
                async with raw.connection() as conn:
                    count = (await (await conn.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE usename='odograph_control'"
                    )).fetchone())[0]
                    assert count >= 5  # One pooled backend plus four independent leases.
                    actor_hash = (await (await conn.execute("SELECT password_hash FROM accounts WHERE id=41")).fetchone())[0]
                    await conn.execute("UPDATE accounts SET is_enabled=false,deletion_deadline=now()-interval '1 day' WHERE id=99")
                async with control_connection(control, lane="lifecycle") as conn:
                    with pytest.raises(errors.InsufficientPrivilege):
                        async with conn.transaction():
                            await conn.execute("SELECT public.admin_purge_account(41,1,99,'account-99@example.invalid',true,%s,NULL)",
                                               (actor_hash,))
                release.set()
                await asyncio.gather(*tasks)
                assert manager.snapshot()["leases"] == 0
                async with control_connection(control, lane="lifecycle") as conn:
                    result = await (await conn.execute(
                        "SELECT public.admin_purge_account(41,1,99,'account-99@example.invalid',true,%s,NULL)", (actor_hash,)
                    )).fetchone()
                    assert result == ("purged",)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())
