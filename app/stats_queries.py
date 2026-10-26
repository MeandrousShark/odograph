"""Stats reads and aggregation shared by the web Stats page and the API: the
year's totals by category, the multiyear monthly series, the classified
share trend by quarter and the per-vehicle breakdown.

The database aggregates and buckets by the display time zone; the pure
builders in app/stats*.py fold those rows. The web draws its charts from the
same folds, and the API returns the numbers alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.account_context import account_id
from app.capacity import owned_thread
from app.rates import YearRate, load_rates
from app.stats import category_amounts
from app.stats_multiyear import YearSeries, multiyear_series
from app.stats_trends import ShareBucket, quarterly_share
from app.stats_vehicle import VehicleBreakdown, build_vehicle_breakdown
from app.trip_queries import DISPLAY_DISTANCE_SQL, VEHICLE_FILTER_UNASSIGNED

@dataclass(frozen=True)
class StatsPeriod:
    """`start <= started_at < end` bounds the year's trips; `start_date` and
    `end_date` (inclusive) are the elapsed calendar span within the year."""

    year: int
    start: datetime
    end: datetime
    start_date: date
    end_date: date
    vehicle_id: int | str | None = None


def stats_period(
    year: int, tz: ZoneInfo, now: datetime, from_dt: datetime | None = None,
    to_dt: datetime | None = None, vehicle_id: int | str | None = None,
) -> StatsPeriod:
    """The Stats period for `year`, narrowed by an optional local range.

    A past year ends on December 31; the current (or a future) year ends
    today. A range that spills outside the year is clamped to it so monthly
    buckets stay within that year.
    """
    year_start = datetime(year, 1, 1, tzinfo=tz)
    next_year_start = datetime(year + 1, 1, 1, tzinfo=tz)
    if from_dt is not None and from_dt > year_start:
        year_start = from_dt
    if to_dt is not None and to_dt < next_year_start:
        next_year_start = to_dt
    start_date = from_dt.date() if from_dt is not None else date(year, 1, 1)
    if to_dt is not None:
        end_date = (to_dt - timedelta(days=1)).date()
    elif year < now.year:
        end_date = date(year, 12, 31)
    else:
        end_date = now.date()
    start_date = max(start_date, date(year, 1, 1))
    end_date = min(end_date, date(year, 12, 31))
    return StatsPeriod(year, year_start, next_year_start, start_date, end_date, vehicle_id)


def vehicle_clause(vehicle_id: int | str | None) -> tuple[str, list]:
    """The Stats queries' vehicle filter: one vehicle, unassigned, or all."""
    if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
        return " AND vehicle_id IS NULL", []
    if vehicle_id is not None:
        return " AND vehicle_id = %s", [vehicle_id]
    return "", []


def multiyear_window(
    years_present: list[int], selected_year: int, now: datetime
) -> tuple[list[int], int, int]:
    """Derive the year-over-year window and cutoff for the stats page's
    cross-year charts from the selected year, not from today.

    A selected year in the past is fully elapsed, so its cutoff is the end
    of December and no month is clamped. `>=` rather than `==` against
    `now.year` is deliberate: a hand-crafted future `?year=2030` should
    degrade to the current-year, same-elapsed-period cutoff rather than
    claim a year that hasn't happened yet is complete.

    The window is the five most recent years present that are no later
    than the selected year, so `?year=2024` ends at 2024 and never shows
    2025 or 2026 even though they exist in the data.
    """
    if selected_year >= now.year:
        cutoff_month, cutoff_day = now.month, now.day
    else:
        cutoff_month, cutoff_day = 12, 31
    eligible_years = [y for y in years_present if y <= selected_year]
    return eligible_years[-5:], cutoff_month, cutoff_day


async def fetch_category_rows(conn, period: StatsPeriod) -> list[tuple]:
    """(category, trip count, meters) for the period."""
    clause, params = vehicle_clause(period.vehicle_id)
    cur = await conn.execute(
        "SELECT CASE WHEN exclusion = 'not_deductible' THEN 'nondeductible' "
        "ELSE category::text END, count(*), "
        f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
        "FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
        f"AND started_at >= %s AND started_at < %s{clause} GROUP BY 1",
        (account_id(conn), period.start, period.end, *params),
    )
    return await cur.fetchall()


