"""Enumerate eligible identities through control, then run owned jobs."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_work import external_account_work
from app.capacity import AdmissionManager, CapacityBusy
from app.account_settings import config_for_account, load_account_settings
from app.worker import BatchOutcome, PokeSweepWorker, RUN_SKIPPED, TurnOutcome

log = logging.getLogger(__name__)
_PARTIAL_FAILURE = object()


def _did_not_run(result) -> bool:
    """True when a wrapped worker reports that it never did its work.

    `RUN_SKIPPED` is the shared sentinel every worker in app/worker.py uses.
    `DetectorRunner.run_once()` predates that sentinel and still reports
    advisory-lock contention as a plain `False`, which is not `RUN_SKIPPED`:
    counting it as a run would record `last_success_at` on a sweep that never
    held the lock, never record `last_skip_at` again, and still fire
    `after_run` to poke the snap/geocode workers. Both values mean the same
    thing here, so both must land as a skip.
    """
    return result is RUN_SKIPPED or result is False


async def enabled_principals(control_pool) -> list[AccountPrincipal]:
    async with control_connection(control_pool, lane="identity") as conn:
        cur = await conn.execute(
            "SELECT id,is_enabled,auth_version FROM accounts WHERE is_enabled ORDER BY id")
        return [AccountPrincipal(*row) for row in await cur.fetchall()]


class BackgroundScheduler:
    """Rotate ready worker types, with one registration per type and no fan-out."""

    def __init__(self, capacity):
        self.capacity = capacity
        self._ready: deque[tuple[str, asyncio.Future]] = deque()
        self._registrations: set[str] = set()
        self._active: str | None = None

    def _advance(self):
        while self._active is None and self._ready:
            label, ready = self._ready.popleft()
            if ready.cancelled():
                continue
            self._active = label
            ready.set_result(None)

    @asynccontextmanager
    async def turn(self, label, principal):
        if label in self._registrations:
            raise RuntimeError("background type already registered")
        if len(self._registrations) >= 7:
            raise RuntimeError("too many background worker types")
        ready = asyncio.get_running_loop().create_future()
        self._registrations.add(label)
        self._ready.append((label, ready))
        self._advance()
        try:
            await ready
            async with self.capacity.operation("background", principal=principal, registration=label):
                yield
        finally:
            self._registrations.discard(label)
            if self._active == label:
                self._active = None
            else:
                self._ready = deque(item for item in self._ready if item[1] is not ready)
            self._advance()


class AccountWorker(PokeSweepWorker):
    """Rotate atomic account units with fresh immutable configuration.

    One account failure leaves other committed units intact. Enabled accounts
    are enumerated at round boundaries, with no task per account or backlog row.
    """

    def __init__(self, pools, config, factory, *, label, debounce_s, sweep_s, after_run=None,
                 capacity=None, scheduler=None, before_turn=None, refresh_deferred_on_wake=False,
                 admitted_turn=None):
        super().__init__(task_name=label, log=log,
            failure_message=f"{label}: account enumeration failed",
            debounce_s=debounce_s, sweep_s=sweep_s)
        self.capacity = (capacity if capacity is not None else
                         getattr(pools.runtime, "capacity", None) or AdmissionManager(config))
        self.scheduler = scheduler if scheduler is not None else BackgroundScheduler(self.capacity)
        self._last_account_started: int | None = None
        self.pools = pools
        self.config = config
        self.factory = factory
        self.after_run = after_run
        self.last_outcome = BatchOutcome()
        self._produced_work = False
        self.before_turn = before_turn
        self.admitted_turn = admitted_turn
        self.refresh_deferred_on_wake = refresh_deferred_on_wake
        self._round: deque[AccountPrincipal] = deque()
        self._last_round_started: int | None = None
        self._continuations: dict[int, TurnOutcome] = {}

    def wake_cycle(self):
        # Durable jobs recheck due times because a wake may introduce earlier work.
        for owner, continuation in self._continuations.items():
            if continuation.deferred_until is None or self.refresh_deferred_on_wake:
                self._continuations[owner] = TurnOutcome(ready=True, cursor=continuation.cursor)

    def _schedule_continuation(self):
        now = asyncio.get_running_loop().time()
        deadlines = [now if outcome.ready else outcome.deferred_until
                     for outcome in self._continuations.values()
                     if outcome.ready or outcome.deferred_until is not None]
        self._continuation_at = min(deadlines) if deadlines else None

    async def _run_admitted(self, principal, cursor, *, legacy=False):
        self._admitted_failed = False
        if self.admitted_turn is not None:
            return await self.admitted_turn(self, principal, cursor, legacy=legacy)
        return await self._default_admitted(principal, cursor, legacy=legacy)

    async def _default_admitted(self, principal, cursor, *, legacy=False):
        async with external_account_work(self.pools.control, principal.account_id):
            pool = AccountPool(self.pools.runtime, principal)
            async with pool.connection() as conn:
                settings = await load_account_settings(conn)
            config = config_for_account(self.config, settings)
            worker = self.factory(pool, config)
            if worker is None:
                return TurnOutcome(skipped=legacy)
            try:
                if not legacy and hasattr(worker, "run_turn"):
                    result = await worker.run_turn(cursor)
                else:
                    value = await worker.run_once()
                    result = TurnOutcome(
                        batch=value if isinstance(value, BatchOutcome) else BatchOutcome(),
                        skipped=_did_not_run(value),
                    )
            finally:
                produced = bool(getattr(worker, "produced_work", False))
                self._produced_work = (self._produced_work or produced) if legacy else produced
            status = getattr(worker, "status", None)
            if status is not None and status.last_failure_at is not None:
                self._admitted_failed = True
                self.status.last_failure_at = status.last_failure_at
                self.status.last_failure_type = status.last_failure_type
            return result

    async def run_turn(self):
        """Rotate accounts after one atomic unit, reconstructing each round live."""
        self.last_outcome = BatchOutcome()
        self._produced_work = False
        now = asyncio.get_running_loop().time()
        if not self._round:
            principals = await enabled_principals(self.pools.control)
            enabled = {p.account_id for p in principals}
            self._continuations = {owner: outcome for owner, outcome in self._continuations.items()
                                   if owner in enabled}
            if self._last_round_started is not None:
                principals = ([p for p in principals if p.account_id > self._last_round_started]
                              + [p for p in principals if p.account_id <= self._last_round_started])
            for principal in principals:
                state = self._continuations.setdefault(principal.account_id, TurnOutcome(ready=True))
                if state.ready or (state.deferred_until is not None and state.deferred_until <= now):
                    self._round.append(principal)
            if self._round:
                self._last_round_started = self._round[0].account_id
        if not self._round:
            self._schedule_continuation()
            return TurnOutcome(deferred_until=self._continuation_at)
        principal = self._round.popleft()
        owner = principal.account_id
        previous = self._continuations[owner]
        result = TurnOutcome()
        cursor = previous.cursor
        admitted = False
        try:
            if self.before_turn is not None:
                cursor = await self.before_turn(principal, cursor)
            async with self.scheduler.turn(self._task_name, principal):
                admitted = True
                self._last_account_started = owner
                result = await self._run_admitted(principal, cursor)
            self.last_outcome = result.batch
            if result.batch.retriable_failures:
                self.status.record_failure_type(result.batch.failure_type or "RetriableFailure")
        except CapacityBusy as exc:
            if admitted:
                self.status.record_failure(exc)
            result = TurnOutcome(cursor=previous.cursor, skipped=not admitted)
        except Exception as exc:
            log.warning("%s: account job failed (%s)", self._task_name, type(exc).__name__)
            self.status.record_failure(exc)
            result = TurnOutcome(cursor=previous.cursor)
        finally:
            if self.before_turn is not None and hasattr(cursor, "close"):
                cursor.close()
        self._continuations[owner] = result
        self._schedule_continuation()
        # Unvisited accounts in this round remain ready even if this account went idle.
        if self._round:
            self._continuation_at = asyncio.get_running_loop().time()
        return result

    async def run_once(self):
        """Legacy explicit sweep; the serving loop uses run_turn instead."""
        ran = False
        failed = False
        self.last_outcome = BatchOutcome()
        self._produced_work = False
        principals = await enabled_principals(self.pools.control)
        if self._last_account_started is not None:
            principals = ([p for p in principals if p.account_id > self._last_account_started]
                          + [p for p in principals if p.account_id <= self._last_account_started])
        first_start = True
        for principal in principals:
            admitted = False
            try:
                async with self.scheduler.turn(self._task_name, principal):
                    admitted = True
                    if first_start:
                        self._last_account_started = principal.account_id
                        first_start = False
                    outcome = await self._run_admitted(principal, None, legacy=True)
                    ran |= not outcome.skipped
                    self.last_outcome += outcome.batch
                    if outcome.batch.retriable_failures:
                        failed = True
                        self.status.record_failure_type(outcome.batch.failure_type or "RetriableFailure")
                    if self._admitted_failed:
                        failed = True
            except CapacityBusy as exc:
                # Preserve downstream pokes after an earlier stream committed.
                if admitted:
                    failed = True
                    self.status.record_failure(exc)
                continue
            except Exception as exc:
                failed = True
                # Provider exceptions can contain private URLs/coordinates.
                log.warning("%s: account job failed (%s)", self._task_name, type(exc).__name__)
                self.status.record_failure(exc)
        return _PARTIAL_FAILURE if failed else (None if ran else RUN_SKIPPED)

    async def _run_guarded(self):
        self.status.record_run()
        last_failure = self.status.last_failure_at
        try:
            result = await self.run_turn()
            if result.skipped:
                self.status.record_skip()
                return
            await self.after_run_once(result)
        except Exception as exc:
            self.status.record_failure(exc)
            log.warning("%s: sweep failed (%s)", self._task_name, type(exc).__name__)
        else:
            if self.status.last_failure_at == last_failure:
                self.status.record_success()

    async def after_run_once(self, result):
        if self.after_run is not None and self._produced_work:
            self.after_run()
