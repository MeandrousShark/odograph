"""Atomic turns rotate completed work without retaining idle admission."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.account_workers as module
from app.account_context import AccountPrincipal
from app.account_workers import AccountWorker, BackgroundScheduler
from app.capacity import AdmissionManager, owned_thread
from app.worker import BatchOutcome, TurnOutcome

pytestmark = pytest.mark.capacity_contract


def _workers(monkeypatch, accounts, factory, *, scheduler=None, before_turn=None,
             label="test", refresh_deferred_on_wake=False):
    async def enabled(_pool):
        return [AccountPrincipal(owner, True, 1) for owner in accounts]

    @asynccontextmanager
    async def lease(_pool, owner):
        assert owner in accounts
        yield

    class Pool:
        def __init__(self, runtime, principal):
            self.principal = principal

        @asynccontextmanager
        async def connection(self):
            assert self.principal.account_id in accounts
            yield None

    async def settings(_conn):
        return None

    monkeypatch.setattr(module, "enabled_principals", enabled)
    monkeypatch.setattr(module, "external_account_work", lease)
    monkeypatch.setattr(module, "AccountPool", Pool)
    monkeypatch.setattr(module, "load_account_settings", settings)
    monkeypatch.setattr(module, "config_for_account", lambda config, settings: config)
    capacity = scheduler.capacity if scheduler else AdmissionManager()
    return AccountWorker(SimpleNamespace(runtime=None, control=None), None, factory,
                         label=label, debounce_s=0, sweep_s=3600, capacity=capacity,
                         scheduler=scheduler, before_turn=before_turn,
                         refresh_deferred_on_wake=refresh_deferred_on_wake)


def test_accounts_rotate_continuations_and_fresh_round_starts(monkeypatch):
    async def scenario():
        calls = []
        class Job:
            def __init__(self, pool):
                self.owner = pool.principal.account_id

            async def run_turn(self, cursor):
                calls.append((self.owner, cursor))
                return TurnOutcome(BatchOutcome(1, 1), ready=(cursor or 0) < 2,
                                   cursor=(cursor or 0) + 1)

        worker = _workers(monkeypatch, [1, 2, 3], lambda pool, config: Job(pool))
        for _ in range(9):
            await worker._run_guarded()
        assert [owner for owner, _ in calls] == [1, 2, 3, 2, 3, 1, 3, 1, 2]
        assert sorted(calls, key=lambda item: (item[0], item[1] or 0)) == [(owner, cursor) for owner in [1, 2, 3] for cursor in [None, 1, 2]]
        assert worker._continuation_at is None
        assert worker.capacity.snapshot()["background"]["active"] == 0
    asyncio.run(scenario())


def test_round_reenumeration_adds_accounts_and_removes_disabled_deleted(monkeypatch):
    async def scenario():
        accounts, calls = [1, 2], []
        class Job:
            def __init__(self, pool):
                self.owner = pool.principal.account_id

            async def run_turn(self, cursor):
                calls.append(self.owner)
                return TurnOutcome(ready=True)

        worker = _workers(monkeypatch, accounts, lambda pool, config: Job(pool))
        await worker.run_turn()
        accounts.append(3)
        await worker.run_turn()
        assert calls == [1, 2]
        accounts.remove(1)
        await worker.run_turn()
        await worker.run_turn()
        assert calls == [1, 2, 2, 3]
        assert set(worker._continuations) == {2, 3}
    asyncio.run(scenario())


def test_idle_restart_and_deferred_turns_do_not_spin(monkeypatch):
    async def scenario():
        calls = []
        class Job:
            def __init__(self, pool):
                self.owner = pool.principal.account_id

            async def run_turn(self, cursor):
                calls.append(self.owner)
                if self.owner == 2:
                    return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + 300)
                return TurnOutcome()

        accounts = [1, 2]
        worker = _workers(monkeypatch, accounts, lambda pool, config: Job(pool))
        await worker.run_turn()
        await worker.run_turn()
        for _ in range(3):
            await worker.run_turn()
        assert calls == [1, 2]
        assert worker._continuation_at > asyncio.get_running_loop().time() + 290
        worker.wake_cycle()
        await worker.run_turn()
        assert calls == [1, 2, 1]
        restarted = _workers(monkeypatch, accounts, lambda pool, config: Job(pool))
        await restarted.run_turn()
        assert calls == [1, 2, 1, 1]
    asyncio.run(scenario())


def test_seven_registered_types_have_at_most_six_completed_turns_ahead(monkeypatch):
    async def scenario():
        scheduler = BackgroundScheduler(AdmissionManager())
        calls = []
        first_started, release = asyncio.Event(), asyncio.Event()
        class Job:
            def __init__(self, label):
                self.label = label

            async def run_turn(self, cursor):
                calls.append(self.label)
                if len(calls) == 1:
                    first_started.set()
                    await release.wait()
                return TurnOutcome(ready=True)

        workers = [_workers(monkeypatch, [1], lambda pool, config, label=label: Job(label),
                            scheduler=scheduler, label=str(label)) for label in range(7)]
        tasks = [asyncio.create_task(workers[0].run_turn())]
        await first_started.wait()
        tasks.extend(asyncio.create_task(worker.run_turn()) for worker in workers[1:])
        await asyncio.sleep(0)
        assert len(scheduler._registrations) == 7
        release.set()
        await asyncio.gather(*tasks)
        assert calls == list(range(7))
        assert not scheduler._registrations
    asyncio.run(scenario())


def test_ready_backlog_drains_without_periodic_wake(monkeypatch):
    async def scenario():
        done = asyncio.Event()
        calls = []
        class Job:
            async def run_turn(self, cursor):
                calls.append(cursor)
                if len(calls) == 4:
                    done.set()
                return TurnOutcome(ready=len(calls) < 4, cursor=len(calls))

        worker = _workers(monkeypatch, [1], lambda pool, config: Job())
        await worker.start()
        try:
            await asyncio.wait_for(done.wait(), 1)
            assert calls == [None, 1, 2, 3]
        finally:
            await worker.stop()
    asyncio.run(scenario())


def test_cancellation_keeps_scheduler_owner_until_actual_report_thread_ends(monkeypatch):
    import threading
    async def scenario():
        scheduler = BackgroundScheduler(AdmissionManager())
        started, release = threading.Event(), threading.Event()
        def report():
            started.set()
            assert release.wait(5)
        class Job:
            async def run_turn(self, cursor):
                await owned_thread(report)
                return TurnOutcome()
        worker = _workers(monkeypatch, [1], lambda pool, config: Job(), scheduler=scheduler)
        task = asyncio.create_task(worker.run_turn())
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert scheduler._active == "test"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scheduler._active is None
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["monthly_report", "annual_report", "smtp"])
def test_digest_cancel_keeps_transaction_and_owner_through_actual_cpu_or_transport(monkeypatch, phase):
    import threading
    from zoneinfo import ZoneInfo
    import app.email_digest as digest_module
    from app.email_digest import EmailDigestWorker
    from app.mailer import Mailer

    async def scenario():
        started, release = threading.Event(), threading.Event()
        connections, ledger = [], []
        def blocking():
            started.set()
            assert release.wait(5)

        class Conn:
            active = False
            @asynccontextmanager
            async def transaction(self):
                self.active = True
                try:
                    yield
                finally:
                    self.active = False

        class MailPool:
            @asynccontextmanager
            async def connection(self):
                conn = Conn()
                connections.append(conn)
                yield conn

        async def fetch(*args):
            return [], {}
        monkeypatch.setattr(digest_module, "_fetch_range_trips_in", fetch)
        builder_name = "build_annual_report" if phase == "annual_report" else "build_range_report"
        original = getattr(digest_module, builder_name)
        if phase != "smtp":
            def build(*args):
                blocking()
                return original(*args)
            monkeypatch.setattr(digest_module, builder_name, build)
        def transport(mailer, message):
            if phase == "smtp":
                blocking()
        mailer = Mailer("smtp.example.test", 587, "", "", "none", False,
                        "from@example.test", "to@example.test", transport=transport)
        class Digest(EmailDigestWorker):
            async def _already_delivered(self, conn, kind, period_end):
                return False
            async def _record_delivery(self, conn, kind, period_end, sent):
                ledger.append(kind)
        digest = Digest(MailPool(), mailer, "https://example.test", ZoneInfo("UTC"),
                        18, 18, 18, "04-01", False, phase != "annual_report",
                        phase == "annual_report", False)
        worker = _workers(monkeypatch, [1], lambda pool, config: digest)
        task = asyncio.create_task(worker.run_turn())
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert worker.scheduler._active == "test"
            assert connections[0].active
            assert not ledger
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not connections[0].active
            assert not ledger
            assert worker.scheduler._active is None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_pre_admission_token_is_closed_when_cancellation_removes_queued_type(monkeypatch):
    async def scenario():
        scheduler = BackgroundScheduler(AdmissionManager())
        prepared = asyncio.Event()
        class Token:
            closed = False
            def close(self):
                self.closed = True
        token = Token()
        async def before(principal, cursor):
            assert scheduler.capacity.snapshot()["background"]["active"] == 1
            prepared.set()
            return token
        worker = _workers(monkeypatch, [1], lambda pool, config: None,
                          scheduler=scheduler, before_turn=before)
        async with scheduler.turn("holder", AccountPrincipal(1, True, 1)):
            task = asyncio.create_task(worker.run_turn())
            await prepared.wait()
            await asyncio.sleep(0)
            assert "test" in scheduler._registrations
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert token.closed
            assert scheduler._registrations == {"holder"}
        assert scheduler._active is None
    asyncio.run(scenario())


def test_durable_retry_wakes_recheck_new_work_without_waiting_for_old_backoff(monkeypatch):
    async def scenario():
        has_new_work = False
        calls = []

        class Job:
            async def run_turn(self, cursor):
                calls.append(has_new_work)
                return (TurnOutcome(BatchOutcome(1, 1)) if has_new_work else
                        TurnOutcome(deferred_until=asyncio.get_running_loop().time() + 3600))

        worker = _workers(monkeypatch, [1], lambda pool, config: Job(),
                          refresh_deferred_on_wake=True)
        await worker.run_turn()
        await worker.run_turn()
        assert calls == [False]
        worker.wake_cycle()
        await worker.run_turn()
        assert calls == [False, False]
        assert worker._continuation_at > asyncio.get_running_loop().time() + 3500
        has_new_work = True
        worker.wake_cycle()
        result = await worker.run_turn()
        assert calls == [False, False, True]
        assert result.batch.completed == 1
        assert worker.capacity.snapshot()["background"]["active"] == 0
        assert worker._continuation_at is None
    asyncio.run(scenario())
