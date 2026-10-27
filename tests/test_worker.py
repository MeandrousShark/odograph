"""Tests for the `WorkerStatus` diagnostics bookkeeping `_LoopWorker` and
`PokeSweepWorker` (app/worker.py) maintain -- last run/success/failure and
a next-run-at estimate, all derived purely from the existing guarded-run and
loop machinery with no change to timing/wake-up behavior (see the module's
"Behavior-preserving refactor" note).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.worker import PokeSweepWorker

log = logging.getLogger("test-worker")


class _PokeSweep(PokeSweepWorker):
    def __init__(self, debounce_s: float = 0.05, sweep_s: float = 10.0):
        super().__init__(
            task_name="test-pokesweep", log=log, failure_message="failed",
            debounce_s=debounce_s, sweep_s=sweep_s,
        )
        self.calls = 0
        self.raise_on_call: int | None = None

    async def run_once(self) -> None:
        self.calls += 1
        if self.raise_on_call == self.calls:
            raise ValueError("boom")


def test_run_guarded_records_run_and_success_with_no_prior_failure():
    worker = _PokeSweep()
    asyncio.run(worker._run_guarded())
    assert worker.calls == 1
    assert worker.status.last_run_at is not None
    assert worker.status.last_success_at is not None
    assert worker.status.last_failure_at is None
    assert worker.status.last_failure_type is None


def test_run_guarded_records_only_the_exception_class_and_time_on_failure():
    worker = _PokeSweep()
    worker.raise_on_call = 1
    asyncio.run(worker._run_guarded())
    assert worker.status.last_run_at is not None
    assert worker.status.last_success_at is None
    assert worker.status.last_failure_at is not None
    assert worker.status.last_failure_type == "ValueError"


def test_run_guarded_a_later_success_does_not_erase_the_failure_record():
    # Diagnostics needs both "did it ever fail" and "is it healthy now" --
    # last_failure_at is a historical marker, not cleared by a later success.
    worker = _PokeSweep()
    worker.raise_on_call = 1
    asyncio.run(worker._run_guarded())
    asyncio.run(worker._run_guarded())
    assert worker.status.last_success_at is not None
    assert worker.status.last_failure_type == "ValueError"


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


def test_pokesweep_steady_pokes_do_not_starve_the_debounced_run():
    async def scenario():
        worker = _PokeSweep(debounce_s=0.2, sweep_s=10.0)
        await worker.start()
        await asyncio.sleep(0.02)
        for _ in range(10):  # a poke every 0.1s, half the debounce
            worker.poke()
            await asyncio.sleep(0.1)
        await worker.stop()
        return worker.calls - 1  # minus the immediate start() run

    runs = asyncio.run(scenario())
    assert 3 <= runs <= 6  # about one per 0.2s window; a trailing debounce gives 0


def test_pokesweep_burst_within_the_window_runs_once():
    async def scenario():
        worker = _PokeSweep(debounce_s=0.2, sweep_s=10.0)
        await worker.start()
        await asyncio.sleep(0.02)
        for _ in range(5):
            worker.poke()
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.4)
        await worker.stop()
        return worker.calls

    assert asyncio.run(scenario()) == 2  # start() run plus exactly one poked run


def test_pokesweep_poke_after_a_run_starts_a_new_window():
    async def scenario():
        worker = _PokeSweep(debounce_s=0.1, sweep_s=10.0)
        await worker.start()
        await asyncio.sleep(0.02)
        worker.poke()
        await asyncio.sleep(0.3)
        after_first = worker.calls
        assert worker._deadline is None
        worker.poke()
        assert worker._deadline is not None
        await asyncio.sleep(0.3)
        await worker.stop()
        return after_first, worker.calls

    assert asyncio.run(scenario()) == (2, 3)
