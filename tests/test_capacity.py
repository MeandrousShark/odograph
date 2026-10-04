"""Serving budgets retain actual work, rather than only waiting callers."""
import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from functools import wraps
from types import SimpleNamespace

import pytest

from app.account_context import AccountPrincipal
from app.capacity import (
    AdmissionManager, CapacityBusy, CapacityContractError, current_owner,
    owned_thread, validate_capacity_config,
)

pytestmark = [pytest.mark.unit, pytest.mark.capacity_contract]


def async_test(test):
    @wraps(test)
    def run():
        return asyncio.run(test())
    return run


def principal(account=1):
    return AccountPrincipal(account, True, 1)


async def wait_pending(manager, lane, count):
    for _ in range(100):
        if manager.snapshot()[lane]["pending"] == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(manager.snapshot())


@async_test
async def test_distinct_accounts_fifo_and_predecessor_cannot_jump():
    manager = AdmissionManager()
    first_exit = asyncio.Event()
    second_exit = asyncio.Event()
    order = []

    async def run(account, release):
        async with manager.operation("foreground", principal(account)):
            order.append(account)
            await release.wait()

    first = asyncio.create_task(run(1, first_exit))
    await asyncio.sleep(0)
    second = asyncio.create_task(run(2, second_exit))
    await wait_pending(manager, "foreground", 1)
    with pytest.raises(CapacityBusy):
        async with manager.operation("foreground", principal(2)):
            pass
    first_exit.set()
    await first
    again = asyncio.create_task(run(1, first_exit))
    await wait_pending(manager, "foreground", 1)
    assert order == [1, 2]
    second_exit.set()
    await asyncio.gather(second, again)
    assert order == [1, 2, 1]
    assert manager.snapshot()["foreground"] == {"active": 0, "pending": 0}


