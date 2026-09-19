"""Worker lifecycle robustness (app/worker.py, app/main.py, app/detector/runner.py):

- A lock-contended run_once() is recorded as a skip, never a success
  (RUN_SKIPPED).
- start() no longer leaks a second running loop if called twice (covered
  indirectly by the done-callback test below, which relies on a fresh task
  per start()).
- A dead loop task (a BaseException escaping run_once/`_loop`) is logged at
  critical, not silently dropped.
- stop() swallows the worker task's own cancellation but re-raises one
  delivered to its own caller.
- The lifespan's startup is wrapped in an AsyncExitStack, so a failure partway
  through startup still tears down every resource already opened/started.

Most of these need only asyncio, no database -- the two DB-backed tests
(a real advisory-lock-contended detector skip, and a real lifespan/pool
teardown) are skipped unless TEST_DATABASE_URL is set, same convention as
tests/test_runner_db.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import psycopg
import pytest

from app.config import Config
from app.db import DETECTOR_ADVISORY_LOCK_KEY, make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner, DetectorScheduler
from app.diagnose import worker_reports_from_config
import app.main as main_module
from app.main import create_app
from app.worker import RUN_SKIPPED, IntervalWorker, PokeSweepWorker
from conftest import reset_db, reset_account_db, seed_tracking_device

log = logging.getLogger("test-worker-lifecycle")

TEST_DB = os.environ.get("TEST_DATABASE_URL")
db_only = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)
# db_only alone only skips when TEST_DATABASE_URL is unset; the three cases
# below also carry pytest.mark.db directly, since this file's name has no
# "_db" suffix for tests/conftest.py's automatic tier assignment to key off.


# ---- 1. skip vs. success (unit) ------------------------------------------

class _SkippingWorker(IntervalWorker):
    def __init__(self):
        super().__init__(
            task_name="test-skip", log=log, failure_message="failed", interval_s=100.0
        )

    async def run_once(self):
        return RUN_SKIPPED


class _NormalWorker(IntervalWorker):
    def __init__(self):
        super().__init__(
            task_name="test-normal", log=log, failure_message="failed", interval_s=100.0
        )

    async def run_once(self):
        return None


def test_run_guarded_records_a_skip_not_a_success_when_run_once_returns_run_skipped():
    worker = _SkippingWorker()
    asyncio.run(worker._run_guarded())
    assert worker.status.last_run_at is not None
    assert worker.status.last_skip_at is not None
    assert worker.status.last_success_at is None


def test_run_guarded_still_records_a_normal_success_and_no_skip():
    # Contrast case: a run that actually happens must not be mistaken for a
    # skip either.
    worker = _NormalWorker()
    asyncio.run(worker._run_guarded())
    assert worker.status.last_success_at is not None
    assert worker.status.last_skip_at is None


async def _run_detector_scheduler_records_skip_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    holder = await psycopg.AsyncConnection.connect(TEST_DB)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn)
            await conn.execute(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom) "
                "VALUES(%s,%s,'phone',now(),ST_SetSRID(ST_MakePoint(10,20),4326)::geography)",
                (pool.principal.account_id, device),
            )

        # Hold the detector's advisory lock in an uncommitted transaction on
        # a second connection, mimicking a concurrent instance's in-flight
        # run -- same technique as test_runner_db.py's lock tests.
        await holder.execute(
            "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
        )

        runner = DetectorRunner(pool, Params())
        scheduler = DetectorScheduler(runner, debounce_s=60.0, sweep_s=900.0)
        await scheduler._run_guarded()

        assert scheduler.status.last_skip_at is not None
        assert scheduler.status.last_success_at is None
    finally:
        await holder.close()
        await raw_pool.close()


@db_only
@pytest.mark.db
def test_detector_scheduler_run_guarded_records_a_skip_when_advisory_lock_is_held():
    """A real lock-contended detector run, driven through the scheduler
    wrapper (not DetectorRunner.run_once() directly), must land as a skip
    -- last_success_at untouched -- not a success."""
    asyncio.run(_run_detector_scheduler_records_skip_scenario())


# ---- 3. stop() re-raises a cancellation delivered to its caller ----------

def test_stop_reraises_a_cancellation_delivered_to_its_caller():
    async def scenario():
        worker = _NormalWorker()  # interval_s=100.0: still sleeping when we act
        await worker.start()
        await asyncio.sleep(0)  # let the loop task run its immediate first pass and reach the sleep

        stop_task = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)  # let stop_task cancel the worker task and suspend on `await self._task`
        stop_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await stop_task
        return stop_task

    stop_task = asyncio.run(scenario())
    assert stop_task.cancelled(), (
        "a cancellation delivered to stop()'s own caller must propagate out "
        "of stop(), not be swallowed as if it were the worker task's own "
        "cancellation"
    )


# ---- 4. a dead loop task (BaseException) is logged, not silent ----------

class Boom(BaseException):
    pass


class _BoomWorker(PokeSweepWorker):
    def __init__(self):
        super().__init__(
            task_name="test-boom", log=log, failure_message="failed",
            debounce_s=0.05, sweep_s=10.0,
        )

    async def run_once(self):
        raise Boom("run_once escaped with a BaseException")


def test_done_callback_logs_critical_when_run_once_raises_a_base_exception(caplog):
    async def scenario():
        worker = _BoomWorker()
        await worker.start()  # PokeSweepWorker runs once immediately
        for _ in range(200):
            if worker._task.done():
                break
            await asyncio.sleep(0.01)
        return worker

    with caplog.at_level(logging.CRITICAL, logger="test-worker-lifecycle"):
        worker = asyncio.run(scenario())

    assert worker._task.done()
    assert not worker._task.cancelled()
    with pytest.raises(Boom):
        worker._task.result()
    assert any(
        record.levelno == logging.CRITICAL
        and "worker task exited unexpectedly" in record.message
        for record in caplog.records
    ), caplog.text


# ---- 2. lifespan startup failure still tears down what already came up --

REQUIRED_ENV = {"INGEST_PASSWORD": "ingest-password", "SESSION_SECRET": "session-secret"}
# Every optional gate cleared so only what a test explicitly sets is enabled
# -- OSRM/geocode/ntfy/SMTP off, retention on (its default, > 0 days).
OPTIONAL_ENV_TO_CLEAR = (
    "OSRM_URL", "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "GEOCODE_NOMINATIM_URL",
    "NTFY_URL", "NTFY_TOPIC", "SMTP_HOST", "EMAIL_FROM", "EMAIL_TO",
    "ODOMETER_REMINDER", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
)


class _FakeLifespanResource:
    def __init__(self, *args, **kwargs):
        self.closed = False

    async def open(self, wait=True):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def close(self):
        self.closed = True

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, (False, True, False, False, False)),
        (
            {
                "OSRM_URL": "http://osrm.internal:5000",
                "RAW_MESSAGE_RETENTION_DAYS": "0",
                "NTFY_URL": "http://ntfy.internal",
                "NTFY_TOPIC": "trips",
                "SMTP_HOST": "smtp.internal",
                "EMAIL_FROM": "from@example.com",
                "EMAIL_TO": "to@example.com",
            },
            (True, False, True, True, True),
        ),
    ],
)
def test_lifespan_and_diagnostics_agree_on_config_worker_predicates(
    monkeypatch, environment, expected,
):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    monkeypatch.setenv("DEV_NO_AUTH", "0")
    for key in OPTIONAL_ENV_TO_CLEAR + (
        "RAW_MESSAGE_RETENTION_DAYS", "EMAIL_FROM", "EMAIL_TO",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env()

    pool = _FakeLifespanResource()
    monkeypatch.setattr(main_module, "make_pool", lambda url: pool)

    async def _run_migrations(pool, config=None):
        pass

    monkeypatch.setattr(main_module, "run_migrations", _run_migrations)
    @asynccontextmanager
    async def fake_role_pools(url):
        yield SimpleNamespace(control=pool, runtime=pool)
    monkeypatch.setattr(main_module, "application_role_pools", fake_role_pools)
    monkeypatch.setattr(main_module, "AccountWorker", _FakeLifespanResource)
    monkeypatch.setattr(main_module.httpx, "AsyncClient", _FakeLifespanResource)
    for name in (
        "SnapWorker", "GeocodeWorker", "RetentionWorker", "NudgeWorker",
        "OdometerReminderWorker", "EmailDigestWorker", "DetectorScheduler",
    ):
        monkeypatch.setattr(main_module, name, _FakeLifespanResource)
    monkeypatch.setattr(main_module, "DetectorRunner", _FakeLifespanResource)
    monkeypatch.setattr(main_module, "Mailer", _FakeLifespanResource)

    app = create_app(cfg)

    async def scenario():
        async with app.router.lifespan_context(app):
            startup = (
                app.state.snap_worker is not None,
                app.state.retention_worker is not None,
                app.state.nudge_worker is not None,
                app.state.odometer_reminder_worker is not None,
                app.state.email_digest_worker is not None,
            )
            diagnostics = {
                report.name: report.enabled for report in worker_reports_from_config(cfg)
            }
            reported = (
                diagnostics["snap"],
                diagnostics["retention"],
                diagnostics["nudge"],
                diagnostics["odometer_reminder"],
                diagnostics["email_digest"],
            )
            assert startup == reported == expected

    asyncio.run(scenario())
    assert pool.closed is True


def _configure_env(monkeypatch, **overrides) -> Config:
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("DEV_NO_AUTH", "1")
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in OPTIONAL_ENV_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def _capture_pool(monkeypatch) -> dict:
    """Patches app.main.make_pool to also stash the real pool it returns,
    so a test can assert on `.closed` after startup fails and `app.state`
    was never populated."""
    pools: dict = {}
    real_make_pool = main_module.make_pool

    def _capturing_make_pool(url):
        pool = real_make_pool(url)
        pools["pool"] = pool
        return pool

    monkeypatch.setattr(main_module, "make_pool", _capturing_make_pool)
    return pools


@db_only
@pytest.mark.db
def test_lifespan_closes_the_pool_when_run_migrations_fails(monkeypatch):
    """run_migrations fails immediately after pool.open() -- before any
    worker exists -- and the pool it already opened must still be closed."""
    cfg = _configure_env(monkeypatch)
    app = create_app(cfg)
    pools = _capture_pool(monkeypatch)

    class _MigrationBoom(Exception):
        pass

    async def _raising_run_migrations(pool, config=None):
        raise _MigrationBoom("migrations boom")

    monkeypatch.setattr(main_module, "run_migrations", _raising_run_migrations)

    async def scenario():
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover -- never reached, run_migrations raises first

    with pytest.raises(_MigrationBoom):
        asyncio.run(scenario())

    assert pools["pool"].closed is True


async def _reset_schema_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
    finally:
        await pool.close()


@db_only
@pytest.mark.db
def test_lifespan_stops_an_already_started_worker_when_a_later_worker_fails_to_start(monkeypatch):
    """The retention worker (on by default) starts well before the nudge
    worker in create_app's lifespan. When constructing the nudge worker
    raises, the already-started retention worker must be stopped (not
    leaked) and the pool must still be closed."""
    asyncio.run(_reset_schema_scenario())

    cfg = _configure_env(monkeypatch, NTFY_URL="http://ntfy.invalid", NTFY_TOPIC="mileage")
    app = create_app(cfg)
    pools = _capture_pool(monkeypatch)

    retention_instances = []
    real_worker_cls = main_module.AccountWorker

    class _NudgeBoom(Exception):
        pass

    class _FailingStartupWorker(real_worker_cls):
        def __init__(self, *args, **kwargs):
            if kwargs["label"] == "nudge-worker":
                raise _NudgeBoom("nudge worker boom")
            super().__init__(*args, **kwargs)
            if kwargs["label"] == "retention-worker":
                retention_instances.append(self)

    monkeypatch.setattr(main_module, "AccountWorker", _FailingStartupWorker)

    async def scenario():
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover -- never reached, NudgeWorker() raises first

    with pytest.raises(_NudgeBoom):
        asyncio.run(scenario())

    assert len(retention_instances) == 1
    # stop()'s finally clears _task to None -- the clearest sign the started
    # worker's task was actually awaited/cancelled, not just abandoned.
    assert retention_instances[0]._task is None, (
        "an already-started worker must be stopped when a later startup "
        "step fails, not leaked with its loop still running"
    )
    assert pools["pool"].closed is True
