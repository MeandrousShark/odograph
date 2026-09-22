from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from psycopg.rows import dict_row

from app.account_context import account_id
from app.auth import require_user
from app.page import render_page
from app.config import DEFAULT_MISSING_TRIP_GAP_M
from app.dashboard import build_week_dashboard, parse_week_anchor, week_bounds
from app.rates import load_rates
from app.stats import build_dashboard
from app.stats_multiyear import build_multiyear_chart
from app.stats_trends import build_share_trend
from app.stats_vehicle import build_vehicle_breakdown
from app.trip_queries import DISPLAY_DISTANCE_SQL
from app.vehicles import list_vehicles

from app.ui._common import (
    TRIP_COLUMNS,
    VEHICLE_FILTER_UNASSIGNED,
    _fetch_recent_purposes,
    _parse_vehicle_id,
    _url_with_filters,
    parse_date_range,
)
from app.ui.reports import _multiyear_window


async def _build_week_dashboard_context(
    request: Request, anchor: date, now: datetime,
) -> dict:
    """Fetch and build one bounded Dashboard model for a local week.

    Dashboard actions use this same path as the full-page GET so a response
    rebuilt after a tag always has the same trip set, rates, expenses, and
    card context as a normal navigation.
    """
    config = request.state.config
    tz = config.display_tz
    bounds = week_bounds(anchor, tz)
    async with request.state.account_pool.connection() as conn:
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
        vehicles = await list_vehicles(conn)
        recent_purposes = await _fetch_recent_purposes(conn)

    threshold_m = getattr(
        config, "missing_trip_gap_m", DEFAULT_MISSING_TRIP_GAP_M
    )
    dashboard = build_week_dashboard(
        trips, expense_total, rates, anchor, tz, now, threshold_m,
    )
    return {
        "dashboard": dashboard,
        "vehicles": vehicles,
        "recent_purposes": recent_purposes,
    }


