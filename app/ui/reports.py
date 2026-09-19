from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from psycopg.rows import dict_row
from starlette.responses import RedirectResponse, Response

from app.account_context import account_id
from app.auth import require_user
from app.page import render_page
from app.export import to_csv, to_range_report_xlsx, to_report_xlsx, to_xlsx
from app.expenses import ExpenseReport, build_expense_report
from app.odometer import OdometerReading, VehicleCoverage, vehicle_coverage_for_report
from app.rates import YearRate, load_rates
from app.report import (
    AnnualReport,
    RangeReport,
    build_annual_report,
    build_range_report,
    default_report_year,
    next_year_disabled,
    range_filename_slug,
)

from app.ui._common import (
    EXPORT_MEDIA_TYPES,
    TRIP_COLUMNS,
    _parse_range_query_dates,
    _parse_vehicle_id,
    _trip_filter_sql,
    parse_date_range,
)
from app.ui.expenses import _EXPENSE_SELECT_JOIN

@dataclass(frozen=True)
class _RangeReportData:
    tz: ZoneInfo
    start: date
    end: date
    report: RangeReport
    trips: list[dict]
    rates: dict[int, YearRate]


@dataclass(frozen=True)
class _AnnualReportData:
    tz: ZoneInfo
    report: AnnualReport
    trips: list[dict]
    rates: dict[int, YearRate]
    odometer_coverage: list[VehicleCoverage]
    expenses: list[dict]
    expense_report: ExpenseReport


def _multiyear_window(
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


async def _fetch_range_trips_in(conn, tz: ZoneInfo, start: date, end: date) -> tuple[list[dict], dict]:
    """`conn`-accepting variant of `_fetch_range_trips`, for callers (the
    email digest worker) that already hold a connection and must not
    acquire a second one from the pool inside the same run -- with a
    pool of size 1 that would deadlock.

    Trips + rates for a `start`..`end` span (both inclusive, local to `tz`),
    shared by the annual/range report pages and their exports so they can't
    drift apart. The DB-side range is only a coarse UTC pre-filter;
    `build_range_report` re-localizes and drops anything outside `[start, end]`,
    so a trip near the boundary can only be excluded, never mis-attributed.
    `end + 1 day` is computed on the plain `date` (not by adding a timedelta
    to an aware datetime) so a DST transition can't shift the boundary's
    local day, for the same reason `_month_bounds` builds its boundary directly.
    """
    range_start = datetime(start.year, start.month, start.day, tzinfo=tz)
    next_day = end + timedelta(days=1)
    range_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips WHERE started_at >= %s AND started_at < %s"
        " AND account_id = %s ORDER BY started_at",
        (range_start, range_end, account_id(conn)),
    )
    trips = await cur.fetchall()
    rates = await load_rates(conn)
    return trips, rates


async def _fetch_range_trips(pool, tz: ZoneInfo, start: date, end: date) -> tuple[list[dict], dict]:
    """Thin pool-owning wrapper around `_fetch_range_trips_in` for callers
    that have no other work to share a connection with.
    """
    async with pool.connection() as conn:
        return await _fetch_range_trips_in(conn, tz, start, end)


