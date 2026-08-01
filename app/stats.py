"""Pure presentation model for the current-year stats dashboard.

The database is better at aggregation and timezone bucketing, while this
module owns the small amount of display math needed to ensure the HTML cards
and SVG charts use exactly the same category totals. Keeping it I/O-free also
makes an empty new year and partial current week testable without freezing the
system clock or manufacturing Postgres rows.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from typing import Iterable

CATEGORY_COLORS = {
    "business": "var(--cat-business)",
    "personal": "var(--cat-personal)",
    "unclassified": "var(--warn)",
}
CATEGORIES = tuple(CATEGORY_COLORS)


@dataclass(frozen=True)
class Bucket:
    label: str
    business_m: float = 0.0
    personal_m: float = 0.0
    unclassified_m: float = 0.0

    @property
    def total_m(self) -> float:
        return self.business_m + self.personal_m + self.unclassified_m

    @property
    def business_share(self) -> float | None:
        classified = self.business_m + self.personal_m
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

    @property
    def total_m(self) -> float:
        return self.business_m + self.personal_m + self.unclassified_m

    @property
    def business_share(self) -> float | None:
        classified = self.business_m + self.personal_m
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
        )


def _stacked_bar_chart(title: str, buckets: list[Bucket]) -> str:
    """Return a self-contained SVG with category-stacked mileage bars.

    The chart has no library/runtime dependency and color tokens resolve in
    SVG just as they do in CSS, so the same generated markup follows the
    user's light/dark setting. Labels are escaped even though only calendar
    labels are supplied today, preserving a safe seam if a later chart gains
    user-provided names.
    """
    width, height, left, top, plot_height = 720, 220, 28, 20, 150
    plot_width = width - left * 2
    max_m = max((bucket.total_m for bucket in buckets), default=0.0) or 1.0
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
        ):
            if not meters:
                continue
            bar_height = (meters / max_m) * plot_height
            y -= bar_height
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                f'height="{bar_height:.1f}" fill="{CATEGORY_COLORS[category]}" />'
            )
        labels.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{top + plot_height + 17}" '
            f'text-anchor="middle">{escape(bucket.label)}</text>'
        )
    return (
        f'<svg class="stats-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(title)}"><title>{escape(title)}</title>'
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - left}" '
        f'y2="{top + plot_height}" class="stats-axis" />'
        f"{''.join(bars)}{''.join(labels)}</svg>"
    )


def build_dashboard(
    year: int, today: date,
    category_rows: Iterable[tuple[str, int, float]],
    weekly_rows: Iterable[tuple[date, str, int, float]],
    monthly_rows: Iterable[tuple[date, str, int, float]],
    routes: list[dict], places: list[dict], unnamed_trip_count: int,
) -> Dashboard:
    amounts = {category: (int(count), float(meters)) for category, count, meters in category_rows}
    week_start = today - timedelta(days=today.weekday())
    weekly = _bucket_map(
        (week_start - timedelta(weeks=offset), (week_start - timedelta(weeks=offset)).strftime("%-d %b"))
        for offset in range(11, -1, -1)
    )
    _add_rows(weekly, weekly_rows)
    monthly = _bucket_map(
        (date(year, month, 1), calendar.month_abbr[month])
        for month in range(1, today.month + 1)
    )
    _add_rows(monthly, monthly_rows)
    business_count, business_m = amounts.get("business", (0, 0.0))
    personal_count, personal_m = amounts.get("personal", (0, 0.0))
    unclassified_count, unclassified_m = amounts.get("unclassified", (0, 0.0))
    return Dashboard(
        year=year,
        trip_count=business_count + personal_count + unclassified_count,
        business_m=business_m,
        personal_m=personal_m,
        unclassified_m=unclassified_m,
        unclassified_trips=unclassified_count,
        weekly_chart=_stacked_bar_chart("Weekly mileage", list(weekly.values())),
        monthly_chart=_stacked_bar_chart("Monthly mileage", list(monthly.values())),
        routes=routes,
        places=places,
        unnamed_trip_count=unnamed_trip_count,
    )