def register(router: APIRouter) -> None:
        @router.get("/stats")
        async def stats(
            request: Request,
            user: dict = Depends(require_user),
            year: int | None = Query(None, ge=1, le=9998),
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            vehicle: str = Query(""),
        ):
            """Operational view, deliberately separate from the filing-oriented
            annual report: category work and repeated routes are most useful for
            a chosen year or range, while the report preserves its tax-specific
            caveats and rate-period accounting.
            """
            tz = request.state.config.display_tz
            now = datetime.now(tz)
            selected_year = year or now.year
            year_start = datetime(selected_year, 1, 1, tzinfo=tz)
            next_year_start = datetime(selected_year + 1, 1, 1, tzinfo=tz)

            from_dt, to_dt = parse_date_range(from_, to, tz)
            if from_dt is not None and from_dt > year_start:
                year_start = from_dt
            if to_dt is not None and to_dt < next_year_start:
                next_year_start = to_dt

            vehicle_id = _parse_vehicle_id(vehicle)
            vehicle_clause = ""
            vehicle_params: list = []
            if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
                vehicle_clause = " AND vehicle_id IS NULL"
            elif vehicle_id is not None:
                vehicle_clause = " AND vehicle_id = %s"
                vehicle_params = [vehicle_id]

            period_start_date = from_dt.date() if from_dt is not None else date(selected_year, 1, 1)
            if to_dt is not None:
                period_end_date = (to_dt - timedelta(days=1)).date()
            elif selected_year < now.year:
                period_end_date = date(selected_year, 12, 31)
            else:
                period_end_date = now.date()
            # A from/to filter can spill outside the selected year (e.g. to=2027-01-15
            # with year=2026); clamp so monthly bucket ranges stay within that year.
            period_start_date = max(period_start_date, date(selected_year, 1, 1))
            period_end_date = min(period_end_date, date(selected_year, 12, 31))

            async with request.state.account_pool.connection() as conn:
                category_cur = await conn.execute(
                    "SELECT CASE WHEN exclusion = 'not_deductible' THEN 'nondeductible' "
                    "ELSE category::text END, count(*), "
                    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
                    "FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f"AND started_at >= %s AND started_at < %s{vehicle_clause} GROUP BY 1",
                    (account_id(conn), year_start, next_year_start, *vehicle_params),
                )
                weekly_cur = await conn.execute(
                    "SELECT date_trunc('week', started_at AT TIME ZONE %s)::date, "
                    "CASE WHEN exclusion = 'not_deductible' THEN 'nondeductible' "
                    "ELSE category::text END, count(*), "
                    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
                    "FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f"AND started_at >= %s AND started_at < %s{vehicle_clause} "
                    "GROUP BY 1, 2 ORDER BY 1",
                    (tz.key, account_id(conn), year_start, next_year_start, *vehicle_params),
                )
                monthly_cur = await conn.execute(
                    "SELECT date_trunc('month', started_at AT TIME ZONE %s)::date, "
                    "CASE WHEN exclusion = 'not_deductible' THEN 'nondeductible' "
                    "ELSE category::text END, count(*), "
                    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
                    "FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f"AND started_at >= %s AND started_at < %s{vehicle_clause} "
                    "GROUP BY 1, 2 ORDER BY 1",
                    (tz.key, account_id(conn), year_start, next_year_start, *vehicle_params),
                )
                routes_cur = await conn.execute(
                    "WITH named_routes AS ("
                    " SELECT LEAST(start_place_id, end_place_id) AS a_id, GREATEST(start_place_id, end_place_id) AS b_id, "
                    f" count(*) AS trip_count, COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) AS total_m "
                    " FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    " AND started_at >= %s AND started_at < %s "
                    " AND start_place_id IS NOT NULL AND end_place_id IS NOT NULL"
                    f"{vehicle_clause} "
                    " GROUP BY 1, 2) "
                    "SELECT a.name, b.name, named_routes.trip_count, named_routes.total_m "
                    "FROM named_routes JOIN places a ON a.id = named_routes.a_id "
                    "JOIN places b ON b.id = named_routes.b_id "
                    "WHERE a.account_id = %s AND b.account_id = %s "
                    "ORDER BY total_m DESC, trip_count DESC, a.name, b.name LIMIT 5",
                    (account_id(conn), year_start, next_year_start, *vehicle_params, account_id(conn), account_id(conn)),
                )
                places_cur = await conn.execute(
                    "WITH endpoints AS ("
                    " SELECT start_place_id AS place_id FROM trips "
                    " WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f" AND started_at >= %s AND started_at < %s{vehicle_clause} "
                    " UNION ALL SELECT end_place_id FROM trips "
                    " WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f" AND started_at >= %s AND started_at < %s{vehicle_clause}) "
                    "SELECT places.name, count(*) AS visit_count FROM endpoints "
                    "JOIN places ON places.id = endpoints.place_id WHERE places.account_id = %s GROUP BY places.id, places.name "
                    "ORDER BY visit_count DESC, places.name LIMIT 5",
                    (account_id(conn), year_start, next_year_start, *vehicle_params, account_id(conn), year_start, next_year_start, *vehicle_params, account_id(conn)),
                )
                unnamed_cur = await conn.execute(
                    "SELECT count(*) FROM trips "
                    "WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f"AND started_at >= %s AND started_at < %s{vehicle_clause} "
                    "AND (start_place_id IS NULL OR end_place_id IS NULL)",
                    (account_id(conn), year_start, next_year_start, *vehicle_params),
                )

                def drill_url(start: date, end: date, category: str) -> str:
                    if category == "nondeductible":
                        return _url_with_filters(
                            "/trips", start.isoformat(), end.isoformat(), vehicle,
                            exclusion="not_deductible",
                        )
                    return _url_with_filters(
                        "/trips", start.isoformat(), end.isoformat(), vehicle,
                        category=category, exclusion="none",
                    )

                dashboard = build_dashboard(
                    selected_year, period_start_date, period_end_date,
                    await category_cur.fetchall(), await weekly_cur.fetchall(),
                    await monthly_cur.fetchall(),
                    [
                        {"start_name": row[0], "end_name": row[1], "trip_count": row[2], "total_m": float(row[3])}
                        for row in await routes_cur.fetchall()
                    ],
                    [{"name": row[0], "visit_count": row[1]} for row in await places_cur.fetchall()],
                    (await unnamed_cur.fetchone())[0],
                    drill_url,
                )

                # The query itself stays unbounded on year so the five-year
                # window can be chosen in Python (see _multiyear_window) instead
                # of re-querying per candidate window. The from/to date filter
                # still does not apply here: a year-over-year comparison is
                # meaningless clamped to a sub-year range. It still respects the
                # vehicle filter.
                cross_year_cur = await conn.execute(
                    "SELECT EXTRACT(year FROM started_at AT TIME ZONE %s)::int, "
                    "EXTRACT(month FROM started_at AT TIME ZONE %s)::int, "
                    "CASE WHEN exclusion = 'not_deductible' THEN 'nondeductible' "
                    "ELSE category::text END, count(*), "
                    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
                    "FROM trips WHERE account_id = %s AND exclusion IS DISTINCT FROM 'not_my_vehicle'"
                    f"{vehicle_clause} GROUP BY 1, 2, 3 ORDER BY 1, 2",
                    (tz.key, tz.key, account_id(conn), *vehicle_params),
                )
                cross_year_rows = await cross_year_cur.fetchall()
                years_present = sorted({row[0] for row in cross_year_rows})
                multiyear_years, cutoff_month, cutoff_day = _multiyear_window(
                    years_present, selected_year, now
                )

                def multiyear_drill_url(bar_year: int, bar_month: int) -> str:
                    # The bar's own year, not `selected_year`: clicking the 2022
                    # bar in the January group must go to January 2022 even
                    # though the page is showing a different year.
                    last_day = calendar.monthrange(bar_year, bar_month)[1]
                    start = date(bar_year, bar_month, 1)
                    end = date(bar_year, bar_month, last_day)
                    return _url_with_filters("/trips", start.isoformat(), end.isoformat(), vehicle)

                multiyear = build_multiyear_chart(
                    cross_year_rows, multiyear_years, cutoff_month, cutoff_day, multiyear_drill_url
                )
                trend = build_share_trend(cross_year_rows, multiyear_years, cutoff_month)

                vehicle_mileage_cur = await conn.execute(
                    "SELECT t.vehicle_id, COALESCE(v.name, ''), "
                    "EXTRACT(month FROM t.started_at AT TIME ZONE %s)::int, "
                    "CASE WHEN t.exclusion = 'not_deductible' THEN 'nondeductible' "
                    "ELSE t.category::text END, count(*), "
                    f"COALESCE(SUM({DISPLAY_DISTANCE_SQL}), 0) "
                    "FROM trips t LEFT JOIN vehicles v ON v.id = t.vehicle_id AND v.account_id = t.account_id "
                    "WHERE t.account_id = %s AND t.exclusion IS DISTINCT FROM 'not_my_vehicle' "
                    f"AND t.started_at >= %s AND t.started_at < %s{vehicle_clause} "
                    "GROUP BY 1, 2, 3, 4",
                    (tz.key, account_id(conn), year_start, next_year_start, *vehicle_params),
                )
                vehicle_expenses_cur = await conn.execute(
                    "SELECT expenses.vehicle_id, vehicles.name, COALESCE(SUM(expenses.amount), 0) "
                    "FROM expenses JOIN vehicles ON vehicles.id = expenses.vehicle_id AND vehicles.account_id = expenses.account_id "
                    f"WHERE expenses.account_id = %s AND expenses.incurred_on >= %s AND expenses.incurred_on <= %s{vehicle_clause} "
                    "GROUP BY 1, 2",
                    (account_id(conn), period_start_date, period_end_date, *vehicle_params),
                )
                rates = await load_rates(conn)
                vehicle_breakdown = build_vehicle_breakdown(
                    await vehicle_mileage_cur.fetchall(),
                    await vehicle_expenses_cur.fetchall(),
                    selected_year,
                    rates,
                )

                vehicles = await list_vehicles(conn)
            return await render_page(
                request, "stats.html", {
                    "user": user,
                    "csrf": request.session.get("csrf", ""),
                    "stats": dashboard,
                    "multiyear": multiyear,
                    "trend": trend,
                    "vehicle_breakdown": vehicle_breakdown,
                    "filter_year": selected_year,
                    "filter_from": from_,
                    "filter_to": to,
                    "filter_vehicle": vehicle,
                    "vehicles": vehicles,
                    "next_year_disabled": selected_year >= now.year,
                    "period_start": period_start_date,
                    "period_end": period_end_date,
                },
            )

        @router.get("/")
        async def weekly_dashboard(
            request: Request,
            user: dict = Depends(require_user),
            week: str = Query(""),
        ):
            """The bounded weekly dashboard. `week` is an anchor, not a trusted
            Monday. `parse_week_anchor`/`week_bounds` normalize it forgivingly
            so a stale `?week=` link never 400s.

            Only two queries: one `TRIP_COLUMNS` fetch for the week and one
            expense sum over the same local week as `incurred_on` *dates* (that
            column has no time component). Every summary figure derives from the
            same fetched `trips` that become the day-grouped cards, so the
            numbers can't drift from what's on screen.
            """
            config = request.state.config
            tz = config.display_tz
            now = datetime.now(tz)
            anchor = parse_week_anchor(week, tz, now)
            dashboard_context = await _build_week_dashboard_context(request, anchor, now)

            return await render_page(
                request, "dashboard.html",
                {
                    **dashboard_context,
                    "user": user, "csrf": request.session.get("csrf", ""),
                },
            )