@async_test
async def test_queue_bound_cancel_timeout_and_account_reentry():
    manager = AdmissionManager(SimpleNamespace(capacity_foreground_wait_s=.02))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(account):
        async with manager.operation("foreground", principal(account)):
            entered.set()
            await release.wait()

    active = asyncio.create_task(run(1))
    await entered.wait()
    queued = [asyncio.create_task(run(n)) for n in range(2, 6)]
    await wait_pending(manager, "foreground", 4)
    with pytest.raises(CapacityBusy):
        async with manager.operation("foreground", principal(6)):
            pass
    queued[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued[0]
    assert manager.snapshot()["foreground"]["pending"] == 3
    retry = asyncio.create_task(run(2))
    await wait_pending(manager, "foreground", 4)
    results = await asyncio.gather(*queued[1:], retry, return_exceptions=True)
    assert all(isinstance(result, CapacityBusy) for result in results)
    assert manager.snapshot()["foreground"]["pending"] == 0
    release.set()
    await active
    async with manager.operation("foreground", principal(2)):
        pass


@async_test
async def test_reservations_do_not_share_and_no_nested_borrows():
    manager = AdmissionManager()
    async with manager.operation("foreground", principal()):
        async with manager.runtime_borrow(principal()) as owner:
            assert owner.lane == "foreground"
            with pytest.raises(CapacityContractError):
                async with manager.runtime_borrow(principal()):
                    pass
            with pytest.raises(CapacityContractError):
                async with manager.control_borrow():
                    pass
        with pytest.raises(CapacityContractError):
            async with manager.runtime_borrow(principal(2)):
                pass
        async with manager.operation("background", principal(2)):
            async with manager.runtime_borrow(principal(2)):
                pass
    async with manager.operation("auth_ingest"):
        with pytest.raises(CapacityContractError):
            async with manager.operation("auth_ingest"):
                pass


@async_test
async def test_control_lifecycle_and_mail_preserve_identity_slots():
    manager = AdmissionManager()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def lifecycle():
        async with manager.control_borrow("lifecycle"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(lifecycle())
    await entered.wait()
    with pytest.raises(CapacityBusy):
        async with manager.control_borrow("lifecycle"):
            pass
    async with manager.control_borrow():
        assert current_owner().lane == "identity"
    async with manager.operation("mail", principal()):
        async with manager.control_borrow():
            assert current_owner().lane == "mail"
    release.set()
    await task


@async_test
async def test_managed_raw_borrows_reject_missing_nested_and_inherited_task():
    class Pool:
        @asynccontextmanager
        async def connection(self, **kwargs):
            yield object()

    manager = AdmissionManager()
    pool = manager.manage_pool(Pool(), "runtime")
    with pytest.raises(CapacityContractError):
        async with pool.connection():
            pass
    with pytest.raises(CapacityContractError):
        pool.getconn
    async with manager.runtime_borrow(principal()):
        async with pool.connection():
            with pytest.raises(CapacityContractError):
                async with pool.connection():
                    pass
        async def copied_context():
            async with pool.connection():
                pass
        with pytest.raises(CapacityContractError):
            await asyncio.create_task(copied_context())
    async with manager.runtime_borrow(principal()):
        async with pool.connection():
            pass


@async_test
async def test_leases_require_owner_and_release_after_failure():
    manager = AdmissionManager()
    with pytest.raises(CapacityContractError):
        async with manager.lease((1,)):
            pass
    async with manager.operation("foreground", principal()) as owner:
        with pytest.raises(FrozenInstanceError):
            owner.principal = principal(2)
        with pytest.raises(CapacityContractError):
            async with manager.lease((2,)):
                pass
        async with manager.lease((1,)):
            assert manager.snapshot()["leases"] == 1
            with pytest.raises(CapacityContractError):
                async with manager.lease((1,)):
                    pass
            with pytest.raises(RuntimeError):
                async with manager.operation("mail"):
                    async with manager.lease((2,)):
                        raise RuntimeError("transport failed")
            assert manager.snapshot()["leases"] == 1
    assert manager.snapshot()["leases"] == 0


@async_test
async def test_thread_cancel_retains_owner_and_lease_until_actual_return():
    manager = AdmissionManager()
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(5)
        return 42

    async def run():
        async with manager.operation("foreground", principal()):
            async with manager.lease((1,)):
                await owned_thread(blocking)

    task = asyncio.create_task(run())
    while not started.is_set():
        await asyncio.sleep(.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert manager.snapshot()["foreground"]["active"] == 1
    assert manager.snapshot()["leases"] == 1
    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    with pytest.raises(CapacityBusy):
        async with manager.operation("routine", principal(2)):
            pass
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await shutdown
    assert manager.snapshot()["leases"] == 0


@async_test
async def test_shutdown_rejects_pending_and_drains_active():
    manager = AdmissionManager()
    release = asyncio.Event()

    async def run(account):
        async with manager.operation("foreground", principal(account)):
            await release.wait()
    active = asyncio.create_task(run(1))
    await asyncio.sleep(0)
    pending = asyncio.create_task(run(2))
    await wait_pending(manager, "foreground", 1)
    shutdown = asyncio.create_task(manager.shutdown())
    with pytest.raises(CapacityBusy):
        await pending
    assert not shutdown.done()
    release.set()
    await asyncio.gather(active, shutdown)


@pytest.mark.parametrize("setting,value", [
    ("foreground_slots", 2), ("background_slots", 2), ("mail_slots", 3),
    ("auth_ingest_slots", 3), ("auth_interactive_slots", 2),
    ("ingest_slots", 3), ("identity_slots", 3), ("routine_pending", 5),
    ("ingest_pending", -1), ("foreground_wait_s", float("inf")),
    ("auth_body_timeout_s", float("nan")), ("response_timeout_s", 0),
    ("basic_header_max_bytes", 8193), ("multipart_max_files", 2),
])
def test_config_rejects_unbounded_or_excess_capacity(setting, value):
    with pytest.raises(ValueError):
        validate_capacity_config(SimpleNamespace(**{"capacity_" + setting: value}))


@pytest.mark.parametrize("lane,limit", [
    ("ingest", 2), ("routine", 2), ("foreground", 1), ("background", 1),
    ("ingest_identity", 1), ("identity", 2), ("lifecycle", 1), ("mail", 2),
    ("auth_ingest", 2), ("auth_interactive", 1),
])
def test_every_lane_respects_active_limit(lane, limit):
    async def run():
        manager = AdmissionManager(SimpleNamespace(**{
            "capacity_ingest_pending": 0, "capacity_routine_pending": 0,
            "capacity_foreground_pending": 0, "capacity_identity_pending": 0,
        }))
        release = asyncio.Event()
        count = 0
        ready = asyncio.Event()

        async def owner(account):
            nonlocal count
            async with manager.operation(lane, principal(account)):
                count += 1
                if count == limit:
                    ready.set()
                await release.wait()
        tasks = [asyncio.create_task(owner(n + 1)) for n in range(limit)]
        await ready.wait()
        with pytest.raises(CapacityBusy):
            async with manager.operation(lane, principal(limit + 1)):
                pass
        assert manager.snapshot()[lane]["active"] == limit
        release.set()
        await asyncio.gather(*tasks)
    asyncio.run(run())


@async_test
async def test_four_physical_lease_owners_and_no_unadmitted_fifth():
    manager = AdmissionManager()
    release = asyncio.Event()
    ready = asyncio.Event()
    count = 0

    async def run(lane, account):
        nonlocal count
        async with manager.operation(lane, principal(account)):
            async with manager.lease((account,)):
                count += 1
                if count == 4:
                    ready.set()
                await release.wait()
    tasks = [asyncio.create_task(run(lane, account)) for lane, account in
             (("foreground", 1), ("background", 2), ("mail", 3), ("mail", 4))]
    await ready.wait()
    assert manager.snapshot()["leases"] == 4
    with pytest.raises(CapacityBusy):
        async with manager.operation("mail", principal(5)):
            pass
    release.set()
    await asyncio.gather(*tasks)
    assert manager.snapshot()["leases"] == 0


@async_test
async def test_managed_account_helper_sets_deadlines_and_rolls_back_before_return():
    from app.account_context import AccountPool
    from psycopg.pq import TransactionStatus

    events = []

    class Cursor:
        async def fetchone(self):
            return ("1",)

    class Conn:
        info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)

        @asynccontextmanager
        async def transaction(self):
            events.append("transaction")
            try:
                yield
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

        async def execute(self, sql, params=None):
            events.append((sql, params))
            return Cursor()

    class Pool:
        @asynccontextmanager
        async def connection(self, **kwargs):
            try:
                yield Conn()
            finally:
                events.append("returned")

    manager = AdmissionManager()
    bound = AccountPool(manager.manage_pool(Pool(), "runtime"), principal())
    with pytest.raises(RuntimeError):
        async with manager.operation("foreground", principal()):
            async with bound.connection(consistent_snapshot=True):
                raise RuntimeError("statement failed")
    assert events[:4] == ["transaction",
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None),
        ("SELECT set_config('statement_timeout', %s, true)", ("15000ms",)),
        ("SELECT set_config('lock_timeout', %s, true)", ("1000ms",))]
    assert events[-2:] == ["rollback", "returned"]
    assert manager.snapshot()["foreground"]["active"] == 0
    events.clear()
    async with bound.connection():
        assert manager.snapshot()["routine"]["active"] == 1
    assert ("SELECT set_config('statement_timeout', %s, true)", ("5000ms",)) in events
    assert manager.snapshot()["routine"]["active"] == 0


@pytest.mark.parametrize("env,value", [
    ("CAPACITY_FOREGROUND_SLOTS", "2"), ("CAPACITY_ROUTINE_PENDING", "5"),
    ("CAPACITY_AUTH_BODY_TIMEOUT_S", "nan"), ("CAPACITY_INGEST_SLOTS", "3"),
])
def test_config_environment_rejects_unreviewed_bounds(monkeypatch, env, value):
    from app.config import Config
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/example")
    monkeypatch.setenv("SESSION_SECRET", "test-session")
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError):
        Config.from_env()


