"""Hourly email digests: weekly unclassified-trip nudge, monthly summary,
year-end filing reminder, and quarterly odometer reminder. One worker, one
ledger (`email_deliveries`), four kinds -- two near-identical ledgers made
sense for the first two ntfy reminders (app/nudge.py,
app/odometer_reminder.py), but the third-through-sixth reminder generalizes
instead of copying the pattern again.

This module imports only the ntfy modules' pure scheduling helpers
(`nudge.latest_window_end`, `odometer.latest_quarter_start`), never the
workers themselves, so the deployed ntfy paths stay byte-identical (email
was added as purely additive). The weekly window start (`window_end - 7
days`) is inlined rather than routed through `nudge.nudge_date_range` (a
date-granularity helper for a different caller) so it matches
`NudgeWorker.run_once`'s timestamptz-precise window exactly.

Each kind is evaluated in its *own* transaction (see `run_once` below): one
kind's SMTP outage must not stop the others from being evaluated, or leave
their ledger rows unwritten because an earlier kind's `Mailer.send()` raised.
"""
from __future__ import annotations

import calendar
import logging
import pathlib
from datetime import date, datetime, timedelta

import jinja2
from psycopg_pool import AsyncConnectionPool

from app.db import EMAIL_DIGEST_ADVISORY_LOCK_KEY
from app.formatting import format_miles, format_usd
from app.mailer import Mailer
from app.notifications import count_unclassified_trips, odometer_reminder_vehicles
from app.nudge import latest_window_end
from app.odometer import latest_quarter_start
from app.report import build_annual_report, build_range_report
from app.ui import _fetch_range_trips_in
from app.worker import IntervalWorker

log = logging.getLogger(__name__)

RUN_INTERVAL_S = 60 * 60.0

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent / "templates" / "email"
_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
    trim_blocks=True, lstrip_blocks=True, autoescape=False,
)

MONTH_ABBR = calendar.month_abbr  # ['', 'Jan', ..., 'Dec']


def _render(template_name: str, **context) -> str:
    return _ENV.get_template(template_name).render(**context)


def _url(app_url: str, path: str) -> str:
    """Empty when `APP_URL` is unset, same convention as
    `app.nudge.nudge_message`: a link is a convenience, never a delivery
    dependency, and every template below is written to read fine without it.
    """
    return f"{app_url}{path}" if app_url else ""


