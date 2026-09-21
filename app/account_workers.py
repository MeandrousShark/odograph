"""Enumerate eligible identities through control, then run owned jobs."""
from __future__ import annotations

import logging

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_settings import config_for_account, load_account_settings
from app.worker import PokeSweepWorker, RUN_SKIPPED

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
    async with control_connection(control_pool) as conn:
        cur = await conn.execute(
            "SELECT id,is_enabled,auth_version FROM accounts WHERE is_enabled ORDER BY id")
        return [AccountPrincipal(*row) for row in await cur.fetchall()]


class AccountWorker(PokeSweepWorker):
    """Each account gets an independent job and immutable configuration.

    A failed account does not roll back another account's completed work.
    poke() (inherited from PokeSweepWorker) takes no target: every sweep
    already revalidates every enabled principal, so there is nothing to
    narrow a wakeup to.
    """

    def __init__(self, pools, config, factory, *, label, debounce_s, sweep_s, after_run=None):
        super().__init__(task_name=label, log=log,
            failure_message=f"{label}: account enumeration failed",
            debounce_s=debounce_s, sweep_s=sweep_s)
        self.pools = pools
        self.config = config
        self.factory = factory
        self.after_run = after_run

    async def run_once(self):
        ran = False
        failed = False
        for principal in await enabled_principals(self.pools.control):
            try:
                pool = AccountPool(self.pools.runtime, principal)
                async with pool.connection() as conn:
                    settings = await load_account_settings(conn)
                config = config_for_account(self.config, settings)
                worker = self.factory(pool, config)
                if worker is None:
                    continue
                result = await worker.run_once()
                ran |= not _did_not_run(result)
                # Not every wrapped job is a _LoopWorker: DetectorRunner owns
                # no WorkerStatus and reports failure by raising, which the
                # clause below already records. Reading `.status` off it
                # unconditionally turns every sweep, successful or not, into
                # an AttributeError logged as an account-job failure.
                status = getattr(worker, "status", None)
                if status is not None and status.last_failure_at is not None:
                    failed = True
                    self.status.last_failure_at = status.last_failure_at
                    self.status.last_failure_type = status.last_failure_type
            except Exception as exc:
                failed = True
                # Provider exceptions can contain private URLs/coordinates.
                log.warning("%s: account job failed (%s)", self._task_name, type(exc).__name__)
                self.status.record_failure(exc)
        return _PARTIAL_FAILURE if failed else (None if ran else RUN_SKIPPED)

    async def _run_guarded(self):
        self.status.record_run()
        try:
            result = await self.run_once()
            if result is RUN_SKIPPED:
                self.status.record_skip()
                return
            await self.after_run_once(result)
        except Exception as exc:
            self.status.record_failure(exc)
            log.warning("%s: sweep failed (%s)", self._task_name, type(exc).__name__)
        else:
            if result is not _PARTIAL_FAILURE:
                self.status.record_success()

    async def after_run_once(self, result):
        if self.after_run is not None:
            self.after_run()
