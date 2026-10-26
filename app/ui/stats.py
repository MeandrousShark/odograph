from __future__ import annotations

import calendar
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request

from app.account_context import account_id
from app.auth import require_user
from app.capacity import owned_thread
from app.page import render_page
from app.dashboard import parse_week_anchor
from app.rates import load_rates
from app.stats import build_dashboard
from app.stats_multiyear import build_multiyear_chart
from app.stats_trends import build_share_trend
from app.stats_vehicle import build_vehicle_breakdown
from app.stats_queries import (
    fetch_category_rows, fetch_cross_year_rows, fetch_vehicle_rows, multiyear_window, stats_period,
    vehicle_clause as stats_vehicle_clause,
)
from app.trip_queries import DISPLAY_DISTANCE_SQL
from app.vehicles import list_vehicles
from app.week_dashboard import build_week, fetch_week_inputs, missing_trip_threshold

from app.ui._common import (
    _fetch_recent_purposes,
    _parse_vehicle_id,
    _url_with_filters,
    parse_date_range,
)


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
    async with request.state.account_pool.connection() as conn:
        inputs = await fetch_week_inputs(conn, anchor, tz)
        vehicles = await list_vehicles(conn)
        recent_purposes = await _fetch_recent_purposes(conn)

    dashboard = await build_week(inputs, anchor, tz, now, missing_trip_threshold(config))
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
            from_dt, to_dt = parse_date_range(from_, to, tz)
            vehicle_id = _parse_vehicle_id(vehicle)
            period = stats_period(selected_year, tz, now, from_dt, to_dt, vehicle_id)
            year_start, next_year_start = period.start, period.end
            period_start_date, period_end_date = period.start_date, period.end_date
            vehicle_clause, vehicle_params = stats_vehicle_clause(vehicle_id)

            async with request.state.account_pool.connection() as conn:
                category_rows = await fetch_category_rows(conn, period)
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

                dashboard = await owned_thread(build_dashboard,
                    selected_year, period_start_date, period_end_date,
                    category_rows, await weekly_cur.fetchall(),
                    await monthly_cur.fetchall(),
                    [
                        {"start_name": row[0], "end_name": row[1], "trip_count": row[2], "total_m": float(row[3])}
                        for row in await routes_cur.fetchall()
                    ],
                    [{"name": row[0], "visit_count": row[1]} for row in await places_cur.fetchall()],
                    (await unnamed_cur.fetchone())[0],
                    drill_url,
                )

                cross_year_rows = await fetch_cross_year_rows(conn, tz, vehicle_id)
                years_present = sorted({row[0] for row in cross_year_rows})
                multiyear_years, cutoff_month, cutoff_day = multiyear_window(
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

                multiyear = await owned_thread(build_multiyear_chart,
                    cross_year_rows, multiyear_years, cutoff_month, cutoff_day, multiyear_drill_url
                )
                trend = await owned_thread(build_share_trend,cross_year_rows, multiyear_years, cutoff_month)

                mileage_rows, expense_rows = await fetch_vehicle_rows(conn, period, tz)
                rates = await load_rates(conn)
                vehicle_breakdown = await owned_thread(build_vehicle_breakdown,
                    mileage_rows,
                    expense_rows,
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
