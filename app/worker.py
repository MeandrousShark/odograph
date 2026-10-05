"""Shared background-worker loop machinery.

AccountWorker runs one account unit per scheduler turn. TurnOutcome carries
ready, deferred or idle continuation state; ready work drains through FIFO
rotation while pokes and periodic sweeps refresh idle accounts. Inner workers
supply run_turn(), with run_once() retained for direct callers. The global
AuditRetentionWorker uses the same poke/sweep loop without continuations.

WorkerStatus records diagnostic history, including failures swallowed by
per-kind email guards. owned_thread retains the surrounding admission and
lease through actual CPU and SMTP completion during cancellation.
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


@dataclass(frozen=True)
class BatchOutcome:
    """Counts for one batch; a retryable failure remains eligible for later work."""

    attempted: int = 0
    completed: int = 0
    retriable_failures: int = 0
    failure_type: str | None = None

    def __add__(self, other: BatchOutcome) -> BatchOutcome:
        return BatchOutcome(
            self.attempted + other.attempted,
            self.completed + other.completed,
            self.retriable_failures + other.retriable_failures,
            self.failure_type or other.failure_type,
        )


@dataclass(frozen=True)
class TurnOutcome:
    """One committed unit and its continuation, using the loop's monotonic clock."""

    batch: BatchOutcome = BatchOutcome()
    ready: bool = False
    deferred_until: float | None = None
    cursor: Any = None
    skipped: bool = False


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
        self.record_failure_type(type(exc).__name__)

    def record_failure_type(self, failure_type: str) -> None:
        self.last_failure_at = _utcnow()
        self.last_failure_type = failure_type


class _LoopWorker:
    """`start`/`stop`/guarded-run bookkeeping for `PokeSweepWorker` below.
    Not meant to be used directly.
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
        self._continuation_at: float | None = None

    def wake_cycle(self) -> None:
        """Reset idle continuations when a poke or periodic sweep is due."""

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
        if self._continuation_at is not None:
            target = min(target, self._continuation_at)
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
            if self._continuation_at is not None:
                timeout = min(timeout, self._continuation_at - now)
            try:
                if timeout <= 0:
                    await asyncio.sleep(0)
                else:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            now = loop.time()
            due = False
            wake_cycle = False
            if self._deadline is not None and now >= self._deadline:
                self._deadline = None
                due = True
                wake_cycle = True
            if now >= self._next_sweep:
                self._next_sweep = now + self.sweep_s
                due = True
                wake_cycle = True
            if self._continuation_at is not None and now >= self._continuation_at:
                self._continuation_at = None
                due = True
            if wake_cycle:
                self.wake_cycle()
            if due:
                await self._run_guarded()
            self._update_next_run_estimate()
