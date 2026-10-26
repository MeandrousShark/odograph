"""The weekly dashboard's reads, beside the pure builder in app/dashboard.py.

Two queries: one `TRIP_COLUMNS` fetch for the local week and one expense sum
over the same local week as `incurred_on` dates (that column has no time
component), plus the rates the deduction estimate prices with. Every summary
figure derives from the same fetched trips, so the numbers can't drift from
the trips the week shows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.capacity import owned_thread
from app.config import DEFAULT_MISSING_TRIP_GAP_M
from app.dashboard import WeekDashboard, build_week_dashboard, week_bounds
from app.rates import YearRate, load_rates
from app.trip_queries import TRIP_COLUMNS


@dataclass(frozen=True)
class WeekInputs:
    trips: list[dict]
    expense_total: Decimal
    rates: dict[int, YearRate]


def missing_trip_threshold(config) -> float:
    return getattr(config, "missing_trip_gap_m", DEFAULT_MISSING_TRIP_GAP_M)


async def fetch_week_inputs(conn, anchor: date, tz: ZoneInfo) -> WeekInputs:
    """The trips, expense total and rates for the local week holding `anchor`."""
    bounds = week_bounds(anchor, tz)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips WHERE started_at >= %s AND started_at < %s "
        "AND account_id = %s ORDER BY started_at DESC, id DESC",
        (bounds.start, bounds.end, account_id(conn)),
    )
    trips = await cur.fetchall()
    expense_cur = await conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses "
        "WHERE incurred_on >= %s AND incurred_on < %s AND account_id = %s",
        (bounds.monday, bounds.monday + timedelta(days=7), account_id(conn)),
    )
    expense_total = (await expense_cur.fetchone())[0]
    rates = await load_rates(conn)
    return WeekInputs(trips, expense_total, rates)


async def build_week(
    inputs: WeekInputs, anchor: date, tz: ZoneInfo, now: datetime, threshold_m: float,
) -> WeekDashboard:
    """Build the week's model off the event loop, under the caller's admission."""
    return await owned_thread(build_week_dashboard,
        inputs.trips, inputs.expense_total, inputs.rates, anchor, tz, now, threshold_m,
    )
