"""Tests for the `WorkerStatus` diagnostics bookkeeping `_LoopWorker` and its
two loop variants (app/worker.py) now maintain -- last run/success/failure and
a next-run-at estimate, all derived purely from the existing guarded-run and
loop machinery with no change to timing/wake-up behavior (see the module's
"Behavior-preserving refactor" note).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.worker import IntervalWorker, PokeSweepWorker

log = logging.getLogger("test-worker")


class _Interval(IntervalWorker):
    def __init__(self, interval_s: float = 0.05):
        super().__init__(
            task_name="test-interval", log=log, failure_message="failed", interval_s=interval_s
        )
        self.calls = 0
        self.raise_on_call: int | None = None

    async def run_once(self) -> None:
        self.calls += 1
        if self.raise_on_call == self.calls:
            raise ValueError("boom")


class _PokeSweep(PokeSweepWorker):
    def __init__(self, debounce_s: float = 0.05, sweep_s: float = 10.0):
        super().__init__(
            task_name="test-pokesweep", log=log, failure_message="failed",
            debounce_s=debounce_s, sweep_s=sweep_s,
        )
        self.calls = 0

    async def run_once(self) -> None:
        self.calls += 1


def test_run_guarded_records_run_and_success_with_no_prior_failure():
    worker = _Interval()
    asyncio.run(worker._run_guarded())
    assert worker.calls == 1
    assert worker.status.last_run_at is not None
    assert worker.status.last_success_at is not None
    assert worker.status.last_failure_at is None
    assert worker.status.last_failure_type is None


def test_run_guarded_records_only_the_exception_class_and_time_on_failure():
    worker = _Interval()
    worker.raise_on_call = 1
    asyncio.run(worker._run_guarded())
    assert worker.status.last_run_at is not None
    assert worker.status.last_success_at is None
    assert worker.status.last_failure_at is not None
    assert worker.status.last_failure_type == "ValueError"


def test_run_guarded_a_later_success_does_not_erase_the_failure_record():
    # Diagnostics needs both "did it ever fail" and "is it healthy now" --
    # last_failure_at is a historical marker, not cleared by a later success.
    worker = _Interval()
    worker.raise_on_call = 1
    asyncio.run(worker._run_guarded())
    asyncio.run(worker._run_guarded())
    assert worker.status.last_success_at is not None
    assert worker.status.last_failure_type == "ValueError"


def test_interval_worker_runs_immediately_and_sets_next_run_at_by_interval():
    async def scenario():
        worker = _Interval(interval_s=0.05)
        await worker.start()
        await asyncio.sleep(0.02)
        await worker.stop()
        return worker

    worker = asyncio.run(scenario())
    assert worker.calls == 1
    assert worker.status.next_run_at is not None
    delta = (worker.status.next_run_at - worker.status.last_run_at).total_seconds()
    assert abs(delta - 0.05) < 0.05


def test_pokesweep_worker_runs_immediately_and_next_run_tracks_the_sweep():
    async def scenario():
        worker = _PokeSweep(debounce_s=0.05, sweep_s=10.0)
        await worker.start()
        await asyncio.sleep(0.02)
        await worker.stop()
        return worker

    worker = asyncio.run(scenario())
    assert worker.calls == 1
    assert worker.status.last_success_at is not None
    delta = (worker.status.next_run_at - worker.status.last_run_at).total_seconds()
    assert 9 < delta <= 10.01


def test_pokesweep_poke_pulls_the_next_run_estimate_in_from_the_sweep():
    async def scenario():
        worker = _PokeSweep(debounce_s=0.05, sweep_s=10.0)
        await worker.start()
        await asyncio.sleep(0.01)
        worker.poke()
        next_run_after_poke = worker.status.next_run_at
        await asyncio.sleep(0.3)  # let the debounce-triggered run land
        await worker.stop()
        return worker, next_run_after_poke

    worker, next_run_after_poke = asyncio.run(scenario())
    assert worker.calls >= 2  # the immediate start() run, plus the poked run
    seconds_out = (next_run_after_poke - datetime.now(timezone.utc)).total_seconds()
    assert seconds_out < 5  # nowhere near the 10s sweep the poke bypassed
