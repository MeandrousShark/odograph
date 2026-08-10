"""Weekly dashboard presentation model. Pure core (this module) + thin I/O
wrapper (the `GET /` route in app/ui.py), same "pure function, thin wrapper"
split as `app/rates.py` and `app/missing_trip.py` -- the week-normalization
and bucketing rules are the part that must be exactly right, so they live
here where they're testable without a database.

Nothing in this module issues a query or knows about FastAPI/Jinja; it
consumes already-fetched `TRIP_COLUMNS` rows (app/ui.py) and an
already-summed expense total, and returns a tree of frozen dataclasses for a
template to render.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.missing_trip import MissingTripBadge, missing_trip_badge
from app.rates import YearRate, deduction


@dataclass(frozen=True)
class WeekBounds:
    monday: date
    start: datetime
    end: datetime


@dataclass(frozen=True)
class DistanceBreakdown:
    """`unclassified_m` is never folded into `business_m` or `personal_m`:
    an unclassified trip has no known tax treatment yet, and silently
    counting its miles toward either bucket would misstate the very number
    the attention strip exists to flag as unresolved.
    """
    total_m: float
    business_m: float
    personal_m: float
    unclassified_m: float


@dataclass(frozen=True)
class DeductionEstimate:
    """`available=False` (and `amount=None`) means at least one local
    `(year, month)` bucket with nonzero business meters has no published
    rate for that period. A partial sum silently omitting that bucket would
    look like a real number while actually understating the deduction --
    worse than an honest "unavailable, add a rate" prompt, since a user has
    no way to tell a partial total from a complete one just by looking at it.
    """
    amount: float | None
    available: bool


@dataclass(frozen=True)
class DayGroup:
    day: date
    is_today: bool
    is_yesterday: bool
    trips: list[dict]


@dataclass(frozen=True)
class AttentionStrip:
    unclassified_count: int
    review_url: str
    missing_trip_count: int
    missing_trip_url: str | None


@dataclass(frozen=True)
class WeekNav:
    week_start: date
    week_end: date
    prev_week_start: date
    next_week_start: date
    is_current_week: bool


@dataclass(frozen=True)
class WeekDashboard:
    trip_count: int
    distance: DistanceBreakdown
    expense_total: Decimal | float
    deduction: DeductionEstimate
    day_groups: list[DayGroup]
    attention: AttentionStrip | None
    nav: WeekNav


def format_week_range(start: date, end: date) -> str:
    """Format a dashboard week as a friendly US date range."""
    return (
        f"{calendar.month_abbr[start.month]} {start.day}, {start.year} - "
        f"{calendar.month_abbr[end.month]} {end.day}, {end.year}"
    )


def parse_week_anchor(week_str: str, tz: ZoneInfo, now: datetime) -> date:
    """Parse the `week` query value as an anchor date, same forgiving
    posture as `parse_date_range` (app/ui.py): a bookmarked or hand-edited
    `?week=` is never worth a 400, and there's no meaningful "invalid week"
    state for a dashboard to render, so absent/malformed/nonsense values all
    fall back to today. The caller still normalizes whatever this returns
    (including a valid-but-arbitrary weekday) through `week_bounds`.
    """
    if week_str:
        try:
            return date.fromisoformat(week_str)
        except ValueError:
            pass
    return now.astimezone(tz).date()


def week_bounds(anchor_date: date, tz: ZoneInfo) -> WeekBounds:
    """Normalize `anchor_date` to its containing Monday-based local week and
    return that Monday plus half-open aware-datetime bounds
    (`start <= t < end`).

    `start`/`end` are built by attaching `tz` directly to local calendar-date
    components (`datetime(year, month, day, tzinfo=tz)`), the same
    convention `_month_bounds` (app/ui.py) uses for month boundaries --
    never by adding a `timedelta` to an already-aware datetime, which would
    silently smear across a DST transition (e.g. "add 7 days" landing at
    23:00 or 01:00 instead of local midnight). Building from calendar dates
    instead means a week containing a spring-forward transition is honestly
    167 wall-clock hours, and one containing a fall-back transition is 169,
    rather than a boundary that's off by an hour without anyone noticing.
    """
    monday = anchor_date - timedelta(days=anchor_date.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    next_monday = monday + timedelta(days=7)
    end = datetime(next_monday.year, next_monday.month, next_monday.day, tzinfo=tz)
    return WeekBounds(monday=monday, start=start, end=end)


def build_week_dashboard(
    trips: list[dict],
    expense_total: Decimal | float,
    rates: dict[int, YearRate],
    week_start: date,
    tz: ZoneInfo,
    now: datetime,
    missing_trip_threshold_m: float,
) -> WeekDashboard:
    """Build the full dashboard model from one week's already-fetched
    `TRIP_COLUMNS` rows and one already-summed expense total.

    Every summary figure below (trip count, distance breakdown, deduction
    buckets, attention counts) is derived from the same `trips` list that
    becomes `day_groups` -- never a second aggregate query -- so the summary
    cards and the trip cards underneath them can never drift apart even if a
    filter or a future caching layer changes what "the week's trips" means.

    `week_start` is re-normalized through `week_bounds` rather than trusted
    as an already-correct Monday, so a caller passing an arbitrary date (or
    reusing this function outside the route's own `week_bounds` call) still
    gets a consistent week.
    """
    bounds = week_bounds(week_start, tz)
    monday = bounds.monday
    sunday = monday + timedelta(days=6)

    local_now = now.astimezone(tz)
    today = local_now.date()
    yesterday = today - timedelta(days=1)

    total_m = business_m = personal_m = unclassified_m = 0.0
    business_buckets: dict[tuple[int, int], float] = {}
    day_buckets: dict[date, list[dict]] = {}
    unclassified_count = 0
    badges: list[MissingTripBadge] = []

    for trip in trips:
        distance_m = float(trip.get("display_distance_m") or 0.0)
        total_m += distance_m
        category = trip.get("category")
        if category == "business":
            business_m += distance_m
        elif category == "personal":
            personal_m += distance_m
        else:
            unclassified_m += distance_m
            unclassified_count += 1

        started_at: datetime = trip["started_at"]
        local_started = started_at.astimezone(tz)

        if category == "business" and distance_m:
            key = (local_started.year, local_started.month)
            business_buckets[key] = business_buckets.get(key, 0.0) + distance_m

        day_buckets.setdefault(local_started.date(), []).append(trip)

        badge = missing_trip_badge(trip, missing_trip_threshold_m, tz)
        if badge is not None:
            badges.append(badge)

    distance = DistanceBreakdown(
        total_m=total_m, business_m=business_m,
        personal_m=personal_m, unclassified_m=unclassified_m,
    )

    deduction_total = 0.0
    deduction_available = True
    for (year, month), meters in business_buckets.items():
        priced = deduction(meters, year, rates, month)
        if priced is None:
            deduction_available = False
            break
        deduction_total += priced
    deduction_estimate = DeductionEstimate(
        amount=deduction_total if deduction_available else None,
        available=deduction_available,
    )

    day_groups = [
        DayGroup(
            day=day,
            is_today=day == today,
            is_yesterday=day == yesterday,
            trips=sorted(day_buckets[day], key=lambda t: t["started_at"], reverse=True),
        )
        for day in sorted(day_buckets, reverse=True)
    ]

    review_url = f"/review?from={monday.isoformat()}&to={sunday.isoformat()}"
    attention = None
    if unclassified_count or badges:
        attention = AttentionStrip(
            unclassified_count=unclassified_count,
            review_url=review_url,
            missing_trip_count=len(badges),
            missing_trip_url=badges[0].prefill_url if badges else None,
        )

    current_monday = week_bounds(today, tz).monday
    nav = WeekNav(
        week_start=monday,
        week_end=sunday,
        prev_week_start=monday - timedelta(days=7),
        next_week_start=monday + timedelta(days=7),
        is_current_week=monday == current_monday,
    )

    return WeekDashboard(
        trip_count=len(trips),
        distance=distance,
        expense_total=expense_total,
        deduction=deduction_estimate,
        day_groups=day_groups,
        attention=attention,
        nav=nav,
    )