async def fetch_cross_year_rows(conn, tz: ZoneInfo, vehicle_id: int | str | None) -> list[tuple]:
    """(year, month, category, trip count, meters) over every year.

    The query stays unbounded on year so the five-year window can be chosen
    in Python (see `multiyear_window`) instead of re-querying per candidate
    window. A from/to date filter does not apply here: a year-over-year
    comparison is meaningless clamped to a sub-year range. It still respects
    the vehicle filter.
    """
    clause, params = vehicle_clause(vehicle_id)
    cur = await conn.execute(
        "SELECT EXTRACT(year FROM started_at AT TIME ZONE %s)::int, "
        "EXTRACT(month FROM started_at AT TIME ZONE %s)::int, "
        "CASE WHEN exclusion = 'not_deductible' THEN 'nondeductible' "
        "ELSE category::text END, count(*), "
        f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
        "FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle'"
        f"{clause} GROUP BY 1, 2, 3 ORDER BY 1, 2",
        (tz.key, tz.key, account_id(conn), *params),
    )
    return await cur.fetchall()


async def fetch_vehicle_rows(conn, period: StatsPeriod, tz: ZoneInfo) -> tuple[list[tuple], list[tuple]]:
    """Per-vehicle (vehicle id, name, month, category, trip count, meters)
    for the period's trips, and (vehicle id, name, amount) expense sums over
    its elapsed calendar span."""
    clause, params = vehicle_clause(period.vehicle_id)
    mileage_cur = await conn.execute(
        "SELECT t.vehicle_id, COALESCE(v.name, ''), "
        "EXTRACT(month FROM t.started_at AT TIME ZONE %s)::int, "
        "CASE WHEN t.exclusion = 'not_deductible' THEN 'nondeductible' "
        "ELSE t.category::text END, count(*), "
        f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
        "FROM trips t LEFT JOIN vehicles v ON v.id = t.vehicle_id AND v.account_id = t.account_id "
        "WHERE t.account_id = %s AND t.exclusion IS DISTINCT FROM 'not_my_vehicle' "
        f"AND t.started_at >= %s AND t.started_at < %s{clause} "
        "GROUP BY 1, 2, 3, 4",
        (tz.key, account_id(conn), period.start, period.end, *params),
    )
    expenses_cur = await conn.execute(
        "SELECT expenses.vehicle_id, vehicles.name, COALESCE(SUM(expenses.amount), 0) "
        "FROM expenses JOIN vehicles ON vehicles.id = expenses.vehicle_id AND vehicles.account_id = expenses.account_id "
        f"WHERE expenses.account_id = %s AND expenses.incurred_on >= %s AND expenses.incurred_on <= %s{clause} "
        "GROUP BY 1, 2",
        (account_id(conn), period.start_date, period.end_date, *params),
    )
    return await mileage_cur.fetchall(), await expenses_cur.fetchall()


@dataclass(frozen=True)
class StatsSummary:
    """The four Stats views as numbers. `quarters` holds `(year, quarter,
    bucket)`; `vehicle_expenses` holds each vehicle's exact expense sum."""

    period: StatsPeriod
    totals: dict[str, tuple[int, float]]
    multiyear: list[YearSeries]
    through_month: int
    quarters: list[tuple[int, int, ShareBucket]]
    quarters_clamped: bool
    vehicles: VehicleBreakdown
    vehicle_expenses: dict[int | None, Decimal]


def summarize_stats(
    period: StatsPeriod, now: datetime, category_rows: list[tuple], cross_year_rows: list[tuple],
    mileage_rows: list[tuple], expense_rows: list[tuple], rates: dict[int, YearRate],
) -> StatsSummary:
    years_present = sorted({row[0] for row in cross_year_rows})
    years, cutoff_month, cutoff_day = multiyear_window(years_present, period.year, now)
    series, through_month = multiyear_series(cross_year_rows, years, cutoff_month, cutoff_day)
    quarters, clamped = quarterly_share(cross_year_rows, years, cutoff_month)
    vehicle_expenses: dict[int | None, Decimal] = {}
    for vehicle_id, _name, amount in expense_rows:
        vehicle_expenses[vehicle_id] = vehicle_expenses.get(vehicle_id, Decimal("0")) + amount
    return StatsSummary(
        period=period, totals=category_amounts(category_rows), multiyear=series,
        through_month=through_month, quarters=quarters, quarters_clamped=clamped,
        vehicles=build_vehicle_breakdown(mileage_rows, expense_rows, period.year, rates),
        vehicle_expenses=vehicle_expenses,
    )


async def fetch_stats_summary(conn, period: StatsPeriod, tz: ZoneInfo, now: datetime) -> StatsSummary:
    category_rows = await fetch_category_rows(conn, period)
    cross_year_rows = await fetch_cross_year_rows(conn, tz, period.vehicle_id)
    mileage_rows, expense_rows = await fetch_vehicle_rows(conn, period, tz)
    rates = await load_rates(conn)
    return await owned_thread(summarize_stats, period, now, category_rows, cross_year_rows,
                              mileage_rows, expense_rows, rates)