def _calendar_month_days(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def latest_month_boundary(now: datetime, hour: int) -> datetime:
    """Return the latest first-of-month-at-`hour` boundary in `now`'s
    timezone that is <= `now` -- the same "latest local boundary <= now"
    shape as `app.nudge.latest_window_end`/`app.odometer.latest_quarter_start`,
    so a missed month (e.g. the app was down through the 1st) is caught by
    the next hourly run rather than skipped. `covered_month` below turns
    this boundary into the calendar month it reports *on* (the one that
    just ended), which is always the boundary's month minus one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidate = datetime(now.year, now.month, 1, hour, tzinfo=now.tzinfo)
    if now >= candidate:
        return candidate
    prev_month = now.month - 1
    prev_year = now.year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    return datetime(prev_year, prev_month, 1, hour, tzinfo=now.tzinfo)


def covered_month(period_end: datetime) -> tuple[int, int]:
    """`(year, month)` of the calendar month that ended at `period_end` --
    e.g. a period_end of 2026-08-01 covers July 2026; a period_end of
    2027-01-01 covers December 2026 (the year-boundary case this is unit
    tested against).
    """
    month = period_end.month - 1
    year = period_end.year
    if month == 0:
        month = 12
        year -= 1
    return year, month


def _parse_mmdd(mmdd: str) -> tuple[int, int]:
    """Parse `EMAIL_FILING_REMINDER_MMDD` ("01-15") into `(month, day)`.
    Raises `ValueError` (uncaught -- a malformed config value should fail
    loudly at worker construction/first run, not silently pick some other
    date) if it isn't exactly `MM-DD`.
    """
    month_str, day_str = mmdd.split("-")
    month, day = int(month_str), int(day_str)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"invalid EMAIL_FILING_REMINDER_MMDD {mmdd!r}")
    return month, day


def latest_filing_reminder_at(now: datetime, mmdd: str, hour: int) -> datetime:
    """Return the latest `mmdd`-at-`hour` boundary in `now`'s timezone that
    is <= `now`. The filing reminder always covers the *prior* calendar
    year (`period_end.year - 1`), so this only needs to find the boundary
    itself -- unlike `latest_month_boundary`, there's no "which period does
    this cover" step, since MM-DD boundaries occur once a year and their
    covered year is always the boundary's year minus one by definition.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    month, day = _parse_mmdd(mmdd)
    candidate = datetime(now.year, month, day, hour, tzinfo=now.tzinfo)
    if now >= candidate:
        return candidate
    return datetime(now.year - 1, month, day, hour, tzinfo=now.tzinfo)


class EmailDigestWorker(IntervalWorker):
    """Hourly eligibility checker for all four email digest kinds.
    `IntervalWorker` (app/worker.py) supplies the
    run/sleep/repeat loop, `start`/`stop`, and guarded-run wrapper -- but
    that wrapper's single try/except is the wrong granularity here (it
    would let one kind's exception stop the rest from being evaluated), so
    `run_once` below does its own per-kind try/except and never lets an
    exception escape to `IntervalWorker`'s guard at all.

    Each kind's own `_run_*` method opens its own pool connection and
    transaction, spanning the advisory lock, the ledger check, and (inside
    the lock) the `Mailer.send()` call -- same at-least-once-window
    tradeoff `NudgeWorker`/`OdometerReminderWorker` already accept: a
    process dying between a successful send and the ledger insert can
    still produce a duplicate email, but ordinary restarts and concurrent
    replicas cannot, since the advisory lock serializes them through that
    insert.
    """

    def __init__(
        self, pool: AsyncConnectionPool, mailer: Mailer, app_url: str, display_tz,
        nudge_weekly_hour: int, odometer_reminder_hour: int, digest_hour: int,
        filing_reminder_mmdd: str,
        email_weekly_nudge: bool, email_monthly_summary: bool,
        email_filing_reminder: bool, email_odometer_reminder: bool,
    ):
        super().__init__(
            task_name="email-digest-worker",
            log=log,
            failure_message="email digest worker run failed; will retry next hour",
            interval_s=RUN_INTERVAL_S,
        )
        self.pool = pool
        self.mailer = mailer
        self.app_url = app_url
        self.display_tz = display_tz
        self.nudge_weekly_hour = nudge_weekly_hour
        self.odometer_reminder_hour = odometer_reminder_hour
        self.digest_hour = digest_hour
        self.filing_reminder_mmdd = filing_reminder_mmdd
        self.email_weekly_nudge = email_weekly_nudge
        self.email_monthly_summary = email_monthly_summary
        self.email_filing_reminder = email_filing_reminder
        self.email_odometer_reminder = email_odometer_reminder

    async def run_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.display_tz)
        local_now = now.astimezone(self.display_tz)
        if self.email_weekly_nudge:
            await self._guarded("weekly_nudge", self._run_weekly_nudge, local_now)
        if self.email_monthly_summary:
            await self._guarded("monthly_summary", self._run_monthly_summary, local_now)
        if self.email_filing_reminder:
            await self._guarded("filing_reminder", self._run_filing_reminder, local_now)
        if self.email_odometer_reminder:
            await self._guarded("quarterly_odometer", self._run_quarterly_odometer, local_now)

    async def _guarded(self, kind: str, run, now: datetime) -> None:
        """Isolate one kind's failure from the others: the transaction it
        ran in already rolled back on the raise (no ledger row), so all
        that's left to do here is make sure the root-cause exception still
        reaches the log instead of vanishing, and that the loop in
        `run_once` keeps going.

        Also records the failure onto `self.status` directly: this
        exception never reaches `IntervalWorker._run_guarded()` (that's the
        whole point of this guard), so without this call a kind stuck
        failing every hour would look identical to a healthy worker in
        diagnostics -- `last_success_at` still advances every run because
        `run_once` itself never raises.
        """
        try:
            await run(now)
        except Exception as exc:
            self.status.record_failure(exc)
            log.exception("email digest: %s failed; will retry next hour", kind)

    async def _already_delivered(self, conn, kind: str, period_end: datetime) -> bool:
        """Take the shared advisory lock, then check the ledger. Callers keep
        the lock (their transaction) through send + `_record_delivery`, which
        is what serializes concurrent replicas through the insert.
        """
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (EMAIL_DIGEST_ADVISORY_LOCK_KEY,)
        )
        cur = await conn.execute(
            "SELECT 1 FROM email_deliveries WHERE kind = %s AND period_end = %s",
            (kind, period_end),
        )
        return await cur.fetchone() is not None

    async def _record_delivery(self, conn, kind: str, period_end: datetime, sent: bool) -> None:
        await conn.execute(
            "INSERT INTO email_deliveries (kind, period_end, sent) VALUES (%s, %s, %s)",
            (kind, period_end, sent),
        )

    async def _run_weekly_nudge(self, now: datetime) -> None:
        window_end = latest_window_end(now, self.nudge_weekly_hour)
        window_start = window_end - timedelta(days=7)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if await self._already_delivered(conn, "weekly_nudge", window_end):
                    return
                trip_count = await count_unclassified_trips(
                    conn, window_start, window_end
                )
                if trip_count:
                    body = _render(
                        "weekly_nudge.txt",
                        count=trip_count,
                        noun="trip" if trip_count == 1 else "trips",
                        review_url=_url(self.app_url, "/review"),
                    )
                    await self.mailer.send(
                        self.mailer.compose("Odograph: weekly unclassified-trip digest", body)
                    )
                await self._record_delivery(conn, "weekly_nudge", window_end, bool(trip_count))
        if trip_count:
            log.info("email digest: weekly_nudge sent for %d unclassified trip(s)", trip_count)
        else:
            log.info("email digest: weekly_nudge no unclassified trips for this window")

    async def _run_monthly_summary(self, now: datetime) -> None:
        period_end = latest_month_boundary(now, self.digest_hour)
        year, month = covered_month(period_end)
        month_start, month_end = _calendar_month_days(year, month)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if await self._already_delivered(conn, "monthly_summary", period_end):
                    return
                trips, rates = await _fetch_range_trips_in(conn, self.display_tz, month_start, month_end)
                report = build_range_report(trips, rates, self.display_tz, month_start, month_end)
                body = _render(
                    "monthly_summary.txt",
                    month_label=f"{MONTH_ABBR[month]} {year}",
                    business_mi=format_miles(report.business_m),
                    deduction=format_usd(report.total_deduction),
                    unclassified=report.caveats.unclassified_trips,
                    report_url=_url(
                        self.app_url,
                        f"/report/range?from={month_start.isoformat()}&to={month_end.isoformat()}",
                    ),
                )
                await self.mailer.send(
                    self.mailer.compose(f"Odograph: {MONTH_ABBR[month]} summary", body)
                )
                await self._record_delivery(conn, "monthly_summary", period_end, True)
        log.info("email digest: monthly_summary sent for %s %d", MONTH_ABBR[month], year)

    async def _run_filing_reminder(self, now: datetime) -> None:
        period_end = latest_filing_reminder_at(now, self.filing_reminder_mmdd, self.digest_hour)
        year = period_end.year - 1
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if await self._already_delivered(conn, "filing_reminder", period_end):
                    return
                trips, rates = await _fetch_range_trips_in(
                    conn, self.display_tz, date(year, 1, 1), date(year, 12, 31)
                )
                report = build_annual_report(trips, rates, self.display_tz, year)
                body = _render(
                    "filing_reminder.txt",
                    year=year,
                    business_mi=format_miles(report.business_m),
                    deduction=format_usd(report.total_deduction),
                    report_url=_url(self.app_url, f"/report/{year}"),
                    export_url=_url(self.app_url, f"/report/{year}/export"),
                )
                await self.mailer.send(
                    self.mailer.compose(f"Odograph: {year} filing reminder", body)
                )
                await self._record_delivery(conn, "filing_reminder", period_end, True)
        log.info("email digest: filing_reminder sent for %d", year)

    async def _run_quarterly_odometer(self, now: datetime) -> None:
        quarter_start = latest_quarter_start(now, self.odometer_reminder_hour)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if await self._already_delivered(conn, "quarterly_odometer", quarter_start):
                    return
                due = await odometer_reminder_vehicles(conn, quarter_start)
                if due:
                    body = _render(
                        "quarterly_odometer.txt",
                        vehicles=", ".join(due),
                        noun="vehicle" if len(due) == 1 else "vehicles",
                        settings_url=_url(self.app_url, "/settings"),
                    )
                    await self.mailer.send(
                        self.mailer.compose("Odograph: log an odometer reading", body)
                    )
                await self._record_delivery(conn, "quarterly_odometer", quarter_start, bool(due))
        if due:
            log.info("email digest: quarterly_odometer sent for %d vehicle(s)", len(due))
        else:
            log.info("email digest: quarterly_odometer no vehicles due this quarter")
