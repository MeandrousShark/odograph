"""Report inputs shared by the report pages, the email digest and the API's
actual-expense comparison: a span's trips and rates, and a year's ledger rows
and odometer readings.

Every function takes an account connection and only reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.capacity import owned_thread
from app.expense_ledger import fetch_expense_rows
from app.expenses import ExpenseReport, build_expense_report
from app.rates import load_rates
from app.rates import YearRate
from app.trip_queries import DISPLAY_DISTANCE_SQL, REPORT_TRIP_COLUMNS

# Only what the expense comparison reads from a trip.
COMPARISON_TRIP_COLUMNS = (
    "id, vehicle_id, "
    "(SELECT name FROM vehicles WHERE account_id = trips.account_id AND id = trips.vehicle_id) AS vehicle_name, "
    "started_at, exclusion::text AS exclusion, category::text AS category, "
    f"{DISPLAY_DISTANCE_SQL} AS display_distance_m"
)


async def fetch_range_trips(conn, tz: ZoneInfo, start: date, end: date) -> tuple[list[dict], dict]:
    """Trips and rates for a `start`..`end` span (both inclusive, local to
    `tz`). The DB-side range is only a coarse UTC pre-filter; the report
    builders re-localize and drop anything outside `[start, end]`, so a trip
    near the boundary can only be excluded, never mis-attributed. `end + 1
    day` is computed on the plain `date` (not by adding a timedelta to an
    aware datetime) so a DST transition can't shift the boundary's local day.
    """
    trips = await _fetch_span_trips(conn, REPORT_TRIP_COLUMNS, tz, start, end)
    rates = await load_rates(conn)
    return trips, rates


async def _fetch_span_trips(conn, columns: str, tz: ZoneInfo, start: date, end: date) -> list[dict]:
    range_start = datetime(start.year, start.month, start.day, tzinfo=tz)
    next_day = end + timedelta(days=1)
    range_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {columns} FROM trips WHERE started_at >= %s AND started_at < %s"
        " AND account_id = %s ORDER BY started_at ASC, id ASC",
        (range_start, range_end, account_id(conn)),
    )
    return await cur.fetchall()


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


@dataclass(frozen=True)
class ComparisonInputs:
    trips: list[dict]
    expenses: list[dict]
    readings: list[dict]
    rates: dict[int, YearRate]


async def fetch_comparison_inputs(conn, year: int, tz: ZoneInfo) -> ComparisonInputs:
    """The year's comparison inputs, with trips narrowed to the columns the
    comparison reads. Callers read them under one consistent snapshot."""
    trips = await _fetch_span_trips(conn, COMPARISON_TRIP_COLUMNS, tz, date(year, 1, 1), date(year, 12, 31))
    rates = await load_rates(conn)
    expenses, readings = await fetch_expense_report_inputs(conn, year)
    return ComparisonInputs(trips, expenses, readings, rates)


async def build_comparison(inputs: ComparisonInputs, year: int, tz: ZoneInfo) -> ExpenseReport:
    """The year's actual-expense comparison, as the annual report computes it."""
    return await owned_thread(build_expense_report, year, inputs.trips, inputs.expenses, inputs.readings,
                              inputs.rates, tz)
