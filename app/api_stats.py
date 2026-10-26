"""API v1 weekly dashboard and Stats: numbers only.

Both build on the services the web pages use, so their totals match the
web's for the same account and period. Responses carry meters, counts,
shares, display-zone dates and decimal-string money by period and category;
the client draws its own charts, so no chart geometry, colors or labels are
returned.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api_common import MAX_YEAR, MIN_YEAR, error_responses
from app.api_expenses import api_day, current_year, money
from app.api_v1 import ApiUser, require_api_user
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.stats import CATEGORIES as STATS_CATEGORIES
from app.stats_queries import StatsSummary, fetch_stats_summary, stats_period
from app.week_dashboard import build_week, fetch_week_inputs, missing_trip_threshold


class Distance(BaseModel):
    total_m: float
    business_m: float
    personal_m: float
    unclassified_m: float
    nondeductible_m: float


class DayDistance(Distance):
    date: str


class WeekDashboardResponse(BaseModel):
    week_start: str
    week_end: str
    previous_week_start: str
    next_week_start: str
    is_current_week: bool
    trip_count: int
    distance: Distance
    days: list[DayDistance]
    expense_total: str
    deduction: str | None
    missing_trip_count: int


class CategoryTotal(BaseModel):
    category: str
    trip_count: int
    distance_m: float


class YearTotals(BaseModel):
    trip_count: int
    distance_m: float
    business_share: float | None
    categories: list[CategoryTotal]


class MonthDistance(Distance):
    month: int


class YearSeriesItem(BaseModel):
    year: int
    months: list[MonthDistance]


class Multiyear(BaseModel):
    through_month: int
    years: list[YearSeriesItem]


class QuarterShare(BaseModel):
    year: int
    quarter: int
    business_m: float
    personal_m: float
    nondeductible_m: float
    business_share: float | None


class ShareTrend(BaseModel):
    quarters: list[QuarterShare]
    future_quarters_excluded: bool


class VehicleTotals(BaseModel):
    vehicle_id: int | None
    vehicle_name: str | None
    total_m: float
    business_m: float
    personal_m: float
    unclassified_m: float
    nondeductible_m: float
    expense_total: str
    deduction: str | None


class VehicleBreakdown(BaseModel):
    items: list[VehicleTotals]
    unassigned_trip_count: int


class StatsResponse(BaseModel):
    year: int
    period_start: str
    period_end: str
    totals: YearTotals
    multiyear: Multiyear
    share_trend: ShareTrend
    vehicles: VehicleBreakdown


def _distance(source) -> dict:
    return {"total_m": source.total_m, "business_m": source.business_m, "personal_m": source.personal_m,
            "unclassified_m": source.unclassified_m, "nondeductible_m": source.nondeductible_m}


def _stats(summary: StatsSummary) -> StatsResponse:
    totals = summary.totals
    meters = {category: totals.get(category, (0, 0.0))[1] for category in STATS_CATEGORIES}
    classified = meters["business"] + meters["personal"] + meters["nondeductible"]
    breakdown = summary.vehicles
    return StatsResponse(
        year=summary.period.year,
        period_start=summary.period.start_date.isoformat(),
        period_end=summary.period.end_date.isoformat(),
        totals=YearTotals(
            trip_count=sum(totals.get(category, (0, 0.0))[0] for category in STATS_CATEGORIES),
            distance_m=sum(meters.values()),
            business_share=meters["business"] / classified if classified else None,
            categories=[
                CategoryTotal(category=category, trip_count=totals.get(category, (0, 0.0))[0],
                              distance_m=meters[category])
                for category in STATS_CATEGORIES
            ],
        ),
        multiyear=Multiyear(
            through_month=summary.through_month,
            years=[
                YearSeriesItem(year=series.year, months=[
                    MonthDistance(month=index + 1, **_distance(bucket))
                    for index, bucket in enumerate(series.monthly_buckets)
                ])
                for series in summary.multiyear
            ],
        ),
        share_trend=ShareTrend(
            quarters=[
                QuarterShare(year=year, quarter=quarter, business_m=bucket.business_m,
                             personal_m=bucket.personal_m, nondeductible_m=bucket.nondeductible_m,
                             business_share=bucket.business_share)
                for year, quarter, bucket in summary.quarters
            ],
            future_quarters_excluded=summary.quarters_clamped,
        ),
        vehicles=VehicleBreakdown(
            items=[
                VehicleTotals(
                    vehicle_id=vehicle.vehicle_id,
                    vehicle_name=None if vehicle.vehicle_id is None else vehicle.vehicle_name,
                    total_m=vehicle.total_m, business_m=vehicle.business_m, personal_m=vehicle.personal_m,
                    unclassified_m=vehicle.unclassified_m, nondeductible_m=vehicle.nondeductible_m,
                    expense_total=money(summary.vehicle_expenses.get(vehicle.vehicle_id, 0)),
                    deduction=money(vehicle.deduction),
                )
                for vehicle in breakdown.vehicles
            ],
            unassigned_trip_count=breakdown.unassigned_trip_count,
        ),
    )


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/dashboard/week", response_model=WeekDashboardResponse,
                responses=error_responses(401, 422, 429, 503))
    @capacity_policy("navigation")
    async def week(
        request: Request,
        api_user: ApiUser = Depends(require_api_user),
        date: str | None = Query(None),
    ):
        """The local week holding `date` (any day of it), or this week."""
        config = request.state.config
        tz = config.display_tz
        now = datetime.now(tz)
        anchor = now.date() if date is None else api_day("date", date)
        async with request.state.account_pool.connection() as conn:
            inputs = await fetch_week_inputs(conn, anchor, tz)
        dashboard = await build_week(inputs, anchor, tz, now, missing_trip_threshold(config))
        nav = dashboard.nav
        return WeekDashboardResponse(
            week_start=nav.week_start.isoformat(), week_end=nav.week_end.isoformat(),
            previous_week_start=nav.prev_week_start.isoformat(),
            next_week_start=nav.next_week_start.isoformat(), is_current_week=nav.is_current_week,
            trip_count=dashboard.trip_count,
            distance=Distance(**_distance(dashboard.distance)),
            days=[DayDistance(date=day.day.isoformat(), **_distance(day)) for day in dashboard.daily_series],
            expense_total=money(dashboard.expense_total),
            deduction=money(dashboard.deduction.amount) if dashboard.deduction.available else None,
            missing_trip_count=0 if dashboard.attention is None else dashboard.attention.missing_trip_count,
        )

    @router.get("/api/v1/stats", response_model=StatsResponse, responses=error_responses(401, 422, 429, 503))
    @capacity_policy("foreground")
    async def stats(
        request: Request,
        api_user: ApiUser = Depends(require_api_user),
        year: int | None = Query(None, ge=MIN_YEAR, le=MAX_YEAR),
    ):
        tz = request.state.config.display_tz
        now = datetime.now(tz)
        period = stats_period(year or current_year(request), tz, now)
        async with request.state.account_pool.connection() as conn:
            summary = await fetch_stats_summary(conn, period, tz, now)
        return _stats(summary)

    return router
