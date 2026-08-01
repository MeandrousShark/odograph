"""Quarterly, opt-in ntfy reminder to log an odometer reading.
Reconciliation (app/odometer.py) is only as good as the readings behind it,
so this nags at the start of each calendar quarter — aligning with quarterly
estimated taxes — when an active vehicle has gone the whole new quarter
without one.

Modeled closely on `app.nudge.NudgeWorker` (same advisory-lock +
ledger-insert-whether-or-not-sent shape, `IntervalWorker` base) but against
its own table (`odometer_reminder_windows`) and its own worker class, never
touching `app/nudge.py` — this feature was added as purely additive to the
already-deployed weekly-unclassified-nudge path. That's also why
`publish_reminder` below duplicates `publish_nudge`'s small
POST/headers/auth shape instead of importing it: `publish_nudge` hardwires
its content to `nudge_message`'s unclassified-trip wording, so reusing it
for a different message would mean changing `app/nudge.py` itself.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from psycopg_pool import AsyncConnectionPool

from app.odometer import latest_quarter_start, vehicles_due_for_reminder
from app.worker import IntervalWorker

log = logging.getLogger(__name__)

# Daily, not hourly like NudgeWorker: a quarter boundary only moves once
# every ~13 weeks, so hourly precision buys nothing here.
RUN_INTERVAL_S = 24 * 60 * 60.0

ADVISORY_LOCK_KEY = 901406  # distinct from nudge.py's 901405


def reminder_message(vehicle_names: list[str], app_url: str) -> str:
    """Names the due vehicle(s) rather than a bare count (unlike
    `nudge.nudge_message`): there are usually only one or two vehicles, and
    naming them turns this into an actionable checklist instead of a vague
    nag. Still no location data, matching `nudge_message`'s lock-screen-safe
    reasoning.
    """
    noun = "vehicle" if len(vehicle_names) == 1 else "vehicles"
    message = f"Odograph: log an odometer reading for {', '.join(vehicle_names)} ({noun})."
    if app_url:
        message += f"\n{app_url.rstrip('/')}/settings"
    return message


async def publish_reminder(
    http_client: httpx.AsyncClient, ntfy_url: str, topic: str, token: str,
    username: str, password: str, vehicle_names: list[str], app_url: str,
) -> None:
    headers = {"Title": "Odograph", "Tags": "car", "Priority": "default"}
    auth = None
    if username and password:
        auth = httpx.BasicAuth(username, password)
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    response = await http_client.post(
        f"{ntfy_url.rstrip('/')}/{topic}", content=reminder_message(vehicle_names, app_url),
        headers=headers, auth=auth,
    )
    response.raise_for_status()


class OdometerReminderWorker(IntervalWorker):
    """Daily eligibility checker for the quarterly odometer reminder.
    `IntervalWorker` (app/worker.py) supplies the run/sleep/repeat loop,
    `start`/`stop`, and guarded-run wrapper.

    Same advisory-lock-spans-the-POST reasoning as `NudgeWorker`: it
    serializes replicas through the completion insert without leaving a
    permanently-pending row when ntfy is temporarily down, at the cost of
    possible at-least-once delivery in the narrow process-dies-mid-POST
    window — unavoidable without a receiver-supported idempotency protocol,
    and already accepted for the weekly nudge.
    """

    def __init__(
        self, pool: AsyncConnectionPool, http_client: httpx.AsyncClient,
        ntfy_url: str, topic: str, token: str, username: str, password: str,
        app_url: str, display_tz, hour: int,
    ):
        super().__init__(
            task_name="odometer-reminder-worker",
            log=log,
            failure_message="odometer reminder worker run failed; will retry tomorrow",
            interval_s=RUN_INTERVAL_S,
        )
        self.pool = pool
        self.http = http_client
        self.ntfy_url = ntfy_url
        self.topic = topic
        self.token = token
        self.username = username
        self.password = password
        self.app_url = app_url
        self.display_tz = display_tz
        self.hour = hour

    async def run_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.display_tz)
        quarter_start = latest_quarter_start(now.astimezone(self.display_tz), self.hour)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
                existing = await conn.execute(
                    "SELECT 1 FROM odometer_reminder_windows WHERE quarter_starts_at = %s",
                    (quarter_start,),
                )
                if await existing.fetchone():
                    return
                vehicles_cur = await conn.execute(
                    "SELECT id, name FROM vehicles WHERE active ORDER BY name"
                )
                active_vehicles = await vehicles_cur.fetchall()
                readings_cur = await conn.execute(
                    "SELECT DISTINCT vehicle_id FROM odometer_readings WHERE recorded_at >= %s",
                    (quarter_start,),
                )
                logged_ids = {row[0] for row in await readings_cur.fetchall()}
                due = vehicles_due_for_reminder(active_vehicles, logged_ids)
                if due:
                    await publish_reminder(
                        self.http, self.ntfy_url, self.topic, self.token, self.username,
                        self.password, due, self.app_url,
                    )
                await conn.execute(
                    "INSERT INTO odometer_reminder_windows (quarter_starts_at, reminded) "
                    "VALUES (%s, %s)",
                    (quarter_start, bool(due)),
                )
        if due:
            log.info("odometer reminder: delivered for %d vehicle(s)", len(due))
        else:
            log.info("odometer reminder: no vehicles due this quarter")
