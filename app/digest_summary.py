"""Exact digest totals with bounded history input and owned preparation cleanup."""
from __future__ import annotations

import asyncio
import math
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import uuid4

from psycopg import InterfaceError, OperationalError
from psycopg.rows import tuple_row

from app.account_context import account_id
from app.capacity import await_completion, owned_thread
from app.rates import YearRate
from app.report import sum_month_deductions

BATCH_SIZE = 256
PREPARATION_SECONDS = 15.0

log = logging.getLogger(__name__)


class DigestPreparationTimeout(TimeoutError):
    """The history/rate preparation attempt exhausted its stop budget."""


@dataclass(slots=True)
class DigestSummary:
    business_m: float = 0.0
    nondeductible_m: float = 0.0
    unclassified_trips: int = 0
    total_deduction: float | None = None
    business_by_month: dict[int, float] = field(default_factory=dict, repr=False)

    def fold(self, rows, tz, start: date, end: date):
        for started_at, category, exclusion, distance_m in rows:
            local = started_at.astimezone(tz)
            if not start <= local.date() <= end:
                continue
            if category == "unclassified":
                self.unclassified_trips += 1
            if exclusion == "not_my_vehicle":
                continue
            if exclusion == "not_deductible":
                self.nondeductible_m += distance_m
            elif category == "business":
                month = local.month
                self.business_by_month[month] = self.business_by_month.get(month, 0.0) + distance_m
                self.business_m += distance_m

    def finish(self, rates, year):
        self.total_deduction = sum_month_deductions(self.business_by_month.items(), year, rates)
        return self


class DigestTurn:
    """Cancel active work once while the transaction owns its actual cleanup."""

    def __init__(self):
        self.stopped = False
        self.active = None
        self.preparation_started = False

    def check(self):
        if self.stopped:
            raise asyncio.CancelledError

    def stop(self):
        self.stopped = True
        if self.active is not None and not self.active.done():
            self.active.cancel()

    async def perform(self, function, *args):
        self.check()
        self.active = asyncio.create_task(function(*args))
        try:
            return await asyncio.shield(self.active)
        finally:
            self.active = None

    async def summarize(self, conn, tz, start, end):
        self.check()
        self.preparation_started = True
        return await self.perform(fetch_digest_summary, conn, tz, start, end)


async def _retain_unconfirmed_cleanup():
    while True:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            continue


async def owned_digest_turn(function, *args):
    turn = DigestTurn()

    async def run():
        try:
            return await function(*args, turn)
        except (OperationalError, InterfaceError) as exc:
            if not turn.preparation_started or exc.sqlstate is not None:
                raise
            log.error("digest cleanup unconfirmed (%s); turn remains owned", type(exc).__name__)
            # A failed connection/rollback wait cannot prove backend work ended.
            # Keep the operation and lifecycle lease until process recovery.
            await _retain_unconfirmed_cleanup()
            raise

    task = asyncio.create_task(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        turn.stop()
        # Only active preparation or transport receives cancellation. Finalizers
        # remain owned by the protected task, even after repeated waiter cancellation.
        try:
            await await_completion(task)
        except BaseException:
            pass
        raise


async def fetch_digest_summary(conn, tz, start: date, end: date) -> DigestSummary:
    if start > end or start.year != end.year:
        raise ValueError("digest range must be ordered within one calendar year")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PREPARATION_SECONDS
    range_start = datetime(start.year, start.month, start.day, tzinfo=tz)
    next_day = end + timedelta(days=1)
    range_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    summary = DigestSummary()
    cursor = conn.cursor(name="digest_" + uuid4().hex, row_factory=tuple_row, scrollable=False, withhold=False)
    original_timeout = None

    def remaining_ms():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise DigestPreparationTimeout("digest preparation deadline expired")
        return max(1, math.floor(remaining * 1000))

    async def ceiling():
        milliseconds = remaining_ms()
        if original_timeout:
            milliseconds = min(milliseconds, original_timeout)
        await conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{milliseconds}ms",))

    try:
        try:
            async with asyncio.timeout_at(deadline):
                cur = await conn.execute("SELECT extract(epoch FROM current_setting('statement_timeout')::interval) * 1000")
                original_timeout = int((await cur.fetchone())[0])
                await ceiling()
                await cursor.execute(
                    "SELECT started_at, category, exclusion, COALESCE(distance_snapped_m, distance_m) "
                    "FROM trips WHERE account_id = %s AND started_at >= %s AND started_at < %s "
                    "ORDER BY started_at ASC, id ASC",
                    (account_id(conn), range_start, range_end),
                )
                while True:
                    await ceiling()
                    rows = await cursor.fetchmany(BATCH_SIZE)
                    remaining_ms()
                    if not rows:
                        break
                    await owned_thread(summary.fold, rows, tz, start, end)
                    del rows
                    remaining_ms()
                await ceiling()
                cur = await conn.execute(
                    "SELECT year, rate_per_mi, rate_h2_per_mi, h2_start_month FROM mileage_rates "
                    "WHERE account_id = %s AND year <= %s ORDER BY year DESC LIMIT 1",
                    (account_id(conn), start.year),
                )
                row = await cur.fetchone()
                rates = {}
                if row is not None:
                    year, r1, r2, month = row
                    rates[year] = YearRate(float(r1), float(r2) if r2 is not None else None,
                                           int(month) if month is not None else None)
                remaining_ms()
                await owned_thread(summary.finish, rates, start.year)
                remaining_ms()
                await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (f"{original_timeout}ms",))
                remaining_ms()
        except TimeoutError as exc:
            raise DigestPreparationTimeout("digest preparation deadline expired") from exc
    finally:
        await await_completion(asyncio.create_task(cursor.close()))
    remaining_ms()
    return summary
