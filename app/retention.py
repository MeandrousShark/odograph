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

log = logging.getLogger(__name__)


class RetentionWorker:
    """Deletes `raw_messages` rows older than the configured retention
    window. `run_once()` is the only method `AccountWorker`
    (app/account_workers.py) calls -- it builds a fresh `RetentionWorker`
    per account on its own daily-cadence sweep; this class supplies no
    loop, `start`/`stop`, or guarded-run wrapper of its own.
    """

    def __init__(self, pool: AsyncConnectionPool, retention_days: float):
        self.pool = pool
        self.retention_days = retention_days

    async def run_once(self) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM raw_messages WHERE account_id = %s AND received_at < now() - %s * interval '1 day'",
                (account_id(conn), self.retention_days),
            )
            deleted = cur.rowcount
        log.info(
            "retention: pruned %d raw_messages row(s) older than %s day(s)",
            deleted, self.retention_days,
        )