async def _fetch_year_odometer_coverage(pool, tz: ZoneInfo, year: int, trips: list[dict]) -> list:
    """Per-vehicle odometer coverage for the report year, computed outside
    `build_annual_report` from the same `trips` the report already fetched,
    only the year's readings need a query. The upper bound is `<=
    next_year_start` (not `<`) so a reading recorded exactly at midnight
    Jan 1 of the following year, the one reading that actually brackets the
    end of the report year, is included; anything after that boundary is
    still excluded so the reconciliation span stays within the report year.
    """
    year_start = datetime(year, 1, 1, tzinfo=tz)
    next_year_start = datetime(year + 1, 1, 1, tzinfo=tz)
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT odometer_readings.vehicle_id, odometer_readings.recorded_at, "
            "odometer_readings.odometer_m, vehicles.name AS vehicle_name "
            "FROM odometer_readings JOIN vehicles ON vehicles.id = odometer_readings.vehicle_id AND vehicles.account_id = odometer_readings.account_id "
            "WHERE recorded_at >= %s AND recorded_at <= %s AND odometer_readings.account_id = %s",
            (year_start, next_year_start, account_id(conn)),
        )
        rows = await cur.fetchall()

    readings_by_vehicle: dict[tuple[int, str], list[OdometerReading]] = {}
    for row in rows:
        key = (row["vehicle_id"], row["vehicle_name"])
        readings_by_vehicle.setdefault(key, []).append(
            OdometerReading(row["recorded_at"], row["odometer_m"])
        )
    if not readings_by_vehicle:
        return []

    trips_by_vehicle: dict[tuple[int, str], list[tuple]] = {}
    for trip in trips:
        if trip.get("exclusion") == "not_my_vehicle":
            continue
        vehicle_id = trip.get("vehicle_id")
        vehicle_name = trip.get("vehicle_name")
        if vehicle_id is not None and vehicle_name:
            trips_by_vehicle.setdefault((vehicle_id, vehicle_name), []).append(
                (trip["started_at"], trip["display_distance_m"])
            )
    return vehicle_coverage_for_report(readings_by_vehicle, trips_by_vehicle, year_start, next_year_start)


async def _fetch_year_expense_report(pool, tz: ZoneInfo, year: int, trips: list[dict], rates: dict):
    """Fetch ledger rows plus the wider odometer boundary set the
    expense comparison needs.

    Unlike the within-year reconciliation, a tax-year denominator must span
    the entire year. Fetching all readings lets the pure layer choose the
    closest reading on each side without annualizing a partial interval.
    """
    async with pool.connection() as conn:
        expense_cur = conn.cursor(row_factory=dict_row)
        await expense_cur.execute(
            _EXPENSE_SELECT_JOIN
            + "WHERE expenses.incurred_on >= %s AND expenses.incurred_on < %s "
            "AND expenses.account_id = %s ORDER BY expenses.incurred_on, expenses.id",
            (date(year, 1, 1), date(year + 1, 1, 1), account_id(conn)),
        )
        expenses = await expense_cur.fetchall()
        reading_cur = conn.cursor(row_factory=dict_row)
        await reading_cur.execute(
            "SELECT odometer_readings.vehicle_id, vehicles.name AS vehicle_name, "
            "odometer_readings.recorded_at, odometer_readings.odometer_m "
            "FROM odometer_readings JOIN vehicles ON vehicles.id = odometer_readings.vehicle_id AND vehicles.account_id = odometer_readings.account_id "
            "WHERE odometer_readings.account_id = %s ORDER BY odometer_readings.recorded_at",
            (account_id(conn),),
        )
        readings = await reading_cur.fetchall()
    return expenses, build_expense_report(year, trips, expenses, readings, rates, tz)


