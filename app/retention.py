"""Background pruning of `raw_messages`.

`raw_messages` stores every accepted OwnTracks POST body verbatim, purely as
insurance for rebuilding `points` from scratch after a parsing bug or a
detector change. Points are derived from a raw message within the ingest
request itself, and the detector reprocesses any device within its debounce
window (seconds) or catch-up sweep (minutes), so by the time a raw message
is old enough to be a pruning candidate, everything derivable from it has
long since been materialized into `points`/`stays`/`trips`. Deleting it loses
only the insurance value, not anything live.

Kept conservative and reversible: age-based only (no volume cap, no
per-device logic), a long default (RAW_MESSAGE_RETENTION_DAYS=365, `_f`'d in
app/config.py), and a `<= 0` value disables the job outright. The off
switch, for anyone who wants the insurance kept indefinitely.
"""
from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from app.account_context import account_id
from app.worker import BatchOutcome, TurnOutcome

log = logging.getLogger(__name__)


class RetentionWorker:
    """Deletes `raw_messages` rows older than the configured retention
    window, in ordered transactions of at most 1,000 rows. AccountWorker
    rotates accounts between run_turn() units, draining ready backlog promptly.
    Idle accounts retain the daily sweep cadence.
    """

    def __init__(self, pool: AsyncConnectionPool, retention_days: float):
        self.pool = pool
        self.retention_days = retention_days

    async def run_once(self) -> None:
        await self.run_turn()

    async def run_turn(self, cursor=None) -> TurnOutcome:
        if self.retention_days <= 0:
            return TurnOutcome()
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "WITH expired AS (SELECT id FROM raw_messages WHERE account_id = %s "
                "AND received_at < now() - %s * interval '1 day' "
                "ORDER BY received_at,id LIMIT 1000) "
                "DELETE FROM raw_messages r USING expired e WHERE r.account_id = %s AND r.id=e.id",
                (account_id(conn), self.retention_days, account_id(conn)),
            )
            deleted = cur.rowcount
        log.info(
            "retention: pruned %d raw_messages row(s) older than %s day(s)",
            deleted, self.retention_days,
        )
        return TurnOutcome(batch=BatchOutcome(deleted, deleted), ready=deleted == 1000)
