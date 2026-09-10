"""Pure presentation model for the multi-year monthly mileage comparison.

Comparing this year against past years only makes sense month-for-month
through the same point in the calendar: a mid-August view showing Jan-Dec for
2024 and Jan-Aug for 2026 would make the current year look artificially low.
This module clamps every year to the same "complete months" window before
handing buckets to the chart, and stays I/O-free so that window logic is
testable without freezing the system clock.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from html import escape
from typing import Callable, Iterable

from app.rates import METERS_PER_MILE
from app.stats import CATEGORIES, Bucket, axis_ticks

# Only --year-1 through --year-5 are defined in CSS. Callers are expected to
# pass at most this many years; a sixth year reuses a color via modulo
# rather than referencing an undefined custom property, which would render
# an invisible bar. A repeated color is the better failure mode.
YEAR_PALETTE_SIZE = 5


@dataclass(frozen=True)
class YearSeries:
    year: int
    monthly_buckets: list[Bucket]


@dataclass(frozen=True)
class MultiYearChart:
    chart_svg: str
    years: list[int]
    coverage_note: str | None


def _last_included_month(cutoff_month: int, cutoff_day: int) -> int:
    """Return the last month that is "complete" as of the cutoff date.

    A month only counts once every compared year could have finished
    reporting trips for it, so the current in-progress month is dropped
    unless the cutoff day is itself that month's last day. Month length is
    resolved against a non-leap reference year, so a cutoff of Feb 28 in a
    leap year is treated as one day short of complete; this is a minor,
    rare edge case given callers only pass month/day, not a full date.
    """
    if not 1 <= cutoff_month <= 12:
        return 0
    last_day = calendar.monthrange(2001, cutoff_month)[1]
    if cutoff_day >= last_day:
        return cutoff_month
    return cutoff_month - 1


def _year_series_for(
    year: int, last_month: int, row_map: dict[tuple[int, int, str], float]
) -> YearSeries:
    buckets = [
        Bucket(
            calendar.month_abbr[month],
            row_map.get((year, month, "business"), 0.0),
            row_map.get((year, month, "personal"), 0.0),
            row_map.get((year, month, "unclassified"), 0.0),
            row_map.get((year, month, "nondeductible"), 0.0),
        )
        for month in range(1, last_month + 1)
    ]
    return YearSeries(year, buckets)


def _multi_year_bar_chart(
    title: str,
    year_series_list: list[YearSeries],
    link_fn: Callable[[int, int], str | None] | None = None,
) -> str:
    """Return a self-contained SVG with grouped, year-over-year monthly bars.

    Bars are grouped by month (one bar per year per group) rather than
    stacked by category as in `_stacked_bar_chart`, since the point here is
    comparing total volume across years, not the business/personal split.

    `link_fn`, when given, is called with the bar's own year and calendar
    month (1-12) for every drawn rect; a None return leaves that rect
    unwrapped. Unlike `_stacked_bar_chart`'s index-based `link_fn`, this one
    takes the year and month directly since both are already on hand here,
    with no need for a caller-side index-to-date adapter.
    """
    width, height, left, right, top, plot_height = 720, 220, 48, 24, 20, 150
    plot_width = width - left - right
    month_count = max((len(ys.monthly_buckets) for ys in year_series_list), default=0)
    max_m = max(
        (bucket.total_m for ys in year_series_list for bucket in ys.monthly_buckets),
        default=0.0,
    ) or 1.0
    # Rounding has to happen in the unit the axis actually displays (miles),
    # not the storage unit (meters); rounding meters to a clean number and
    # then converting to miles for the label just moves the ugly number to
    # the part a human reads.
    ticks = axis_ticks(max_m / METERS_PER_MILE)
    axis_max_miles = ticks[-1][0]
    axis_max = axis_max_miles * METERS_PER_MILE
    step = plot_width / max(month_count, 1)
    year_count = max(len(year_series_list), 1)
    group_width = step * 0.78
    bar_width = max(2.0, group_width / year_count * 0.8)
    bars = []
    labels = []
    for month_index in range(month_count):
        group_x = left + month_index * step + (step - group_width) / 2
        label = next(
            (
                ys.monthly_buckets[month_index].label
                for ys in year_series_list
                if month_index < len(ys.monthly_buckets)
            ),
            "",
        )
        labels.append(
            f'<text x="{left + month_index * step + step / 2:.1f}" '
            f'y="{top + plot_height + 17}" text-anchor="middle">{escape(label)}</text>'
        )
        for series_index, ys in enumerate(year_series_list):
            if month_index >= len(ys.monthly_buckets):
                continue
            bucket = ys.monthly_buckets[month_index]
            if not bucket.total_m:
                continue
            bar_height = (bucket.total_m / axis_max) * plot_height
            x = group_x + series_index * (group_width / year_count)
            y = top + plot_height - bar_height
            palette_index = series_index % YEAR_PALETTE_SIZE + 1
            tip = f"{ys.year} {bucket.label}: {bucket.total_m / METERS_PER_MILE:.1f} mi"
            rect = (
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                f'height="{bar_height:.1f}" fill="var(--year-{palette_index})">'
                f"<title>{escape(tip)}</title></rect>"
            )
            href = link_fn(ys.year, month_index + 1) if link_fn else None
            if href is not None:
                rect = f'<a href="{escape(href)}">{rect}</a>'
            bars.append(rect)
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


def build_multiyear_chart(
    rows: Iterable[tuple[int, int, str, int, float]],
    years: list[int],
    cutoff_month: int,
    cutoff_day: int,
    link_fn: Callable[[int, int], str | None] | None = None,
) -> MultiYearChart:
    last_month = _last_included_month(cutoff_month, cutoff_day)
    row_map: dict[tuple[int, int, str], float] = {}
    for year, month, category, _count, meters in rows:
        if category not in CATEGORIES:
            continue
        key = (year, month, category)
        row_map[key] = row_map.get(key, 0.0) + float(meters)
    year_series_list = [_year_series_for(year, last_month, row_map) for year in years]
    chart_svg = _multi_year_bar_chart("Year-over-year monthly miles", year_series_list, link_fn)
    coverage_note = None
    if 0 < last_month < 12:
        coverage_note = (
            f"All years shown through {calendar.month_abbr[last_month]} "
            "to match the current period."
        )
    return MultiYearChart(chart_svg=chart_svg, years=list(years), coverage_note=coverage_note)