async def _build_range_report_data(
    request: Request, from_str: str, to_str: str,
) -> _RangeReportData:
    tz = request.state.config.display_tz
    start, end = _parse_range_query_dates(from_str, to_str)
    trips, rates = await _fetch_range_trips(request.state.account_pool, tz, start, end)
    try:
        report = build_range_report(trips, rates, tz, start, end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _RangeReportData(tz, start, end, report, trips, rates)


async def _build_annual_report_data(request: Request, year: int) -> _AnnualReportData:
    pool = request.state.account_pool
    tz = request.state.config.display_tz
    trips, rates = await _fetch_range_trips(
        pool, tz, date(year, 1, 1), date(year, 12, 31)
    )
    report = build_annual_report(trips, rates, tz, year)
    odometer_coverage = await _fetch_year_odometer_coverage(pool, tz, year, trips)
    expenses, expense_report = await _fetch_year_expense_report(pool, tz, year, trips, rates)
    return _AnnualReportData(
        tz, report, trips, rates, odometer_coverage, expenses, expense_report
    )


def register(router: APIRouter) -> None:
        @router.get("/export")
        async def export_trips(
            request: Request,
            user: dict = Depends(require_user),
            format: str = Query("csv"),
            category: str = Query(""),
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            vehicle: str = Query(""),
            q: str = Query(""),
            exclusion: str = Query(""),
        ):
            exclusion = exclusion if isinstance(exclusion, str) else ""
            if format not in EXPORT_MEDIA_TYPES:
                raise HTTPException(status_code=400, detail="format must be csv or xlsx")
            tz = request.state.config.display_tz
            from_dt, to_dt = parse_date_range(from_, to, tz)
            vehicle_id = _parse_vehicle_id(vehicle)
            # Reuses the exact same filter SQL as index() so a filtered export
            # can never drift from what's currently on screen.
            where, params = _trip_filter_sql(
                category, from_dt, to_dt, vehicle_id, q=q, exclusion=exclusion,
                owner_id=request.state.principal.account_id,
            )
            async with request.state.account_pool.connection() as conn:
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    f"SELECT {TRIP_COLUMNS} FROM trips {where} ORDER BY started_at DESC", params
                )
                trips = await cur.fetchall()
                rates = await load_rates(conn)

            # CSV/XLSX serialization is CPU-bound; offload so it doesn't block
            # the event loop for other requests while a large export builds.
            if format == "csv":
                content = await asyncio.to_thread(to_csv, trips, rates, tz)
            else:
                content = await asyncio.to_thread(to_xlsx, trips, rates, tz)
            return Response(
                content=content,
                media_type=EXPORT_MEDIA_TYPES[format],
                headers={"Content-Disposition": f'attachment; filename="trips.{format}"'},
            )

        @router.get("/report")
        async def report_redirect(request: Request, user: dict = Depends(require_user)):
            tz = request.state.config.display_tz
            year = default_report_year(datetime.now(tz))
            return RedirectResponse(f"/report/{year}", status_code=302)

        # Registered ahead of "/report/{year}" (and its /export). FastAPI/
        # Starlette match a bare "{year}" path segment structurally before ever
        # trying to convert it to int, so "/report/range" would otherwise be
        # swallowed by "/report/{year}" and 422 on int-parsing "range" instead of
        # ever reaching these handlers.
        @router.get("/report/range")
        async def report_range_page(
            request: Request,
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            user: dict = Depends(require_user),
        ):
            data = await _build_range_report_data(request, from_, to)
            return await render_page(
                request, "report_range.html",
                {
                    "report": data.report,
                    "user": user, "csrf": request.session.get("csrf", ""),
                },
            )

        @router.get("/report/range/export")
        async def report_range_export(
            request: Request,
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            user: dict = Depends(require_user),
        ):
            data = await _build_range_report_data(request, from_, to)
            # XLSX serialization is CPU-bound; offload so it doesn't block the
            # event loop for other requests while the report builds.
            content = await asyncio.to_thread(
                to_range_report_xlsx,
                data.report,
                data.trips,
                data.rates,
                data.tz,
            )
            filename = f"mileage-report-{range_filename_slug(data.start, data.end)}.xlsx"
            return Response(
                content=content,
                media_type=EXPORT_MEDIA_TYPES["xlsx"],
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        @router.get("/report/{year}")
        async def report_page(
            request: Request,
            year: int = Path(ge=1, le=9998),  # +1 must also stay in datetime's 1-9999 range
            user: dict = Depends(require_user),
        ):
            data = await _build_annual_report_data(request, year)
            return await render_page(
                request, "report.html",
                {
                    "report": data.report, "odometer_coverage": data.odometer_coverage,
                    "expenses": data.expenses, "expense_report": data.expense_report,
                    "user": user, "csrf": request.session.get("csrf", ""),
                    "next_year_disabled": next_year_disabled(
                        data.report.year, datetime.now(data.tz)
                    ),
                },
            )

        @router.get("/report/{year}/export")
        async def report_export(
            request: Request,
            year: int = Path(ge=1, le=9998),
            user: dict = Depends(require_user),
        ):
            data = await _build_annual_report_data(request, year)
            # XLSX serialization is CPU-bound; offload so it doesn't block the
            # event loop for other requests while the report builds.
            content = await asyncio.to_thread(
                to_report_xlsx,
                data.report,
                data.trips,
                data.rates,
                data.tz,
                data.odometer_coverage,
                data.expense_report,
                data.expenses,
            )
            return Response(
                content=content,
                media_type=EXPORT_MEDIA_TYPES["xlsx"],
                headers={"Content-Disposition": f'attachment; filename="mileage-report-{year}.xlsx"'},
            )