@pytest.mark.parametrize("workers", ["2", "0", "-1", "garbage"])
@pytest.mark.parametrize("env", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
def test_config_rejects_multiple_serving_processes(monkeypatch, workers, env):
    from app.config import Config
    monkeypatch.setenv(env, workers)
    with pytest.raises(RuntimeError, match="one application process"):
        Config.from_env()


@pytest.mark.parametrize("managed,exception,busy", [
    (True, "statement_timeout", True), (True, "lock_timeout", True),
    (True, "administrator", False), (True, "import_active", False),
    (False, "statement_timeout", False), (False, "lock_timeout", False),
])
def test_timeout_map_preserves_unrelated_database_failures(managed, exception, busy):
    from psycopg.errors import QueryCanceled, LockNotAvailable
    from app.account_context import _serving_timeout_errors

    errors = {
        "statement_timeout": QueryCanceled("canceling statement due to statement timeout"),
        "lock_timeout": LockNotAvailable("canceling statement due to lock timeout"),
        "administrator": QueryCanceled("canceling statement due to user request"),
        "import_active": LockNotAvailable("account import is active"),
    }
    async def run():
        error = errors[exception]
        with pytest.raises(CapacityBusy if busy else type(error)):
            async with _serving_timeout_errors(managed):
                raise error
    asyncio.run(run())


@async_test
async def test_global_owner_rejects_mutable_claimed_identity():
    manager = AdmissionManager()
    with pytest.raises(CapacityContractError):
        async with manager.operation("mail", SimpleNamespace(account_id=1)):
            pass
    with pytest.raises(CapacityContractError):
        async with manager.operation("identity", "client-supplied-identity"):
            pass
