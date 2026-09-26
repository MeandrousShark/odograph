"""Bounded, periodic pruning of account security audit rows."""
from __future__ import annotations

import logging

from app.account_context import control_connection
from app.worker import PokeSweepWorker

log = logging.getLogger(__name__)


class AuditRetentionWorker(PokeSweepWorker):
    def __init__(self, control_pool):
        super().__init__(
            task_name="account-audit-retention",
            log=log,
            failure_message="account audit retention failed",
            debounce_s=1,
            sweep_s=3600,
        )
        self.control_pool = control_pool

    async def run_once(self) -> None:
        async with control_connection(self.control_pool) as conn:
            cur = await conn.execute("SELECT public.prune_account_security_audit()")
            deleted = (await cur.fetchone())[0]
        if deleted:
            log.info("account audit retention pruned %d row(s)", deleted)
