"""Month calendar presentation model, the pure core behind `GET /calendar`
(app/ui/stats.py). Day totals come from `daily_distances` in app/dashboard.py,
the same pass the week chart uses, so a calendar day cannot differ from the
dashboard's bar for that day.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.dashboard import DailyDistanceBreakdown, daily_distances

# The same year bounds as the place page's `year` query.
MIN_YEAR = 1
MAX_YEAR = 9998
_MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{2})")


@dataclass(frozen=True)
class CalendarDay:
    """A grid cell. Days from the neighboring months carry no distance or link."""

    day: date
    in_month: bool
    is_today: bool
    distance: DailyDistanceBreakdown | None
    archive_url: str | None


@dataclass(frozen=True)
class MonthNav:
    month: date
    prev_month: date | None
    next_month: date | None
    is_current_month: bool


@dataclass(frozen=True)
class MonthCalendar:
    month: date
    weeks: list[list[CalendarDay]]
    max_day_m: float
    nav: MonthNav


def parse_month(value: str, today: date) -> date:
    """The first day of the requested month; an empty value is `today`'s
    month. Raises ValueError for anything but `YYYY-MM` within the year
    bounds, which the route reports as a 422.
    """
    if not value:
        return today.replace(day=1)
    match = _MONTH_RE.fullmatch(value)
    if match is None:
        raise ValueError("month must be YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not MIN_YEAR <= year <= MAX_YEAR or not 1 <= month <= 12:
        raise ValueError("month out of range")
    return date(year, month, 1)


def _next_month(month: date) -> date:
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


def _prev_month(month: date) -> date | None:
    if month.month == 1:
        return None if month.year == MIN_YEAR else date(month.year - 1, 12, 1)
    return date(month.year, month.month - 1, 1)


def month_grid(month: date) -> list[list[date]]:
    """Monday-first weeks covering the month, with the neighboring months'
    days filling the first and last week."""
    first = month.replace(day=1)
    last = _next_month(first) - timedelta(days=1)
    day = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    weeks = []
    while day <= end:
        weeks.append([day + timedelta(days=offset) for offset in range(7)])
        day += timedelta(days=7)
    return weeks


def build_month_calendar(
    trips: list[dict],
    month: date,
    tz: ZoneInfo,
    today: date,
    day_url: Callable[[date], str],
) -> MonthCalendar:
    """Build the month's grid from its already-fetched trip rows.

    Next is unavailable from the current month on, as the dashboard's next
    week is on the current week; a later month reached by URL still renders.
    """
    first = month.replace(day=1)
    following = _next_month(first)
    day_count = (following - first).days
    by_day = {entry.day: entry for entry in daily_distances(trips, tz, first, day_count)}

    weeks = [
        [
            CalendarDay(
                day=day,
                in_month=day in by_day,
                is_today=day == today,
                distance=by_day.get(day),
                archive_url=day_url(day) if day in by_day else None,
            )
            for day in week
        ]
        for week in month_grid(first)
    ]
    current = today.replace(day=1)
    nav = MonthNav(
        month=first,
        prev_month=_prev_month(first),
        next_month=None if first >= current else following,
        is_current_month=first == current,
    )
    return MonthCalendar(
        month=first,
        weeks=weeks,
        max_day_m=max((entry.total_m for entry in by_day.values()), default=0.0),
        nav=nav,
    )
