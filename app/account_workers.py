"""Enumerate eligible identities through control, then run owned jobs."""
from __future__ import annotations

import logging

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_settings import config_for_account, load_account_settings
from app.worker import PokeSweepWorker, RUN_SKIPPED

log = logging.getLogger(__name__)
_PARTIAL_FAILURE = object()


async def enabled_principals(control_pool) -> list[AccountPrincipal]:
    async with control_connection(control_pool) as conn:
        cur = await conn.execute(
            "SELECT id,is_enabled,auth_version FROM accounts WHERE is_enabled ORDER BY id")
        return [AccountPrincipal(*row) for row in await cur.fetchall()]


class AccountWorker(PokeSweepWorker):
    """Each account gets an independent job and immutable configuration.

    A failed account does not roll back another account's completed work.
    Queued wakeups contain stable identity only; every sweep revalidates it.
    """

    def __init__(self, pools, config, factory, *, label, debounce_s, sweep_s, after_run=None):
        super().__init__(task_name=label, log=log,
            failure_message=f"{label}: account enumeration failed",
            debounce_s=debounce_s, sweep_s=sweep_s)
        self.pools = pools
        self.config = config
        self.factory = factory
        self.after_run = after_run
        self._wake_keys: set[tuple[int, int | None]] = set()

    def poke(self, account_id: int | None = None, tracking_device_id: int | None = None):
        if account_id is not None:
            self._wake_keys.add((account_id, tracking_device_id))
        super().poke()

    async def run_once(self):
        self._wake_keys.clear()
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
                ran |= result is not RUN_SKIPPED
                if worker.status.last_failure_at is not None:
                    failed = True
                    self.status.last_failure_at = worker.status.last_failure_at
                    self.status.last_failure_type = worker.status.last_failure_type
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
