"""Shared background-worker loop machinery.

Five workers grew independently and converged on two shapes:

- `AccountWorker`, `SnapWorker`, `GeocodeWorker` each needed identical
  poke/debounce/sweep wake-up logic -- an external `poke()` resets a
  debounce deadline so a burst of pokes coalesces into one run shortly
  after the burst settles, while an independent periodic sweep guarantees
  forward progress even if nothing ever pokes (or a poked run is skipped
  or fails).
- `RetentionWorker` and `NudgeWorker` needed only a simpler fixed-interval
  variant: run, sleep a fixed duration, repeat forever, with no external
  wake-up at all.

`PokeSweepWorker` and `IntervalWorker` below factor out the loop,
`start`/`stop`, and guarded-run wrapper both variants shared line-for-line
across their five copies. A subclass implements only `run_once()` -- the
actual unit of work -- and passes its task name, sweep/interval cadence,
and failure-log wording to the base constructor. `after_run_once()` is a
no-op hook a subclass can override to react to its own `run_once()`
result; `AccountWorker` (app/account_workers.py) uses it to call an
optional `after_run` callback, which is how app/main.py pokes the
detector's snap/geocode workers, but only after a sweep that actually did
something (not one skipped for advisory-lock contention).

Behavior-preserving refactor: no timing or wake-up semantics changed from
the five pre-extraction copies. Each worker keeps emitting its own
pre-existing log messages through its own module's logger (passed in
here, not a shared one), since those exact messages are load-bearing for
ops -- see each worker module for its specific wording.

`WorkerStatus` below is the one addition: every subclass gets a `status`
attribute the base classes update from `_run_guarded()`/`_loop()` with no
subclass changes required. `EmailDigestWorker` is the one exception -- its
own per-kind guard (see app/email_digest.py) never lets an exception reach
`_run_guarded()`'s except clause, so it records its own failures directly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# A run_once() return value meaning "deliberately skipped, e.g. lost the
# advisory lock" -- _run_guarded records this as a skip, not a success, so
# diagnostics can tell "nothing needed doing" apart from "lost a race with
# another instance/process and did not run at all".
RUN_SKIPPED = object()


@dataclass
class WorkerStatus:
    """In-memory run history for one worker -- diagnostics-only, reset on
    every process restart by design (no table, no migration; see the
    diagnostics report builder for why that tradeoff was accepted). Holds
    only the exception *class* on failure, never the traceback or its
    arguments, since those can carry whatever the failing call was
    operating on (a coordinate, a URL with a query-string credential).
    """

    label: str
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_skip_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_type: str | None = None
    next_run_at: datetime | None = None

    def record_run(self) -> None:
        self.last_run_at = _utcnow()

    def record_success(self) -> None:
        self.last_success_at = _utcnow()

    def record_skip(self) -> None:
        self.last_skip_at = _utcnow()

    def record_failure(self, exc: BaseException) -> None:
        self.last_failure_at = _utcnow()
        self.last_failure_type = type(exc).__name__


class _LoopWorker:
    """`start`/`stop`/guarded-run bookkeeping shared by both loop variants
    below. Not meant to be used directly -- see `PokeSweepWorker` and
    `IntervalWorker`.
    """

    def __init__(self, task_name: str, log: logging.Logger, failure_message: str):
        self._task_name = task_name
        self._log = log
        self._failure_message = failure_message
        self._task: asyncio.Task | None = None
        self.status = WorkerStatus(label=task_name)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name=self._task_name)
        self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return  # normal shutdown via stop()
        exc = task.exception()
        if exc is not None:
            # _run_guarded already catches Exception, so only a BaseException
            # (or a bug in the loop machinery itself) can get the task here --
            # either way this worker is now dead until the process restarts,
            # which is worth shouting about. Calling .exception() also
            # retrieves it, so asyncio doesn't separately log "exception was
            # never retrieved".
            self._log.critical(
                "%s: worker task exited unexpectedly and will not run again "
                "until restart", self._task_name, exc_info=exc,
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            # Swallow the worker task's own cancellation, but a cancellation
            # delivered to THIS coroutine belongs to our caller and must
            # propagate (Task.cancelling() counts requests against us).
            if asyncio.current_task().cancelling() > 0:
                raise
        finally:
            self._task = None

    async def run_once(self) -> Any:
        raise NotImplementedError

    async def after_run_once(self, result: Any) -> None:
        """Overridable hook for a subclass that needs to react to its own
        `run_once()` result. No-op by default.
        """

    async def _run_guarded(self) -> None:
        self.status.record_run()
        try:
            result = await self.run_once()
            if result is RUN_SKIPPED:
                self.status.record_skip()
                return
            await self.after_run_once(result)
        except Exception as exc:
            self.status.record_failure(exc)
            self._log.exception(self._failure_message)
        else:
            self.status.record_success()

    async def _loop(self) -> None:
        raise NotImplementedError


class PokeSweepWorker(_LoopWorker):
    """poke() (from whatever produces this worker's work) resets a debounce
    deadline; independently, a sweep fires every `sweep_s` to catch
    anything a crashed/skipped run left behind. Always runs once
    immediately on `start()`, before the first wait -- this both drains
    anything left over from a previous process's lifetime and (for the
    detector's `AccountWorker` specifically, through `DetectorRunner.run_once()`)
    applies a pending `DETECTOR_VERSION` bump's full reprocess without
    waiting for a poke.
    """

    def __init__(
        self,
        task_name: str,
        log: logging.Logger,
        failure_message: str,
        debounce_s: float,
        sweep_s: float,
    ):
        super().__init__(task_name, log, failure_message)
        self.debounce_s = debounce_s
        self.sweep_s = sweep_s
        self._wake = asyncio.Event()
        self._deadline: float | None = None
        self._next_sweep: float | None = None

    def poke(self) -> None:
        self._deadline = asyncio.get_running_loop().time() + self.debounce_s
        self._wake.set()
        self._update_next_run_estimate()

    def _update_next_run_estimate(self) -> None:
        """Translate the monotonic-clock deadline/sweep the loop actually
        tracks into a wall-clock estimate for diagnostics, recomputed from a
        fresh anchor each call (rather than one anchor captured at loop
        start) so it never drifts across a long-running process.
        """
        if self._next_sweep is None:
            self.status.next_run_at = None
            return
        target = self._next_sweep
        if self._deadline is not None:
            target = min(target, self._deadline)
        loop = asyncio.get_running_loop()
        self.status.next_run_at = _utcnow() + timedelta(seconds=target - loop.time())

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        await self._run_guarded()
        self._next_sweep = loop.time() + self.sweep_s
        self._update_next_run_estimate()
        while True:
            now = loop.time()
            timeout = self._next_sweep - now
            if self._deadline is not None:
                timeout = min(timeout, self._deadline - now)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(timeout, 0.05))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            now = loop.time()
            due = False
            if self._deadline is not None and now >= self._deadline:
                self._deadline = None
                due = True
            if now >= self._next_sweep:
                self._next_sweep = now + self.sweep_s
                due = True
            if due:
                await self._run_guarded()
            self._update_next_run_estimate()


class IntervalWorker(_LoopWorker):
    """Fixed-interval loop with no external wake-up: run, sleep
    `interval_s`, repeat forever. For workers nothing else in the app
    needs to react to (unlike the poke/sweep trio, which chain off
    ingest/each other) -- a plain periodic wake is enough.
    """

    def __init__(
        self,
        task_name: str,
        log: logging.Logger,
        failure_message: str,
        interval_s: float,
    ):
        super().__init__(task_name, log, failure_message)
        self.interval_s = interval_s

    async def _loop(self) -> None:
        while True:
            await self._run_guarded()
            self.status.next_run_at = _utcnow() + timedelta(seconds=self.interval_s)
            await asyncio.sleep(self.interval_s)
