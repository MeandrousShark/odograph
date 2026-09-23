"""Weekly, opt-in ntfy reminders for trips that still need a human tag.

Classification is a tax-record task that is easiest while a drive is fresh,
but it must never become a delivery dependency for ingest or detection. This
worker therefore reads an already-materialized `trips` slice on its own slow
cadence and can be absent entirely when ntfy is not configured. The durable
window ledger is deliberately the source of truth rather than an in-memory
timestamp: deploy restarts and briefly overlapping app processes are normal
enough that duplicate reminders would quickly make an otherwise useful nudge
annoying.
"""
from __future__ import annotations

from app.account_context import account_id

import logging
from datetime import date, datetime, timedelta

import httpx
from psycopg_pool import AsyncConnectionPool

from app.db import NUDGE_ADVISORY_LOCK_KEY
from app.notifications import notification_preferences_current, count_unclassified_trips, publish_ntfy

log = logging.getLogger(__name__)

SUNDAY = 6


def latest_window_end(now: datetime, hour: int) -> datetime:
    """Return the latest Sunday-at-``hour`` boundary in ``now``'s timezone.

    A rolling seven-day window ending at that fixed boundary means a failed
    Sunday delivery can be retried on Monday without silently changing which
    trips the eventual reminder describes. Constructing in local time rather
    than subtracting fixed UTC seconds also keeps a daylight-saving week at
    its expected local Sunday evening.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    days_since_sunday = (now.weekday() - SUNDAY) % 7
    candidate_date = now.date() - timedelta(days=days_since_sunday)
    candidate = datetime.combine(candidate_date, datetime.min.time(), tzinfo=now.tzinfo)
    candidate = candidate.replace(hour=hour)
    return candidate if now >= candidate else candidate - timedelta(days=7)


def nudge_date_range(window_end: datetime) -> tuple[date, date]:
    """Return inclusive local dates suitable for the existing trip filter."""
    return ((window_end - timedelta(days=7)).date(), window_end.date())


def nudge_message(trip_count: int, window_end: datetime, app_url: str) -> str:
    """Keep push content intentionally non-sensitive; notifications often
    preview on lock screens, so the count is useful while locations, times,
    and route labels are not. A configured app URL is only a convenience --
    ntfy delivery itself must not depend on knowing the reverse-proxy URL.

    The link targets `/review` rather than the filtered list view: the
    reminder should land the user directly in the triage flow built for
    acting on it, not just a filtered table they still have to scroll.
    """
    noun = "trip" if trip_count == 1 else "trips"
    message = f"Odograph: {trip_count} unclassified {noun} in the past week."
    if not app_url:
        return message
    return f"{message}\n{app_url.rstrip('/')}/review"


async def publish_nudge(
    http_client: httpx.AsyncClient, ntfy_url: str, topic: str, token: str,
    username: str, password: str,
    trip_count: int, window_end: datetime, app_url: str,
) -> None:
    await publish_ntfy(
        http_client, ntfy_url, topic, token, username, password,
        nudge_message(trip_count, window_end, app_url),
    )


class NudgeWorker:
    """Hourly eligibility checker for the weekly ntfy digest. `run_once()`
    is the only method `AccountWorker` (app/account_workers.py) calls -- it
    builds a fresh `NudgeWorker` per account on its own hourly-cadence
    sweep; this class supplies no loop, `start`/`stop`, or guarded-run
    wrapper of its own.

    The Postgres advisory lock spans the small external POST intentionally:
    it serializes replicas through the completion insert without making a
    permanently pending row when ntfy is temporarily down. HTTP success is
    followed immediately by the ledger insert in the same lock scope. A
    process dying in that narrow network/DB gap can still produce at-least-
    once delivery -- unavoidable without a receiver-supported idempotency
    protocol -- but ordinary restarts and concurrent workers cannot.
    """

    def __init__(
        self, pool: AsyncConnectionPool, http_client: httpx.AsyncClient,
        ntfy_url: str, topic: str, token: str, username: str, password: str,
        app_url: str, display_tz, hour: int,
    ):
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
        window_end = latest_window_end(now.astimezone(self.display_tz), self.hour)
        window_start = window_end - timedelta(days=7)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (NUDGE_ADVISORY_LOCK_KEY,)
                )
                if not await notification_preferences_current(
                    conn, ntfy_topic=self.topic, display_tz=str(self.display_tz),
                    nudge_weekly_hour=self.hour,
                ) or not self.topic:
                    return
                existing = await conn.execute(
                    "SELECT 1 FROM nudge_delivery_windows WHERE account_id = %s AND window_ends_at = %s", (account_id(conn), window_end)
                )
                if await existing.fetchone():
                    return
                trip_count = await count_unclassified_trips(
                    conn, window_start, window_end
                )
                if trip_count:
                    await publish_nudge(
                        self.http, self.ntfy_url, self.topic, self.token, self.username, self.password,
                        trip_count, window_end, self.app_url,
                    )
                await conn.execute(
                    "INSERT INTO nudge_delivery_windows (account_id, window_ends_at, trip_count) VALUES (%s, %s, %s)",
                    (account_id(conn), window_end, trip_count),
                )
        if trip_count:
            log.info("nudge: delivered reminder for %d unclassified trip(s)", trip_count)
        else:
            log.info("nudge: no unclassified trips for this weekly window")
