"""Report inputs shared by the report pages, the email digest and the API's
actual-expense comparison: a span's trips and rates, and a year's ledger rows
and odometer readings.

Every function takes an account connection and only reads.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.capacity import owned_thread
from app.expense_ledger import fetch_expense_rows
from app.expenses import ExpenseReport, build_expense_report
from app.rates import load_rates
from app.trip_queries import REPORT_TRIP_COLUMNS


async def fetch_range_trips(conn, tz: ZoneInfo, start: date, end: date) -> tuple[list[dict], dict]:
    """Trips and rates for a `start`..`end` span (both inclusive, local to
    `tz`). The DB-side range is only a coarse UTC pre-filter; the report
    builders re-localize and drop anything outside `[start, end]`, so a trip
    near the boundary can only be excluded, never mis-attributed. `end + 1
    day` is computed on the plain `date` (not by adding a timedelta to an
    aware datetime) so a DST transition can't shift the boundary's local day.
    """
    range_start = datetime(start.year, start.month, start.day, tzinfo=tz)
    next_day = end + timedelta(days=1)
    range_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {REPORT_TRIP_COLUMNS} FROM trips WHERE started_at >= %s AND started_at < %s"
        " AND account_id = %s ORDER BY started_at ASC, id ASC",
        (range_start, range_end, account_id(conn)),
    )
    trips = await cur.fetchall()
    rates = await load_rates(conn)
    return trips, rates


async def fetch_expense_report_inputs(conn, year: int) -> tuple[list[dict], list[dict]]:
    """A year's ledger rows, oldest first, plus every odometer reading.

    Unlike the within-year reconciliation, a tax-year denominator must span
    the entire year. Fetching all readings lets the pure layer choose the
    closest reading on each side without annualizing a partial interval.
    """
    expenses = await fetch_expense_rows(conn, date(year, 1, 1), date(year + 1, 1, 1), descending=False)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT odometer_readings.vehicle_id, vehicles.name AS vehicle_name, "
        "odometer_readings.recorded_at, odometer_readings.odometer_m "
        "FROM odometer_readings JOIN vehicles ON vehicles.id = odometer_readings.vehicle_id AND vehicles.account_id = odometer_readings.account_id "
        "WHERE odometer_readings.account_id = %s ORDER BY odometer_readings.recorded_at",
        (account_id(conn),),
    )
    return expenses, await cur.fetchall()


async def fetch_expense_comparison(conn, year: int, tz: ZoneInfo) -> ExpenseReport:
    """The year's actual-expense comparison, as the annual report computes it."""
    trips, rates = await fetch_range_trips(conn, tz, date(year, 1, 1), date(year, 12, 31))
    expenses, readings = await fetch_expense_report_inputs(conn, year)
    return await owned_thread(build_expense_report, year, trips, expenses, readings, rates, tz)
