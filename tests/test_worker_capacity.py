"""Shared worker turns and security-mail ownership under contention."""
import asyncio
import contextvars
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.account_workers as workers
from app.account_context import AccountPrincipal
from app.account_workers import AccountWorker, BackgroundScheduler
from app.capacity import AdmissionManager, current_owner, owned_thread
from app.password_reset import SecurityMailAdmission

pytestmark = pytest.mark.capacity_contract


def principal(value):
    return AccountPrincipal(value, True, 1)


def test_ready_types_alternate_account_turns_with_one_registration_each():
    async def run():
        manager = AdmissionManager()
        scheduler = BackgroundScheduler(manager)
        order = []
        first_started, release = asyncio.Event(), asyncio.Event()

        async def sweep(label):
            for value in range(1, 4):
                async with scheduler.turn(label, principal(value)):
                    assert manager.snapshot()["background"]["active"] == 1
                    order.append((label, value))
                    if len(order) == 1:
                        first_started.set()
                        await release.wait()

        first = asyncio.create_task(sweep("detector"))
        await first_started.wait()
        second = asyncio.create_task(sweep("snap"))
        await asyncio.sleep(0)
        assert len(scheduler._ready) == 1
        release.set()
        await asyncio.gather(first, second)
        assert order == [(label, value) for value in range(1, 4) for label in ("detector", "snap")]
        assert not scheduler._registrations and not scheduler._ready
        assert manager.snapshot()["background"]["active"] == 0

    asyncio.run(run())


def test_cancelled_ready_type_does_not_keep_a_registration_or_displace_next():
    async def run():
        manager = AdmissionManager()
        scheduler = BackgroundScheduler(manager)
        entered = []

        async def enter(label):
            async with scheduler.turn(label, principal(2)):
                entered.append(label)

        async with scheduler.turn("detector", principal(1)):
            cancelled = asyncio.create_task(enter("snap"), context=contextvars.Context())
            successor = asyncio.create_task(enter("geocode"), context=contextvars.Context())
            await asyncio.sleep(0)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert scheduler._registrations == {"detector", "geocode"}
        await successor
        assert entered == ["geocode"]
        assert not scheduler._registrations

    asyncio.run(run())


def test_account_sweeps_rotate_the_first_eligible_start(monkeypatch):
    order = []

    async def enabled(pool):
        return [principal(value) for value in (1, 2, 3)]

    @asynccontextmanager
    async def lease(pool, account_id):
        assert current_owner().lane == "background"
        assert current_owner().principal.account_id == account_id
        yield

    class Pool:
        def __init__(self, raw, principal):
            self.principal = principal

        @asynccontextmanager
        async def connection(self):
            yield None

    async def settings(conn):
        return None

    class Job:
        def __init__(self, pool):
            self.pool = pool

        async def run_once(self):
            order.append(self.pool.principal.account_id)

    monkeypatch.setattr(workers, "enabled_principals", enabled)
    monkeypatch.setattr(workers, "external_account_work", lease)
    monkeypatch.setattr(workers, "AccountPool", Pool)
    monkeypatch.setattr(workers, "load_account_settings", settings)
    monkeypatch.setattr(workers, "config_for_account", lambda config, settings: config)

    async def run():
        worker = AccountWorker(SimpleNamespace(control=None, runtime=None), None,
                               lambda pool, config: Job(pool), label="detector",
                               debounce_s=0, sweep_s=60)
        for _ in range(3):
            await worker.run_once()

    asyncio.run(run())
    assert order == [1, 2, 3, 2, 3, 1, 3, 1, 2]


def test_cancelled_background_thread_retains_owner_and_purge_lease():
    started, release = threading.Event(), threading.Event()

    def blocking():
        started.set()
        release.wait(5)

    async def run():
        manager = AdmissionManager()
        scheduler = BackgroundScheduler(manager)

        async def job():
            async with scheduler.turn("detector", principal(1)):
                async with manager.lease((1,)):
                    await owned_thread(blocking)

        task = asyncio.create_task(job())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert manager.snapshot()["leases"] == 1
            assert manager.snapshot()["background"]["active"] == 1
            assert scheduler._registrations == {"detector"}
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.snapshot()["leases"] == 0
        assert not scheduler._registrations

    asyncio.run(run())


def test_saturated_security_mail_refuses_http_initiators_without_waiting():
    async def run():
        manager = AdmissionManager()
        admission = SecurityMailAdmission(capacity=manager)
        release = asyncio.Event()
        both_started = asyncio.Event()
        started = 0

        class Transport:
            async def send(self, message):
                nonlocal started
                assert current_owner().lane == "mail"
                started += 1
                if started == 2:
                    both_started.set()
                await release.wait()

        transport = Transport()
        sends = [asyncio.create_task(admission.send(transport, value)) for value in (1, 2)]
        await both_started.wait()
        assert manager.snapshot()["mail"]["active"] == 2
        assert await admission.send(transport, 3) is False
        assert len(admission._tasks) == 2
        release.set()
        assert await asyncio.gather(*sends) == [True, True]
        await admission.drain()
        assert manager.snapshot()["mail"]["active"] == 0

    asyncio.run(run())


def test_mail_final_check_reserved_while_identity_and_lifecycle_are_full():
    async def run():
        manager = AdmissionManager()
        admission = SecurityMailAdmission(capacity=manager)
        checks = []

        @asynccontextmanager
        async def admit():
            async with manager.control_borrow("mail"):
                checks.append("usable")
                yield True

        @asynccontextmanager
        async def lease():
            async with manager.lease((1,)):
                yield

        class Transport:
            async def send(self, message):
                assert manager.snapshot()["leases"] == 1
                checks.append("sent")

        release, full = asyncio.Event(), asyncio.Event()
        held = 0

        async def occupy(lane):
            nonlocal held
            async with manager.operation(lane):
                held += 1
                if held == 3:
                    full.set()
                await release.wait()

        holders = [asyncio.create_task(occupy(lane)) for lane in ("identity", "identity", "lifecycle")]
        await full.wait()
        try:
            assert await admission.send(Transport(), 1, admit=admit, lease=lease)
        finally:
            release.set()
            await asyncio.gather(*holders)
        assert checks == ["usable", "sent"]
        assert manager.snapshot()["leases"] == 0

    asyncio.run(run())


def test_partial_detector_commit_still_pokes_after_a_later_busy_failure(monkeypatch):
    from app.capacity import CapacityBusy

    async def enabled(pool):
        return [principal(1)]

    @asynccontextmanager
    async def lease(*args):
        yield

    class Pool:
        def __init__(self, *args):
            pass

        @asynccontextmanager
        async def connection(self):
            yield None

    async def settings(conn):
        return None

    class Job:
        produced_work = True

        async def run_once(self):
            raise CapacityBusy("later stream timed out")

    monkeypatch.setattr(workers, "enabled_principals", enabled)
    monkeypatch.setattr(workers, "external_account_work", lease)
    monkeypatch.setattr(workers, "AccountPool", Pool)
    monkeypatch.setattr(workers, "load_account_settings", settings)
    monkeypatch.setattr(workers, "config_for_account", lambda config, settings: config)

    async def run():
        pokes = []
        worker = AccountWorker(SimpleNamespace(control=None, runtime=None), None,
                               lambda pool, config: Job(), label="detector",
                               debounce_s=0, sweep_s=60, after_run=lambda: pokes.append(True))
        await worker._run_guarded()
        assert pokes == [True]
        assert worker.status.last_failure_type == "CapacityBusy"
        assert worker.status.last_success_at is None
        assert worker.status.last_skip_at is None

    asyncio.run(run())
