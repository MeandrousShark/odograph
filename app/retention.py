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

from app.worker import IntervalWorker
from app.account_context import account_id

log = logging.getLogger(__name__)

# No poke/debounce here, since nothing else in the app needs to react to this
# job, unlike the detector, which pokes SnapWorker and GeocodeWorker after
# each sweep that does something (app/main.py's after_detection). A plain
# daily wake keeps the growth of a slow, low-priority prune bounded without
# a dedicated cadence env var.
RUN_INTERVAL_S = 24 * 60 * 60.0


class RetentionWorker(IntervalWorker):
    """Daily loop that deletes `raw_messages` rows older than the configured
    retention window. `IntervalWorker` (app/worker.py) supplies the
    run/sleep/repeat loop, `start`/`stop`, and guarded-run wrapper, with no
    debounce/sweep distinction, since nothing pokes this worker early.
    """

    def __init__(self, pool: AsyncConnectionPool, retention_days: float):
        super().__init__(
            task_name="retention-worker",
            log=log,
            failure_message="retention worker run failed; will retry on next daily wake",
            interval_s=RUN_INTERVAL_S,
        )
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
