"""Pure presentation model for the current-year stats dashboard.

The database is better at aggregation and timezone bucketing, while this
module owns the small amount of display math needed to ensure the HTML cards
and SVG charts use exactly the same category totals. Keeping it I/O-free also
makes an empty new year and partial current week testable without freezing the
system clock or manufacturing Postgres rows.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from typing import Callable, Iterable

from app.rates import METERS_PER_MILE

CATEGORY_COLORS = {
    "business": "var(--cat-business)",
    "personal": "var(--cat-personal)",
    "unclassified": "var(--warn)",
    "nondeductible": "var(--overlay0)",
}
CATEGORIES = tuple(CATEGORY_COLORS)


@dataclass(frozen=True)
class Bucket:
    label: str
    business_m: float = 0.0
    personal_m: float = 0.0
    unclassified_m: float = 0.0
    nondeductible_m: float = 0.0

    @property
    def total_m(self) -> float:
        return self.business_m + self.personal_m + self.unclassified_m + self.nondeductible_m

    @property
    def business_share(self) -> float | None:
        classified = self.business_m + self.personal_m + self.nondeductible_m
        return self.business_m / classified if classified else None


@dataclass(frozen=True)
class Dashboard:
    year: int
    trip_count: int
    business_m: float
    personal_m: float
    unclassified_m: float
    unclassified_trips: int
    weekly_chart: str
    monthly_chart: str
    routes: list[dict]
    places: list[dict]
    unnamed_trip_count: int
    nondeductible_m: float = 0.0

    @property
    def total_m(self) -> float:
        return self.business_m + self.personal_m + self.unclassified_m + self.nondeductible_m

    @property
    def business_share(self) -> float | None:
        classified = self.business_m + self.personal_m + self.nondeductible_m
        return self.business_m / classified if classified else None


def _bucket_map(labels: Iterable[tuple[date, str]]) -> dict[date, Bucket]:
    return {key: Bucket(label) for key, label in labels}


def _add_rows(buckets: dict[date, Bucket], rows: Iterable[tuple[date, str, int, float]]) -> None:
    for key, category, _count, meters in rows:
        if key not in buckets or category not in CATEGORIES:
            continue
        old = buckets[key]
        amounts = {category: float(meters)}
        buckets[key] = Bucket(
            old.label,
            old.business_m + amounts.get("business", 0.0),
            old.personal_m + amounts.get("personal", 0.0),
            old.unclassified_m + amounts.get("unclassified", 0.0),
            old.nondeductible_m + amounts.get("nondeductible", 0.0),
        )


def nice_axis_max(value: float) -> float:
    """Round a raw maximum up to a bound that reads as a clean tick value.

    Bare bar charts with no scale only communicate relative size; adding an
    axis is only worth doing if the numbers on it are ones a human would
    actually pick, e.g. 500 rather than 847.3. Rounding to the nearest
    power-of-ten multiple from a small, familiar set (1, 2, 2.5, 5, 10) is
    the standard trick for that. Shared here so every chart in this codebase
    lands on the same tick values for the same data shape.
    """
    if value <= 0:
        return 1.0
    multipliers = (1, 2, 2.5, 5, 10)
    exponent = math.floor(math.log10(value))
    while True:
        for multiplier in multipliers:
            candidate = multiplier * (10 ** exponent)
            if candidate >= value:
                return candidate
        exponent += 1


def axis_ticks(max_value: float, count: int = 4) -> list[tuple[float, float]]:
    """Return `count + 1` evenly spaced (value, fraction) ticks from 0 to axis max.

    `fraction` is 0.0 at the baseline and 1.0 at the top of the plot area.
    Returning fractions instead of pixel positions keeps this helper
    independent of any one chart's geometry, so the weekly/monthly bar chart
    and the sibling multi-year and trend charts can all share it and just
    multiply by their own plot height.
    """
    axis_max = nice_axis_max(max_value)
    return [(axis_max * i / count, i / count) for i in range(count + 1)]


def _stacked_bar_chart(
    title: str,
    buckets: list[Bucket],
    link_fn: Callable[[int, str], str | None] | None = None,
) -> str:
    """Return a self-contained SVG with category-stacked mileage bars.

    The chart has no library/runtime dependency and color tokens resolve in
    SVG just as they do in CSS, so the same generated markup follows the
    user's light/dark setting. Labels are escaped even though only calendar
    labels are supplied today, preserving a safe seam if a later chart gains
    user-provided names.

    `link_fn`, when given, is called with the bucket index and category for
    every drawn rect; a None return leaves that rect unwrapped. Omitting
    `link_fn` entirely must produce byte-identical output to before this
    parameter existed, since existing callers and tests depend on it.
    """
    width, height, left, right, top, plot_height = 720, 220, 48, 24, 20, 150
    plot_width = width - left - right
    max_m = max((bucket.total_m for bucket in buckets), default=0.0) or 1.0
    # Rounding has to happen in the unit the axis actually displays (miles),
    # not the storage unit (meters); rounding meters to a clean number and
    # then converting to miles for the label just moves the ugly number to
    # the part a human reads.
    ticks = axis_ticks(max_m / METERS_PER_MILE)
    axis_max_miles = ticks[-1][0]
    axis_max = axis_max_miles * METERS_PER_MILE
    step = plot_width / max(len(buckets), 1)
    bar_width = max(3.0, step * 0.64)
    bars = []
    labels = []
    for i, bucket in enumerate(buckets):
        x = left + i * step + (step - bar_width) / 2
        y = top + plot_height
        for category, meters in (
            ("business", bucket.business_m),
            ("personal", bucket.personal_m),
            ("unclassified", bucket.unclassified_m),
            ("nondeductible", bucket.nondeductible_m),
        ):
            if not meters:
                continue
            bar_height = (meters / axis_max) * plot_height
            y -= bar_height
            tip = f"{bucket.label} {category}: {meters / METERS_PER_MILE:.1f} mi"
            rect = (
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                f'height="{bar_height:.1f}" fill="{CATEGORY_COLORS[category]}">'
                f"<title>{escape(tip)}</title></rect>"
            )
            href = link_fn(i, category) if link_fn else None
            if href is not None:
                rect = f'<a href="{escape(href)}">{rect}</a>'
            bars.append(rect)
        labels.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{top + plot_height + 17}" '
            f'text-anchor="middle">{escape(bucket.label)}</text>'
        )
    gridlines = []
    tick_labels = []
    for value_miles, fraction in ticks:
        y = top + plot_height - fraction * plot_height
        gridlines.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'class="stats-gridline" />'
        )
        miles_label = f"{value_miles:,.0f}"
        tick_labels.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{escape(miles_label)}</text>'
        )
    return (
        f'<svg class="stats-chart" viewBox="0 0 {width} {height}" '
        f'aria-label="{escape(title)}"><title>{escape(title)}</title>'
        f"{''.join(gridlines)}{''.join(tick_labels)}"
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" class="stats-axis" />'
        f"{''.join(bars)}{''.join(labels)}</svg>"
    )


def build_dashboard(
    year: int, period_start: date, period_end: date,
    category_rows: Iterable[tuple[str, int, float]],
    weekly_rows: Iterable[tuple[date, str, int, float]],
    monthly_rows: Iterable[tuple[date, str, int, float]],
    routes: list[dict], places: list[dict], unnamed_trip_count: int,
    link_fn: Callable[[date, date, str], str | None] | None = None,
) -> Dashboard:
    amounts = {category: (int(count), float(meters)) for category, count, meters in category_rows}
    week_end = period_end - timedelta(days=period_end.weekday())
    weekly_labels = []
    for offset in range(11, -1, -1):
        wk = week_end - timedelta(weeks=offset)
        if wk + timedelta(days=6) >= period_start:
            weekly_labels.append((wk, wk.strftime("%-d %b")))
    weekly = _bucket_map(weekly_labels)
    _add_rows(weekly, weekly_rows)
    month_range = list(range(period_start.month, period_end.month + 1))
    monthly = _bucket_map(
        (date(year, month, 1), calendar.month_abbr[month]) for month in month_range
    )
    _add_rows(monthly, monthly_rows)
    business_count, business_m = amounts.get("business", (0, 0.0))
    personal_count, personal_m = amounts.get("personal", (0, 0.0))
    unclassified_count, unclassified_m = amounts.get("unclassified", (0, 0.0))
    nondeductible_count, nondeductible_m = amounts.get("nondeductible", (0, 0.0))

    weekly_link_fn = None
    monthly_link_fn = None
    if link_fn is not None:
        def weekly_link_fn(i: int, category: str) -> str | None:
            start = weekly_labels[i][0]
            return link_fn(start, start + timedelta(days=6), category)

        def monthly_link_fn(i: int, category: str) -> str | None:
            month = month_range[i]
            start = date(year, month, 1)
            last_day = calendar.monthrange(year, month)[1]
            return link_fn(start, date(year, month, last_day), category)

    return Dashboard(
        year=year,
        trip_count=business_count + personal_count + unclassified_count + nondeductible_count,
        business_m=business_m,
        personal_m=personal_m,
        unclassified_m=unclassified_m,
        unclassified_trips=unclassified_count,
        weekly_chart=_stacked_bar_chart("Weekly mileage", list(weekly.values()), weekly_link_fn),
        monthly_chart=_stacked_bar_chart(
            "Monthly mileage", list(monthly.values()), monthly_link_fn
        ),
        routes=routes,
        places=places,
        unnamed_trip_count=unnamed_trip_count,
        nondeductible_m=nondeductible_m,
    )
