"""Pure presentation model for the business/personal share trend chart.

Like app/stats.py, this module owns only the display math: the database
handles aggregation and the caller supplies raw per-month category rows, so
an empty dataset or a partially elapsed current year is testable without a
live Postgres connection or a frozen system clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Iterable

from app.rates import METERS_PER_MILE
from app.stats import CATEGORY_COLORS

QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}
MIN_STEP = 56
COVERAGE_NOTE = (
    "Quarters that have not started yet are excluded. The current quarter "
    "is still in progress."
)


@dataclass(frozen=True)
class ShareBucket:
    label: str
    business_m: float
    personal_m: float
    business_share: float | None


@dataclass(frozen=True)
class ShareTrend:
    chart_svg: str
    coverage_note: str | None
    plottable_quarters: int = 0


def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _quarterly_buckets(
    rows: Iterable[tuple[int, int, str, int, float]],
    years: list[int],
    cutoff_month: int,
) -> tuple[list[ShareBucket], bool]:
    """Aggregate raw (year, month, category, count, meters) rows into quarters.

    A quarter's last calendar month reaching cutoff_month counts as complete,
    so the most recent year in `years` is clamped to the same elapsed period
    as prior years, and any quarter entirely beyond that point is dropped.
    Older years always show their full four quarters since the whole year
    has already elapsed regardless of cutoff_month. Without this, a
    still-accumulating final quarter would silently pull the most recent
    year's share toward whatever's happened so far, misleading a
    year-over-year comparison.
    """
    if not years:
        return [], False

    totals: dict[tuple[int, int], dict[str, float]] = {}
    for year, month, category, _count, meters in rows:
        if category not in ("business", "personal"):
            continue
        key = (year, _quarter_of(month))
        amounts = totals.setdefault(key, {"business": 0.0, "personal": 0.0})
        amounts[category] += float(meters)

    max_year = max(years)
    clamped = False
    buckets: list[ShareBucket] = []
    for year in sorted(years):
        for quarter in (1, 2, 3, 4):
            if year == max_year:
                first_month, last_month = QUARTER_MONTHS[quarter]
                if last_month <= cutoff_month:
                    pass  # fully elapsed
                elif first_month <= cutoff_month:
                    pass  # in progress; still shown as the current quarter
                else:
                    clamped = True
                    continue  # entirely future, excluded
            amounts = totals.get((year, quarter), {"business": 0.0, "personal": 0.0})
            business_m = amounts["business"]
            personal_m = amounts["personal"]
            classified = business_m + personal_m
            buckets.append(
                ShareBucket(
                    label=f"Q{quarter} {year}",
                    business_m=business_m,
                    personal_m=personal_m,
                    business_share=(business_m / classified) if classified else None,
                )
            )
    return buckets, clamped


def _share_trend_chart(title: str, buckets: list[ShareBucket]) -> str:
    """Return a self-contained SVG percentage-stacked bar chart of business share.

    Bars are normalized to 100% height rather than raw mileage: the point of
    this chart is the business/personal split, not trip volume, which the
    mileage charts in app/stats.py already cover. A quarter with no
    classified miles has no meaningful split to plot, so its bar is skipped
    rather than drawn as an empty or misleading zero-height bar.

    A fixed 0/25/50/75/100 percent axis is used instead of app.stats's
    nice_axis_max/axis_ticks: those size an axis to a mileage maximum, but
    every bar here is already normalized to the same 100% height, so the
    scale is constant regardless of the data.

    The SVG width grows with the bucket count (left + right + bucket_count *
    MIN_STEP) rather than staying fixed at 720: at a fixed width, five years
    of quarters packs each "Qn YYYY" label into too few viewBox units to
    render without overlapping, and since the viewBox scales everything
    together, a wider browser window can't fix that on its own. Widening
    the viewBox keeps each bucket at or above MIN_STEP units; because the
    ratio of units per bucket to label width is preserved under scaling,
    this holds regardless of the size the chart is actually rendered at.
    """
    height, top, plot_height = 220, 20, 150
    left, right = 48, 24
    bucket_count = max(len(buckets), 1)
    width = max(720, left + right + bucket_count * MIN_STEP)
    plot_width = width - left - right
    step = plot_width / bucket_count
    bar_width = max(3.0, step * 0.64)
    bars = []
    labels = []
    for i, bucket in enumerate(buckets):
        x = left + i * step + (step - bar_width) / 2
        labels.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{top + plot_height + 17}" '
            f'text-anchor="middle">{escape(bucket.label)}</text>'
        )
        if bucket.business_share is None:
            continue
        business_height = bucket.business_share * plot_height
        personal_height = plot_height - business_height
        business_pct = bucket.business_share * 100
        personal_pct = 100 - business_pct
        business_tip = (
            f"{bucket.label} business: {bucket.business_m / METERS_PER_MILE:.1f} mi "
            f"({business_pct:.0f}%)"
        )
        personal_tip = (
            f"{bucket.label} personal: {bucket.personal_m / METERS_PER_MILE:.1f} mi "
            f"({personal_pct:.0f}%)"
        )
        bars.append(
            f'<rect x="{x:.1f}" y="{top + plot_height - business_height:.1f}" '
            f'width="{bar_width:.1f}" height="{business_height:.1f}" '
            f'fill="{CATEGORY_COLORS["business"]}">'
            f"<title>{escape(business_tip)}</title></rect>"
        )
        bars.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_width:.1f}" '
            f'height="{personal_height:.1f}" fill="{CATEGORY_COLORS["personal"]}">'
            f"<title>{escape(personal_tip)}</title></rect>"
        )
    gridlines = []
    tick_labels = []
    for pct in (0, 25, 50, 75, 100):
        y = top + plot_height - (pct / 100) * plot_height
        # 50% is the business/personal break-even point, the one line on
        # this chart worth calling out even now that it's one of five
        # gridlines rather than the only one.
        dash = ' stroke-dasharray="4 3"' if pct == 50 else ""
        gridlines.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" '
            f'y2="{y:.1f}" class="stats-gridline"{dash} />'
        )
        tick_labels.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{pct}%</text>'
        )
    return (
        f'<svg class="stats-chart" viewBox="0 0 {width} {height}" '
        f'aria-label="{escape(title)}">'
        f'<title>{escape(title)}</title>'
        f"{''.join(gridlines)}{''.join(tick_labels)}"
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" class="stats-axis" />'
        f"{''.join(bars)}{''.join(labels)}</svg>"
    )


def build_share_trend(
    rows: Iterable[tuple[int, int, str, int, float]],
    years: list[int],
    cutoff_month: int,
) -> ShareTrend:
    buckets, clamped = _quarterly_buckets(rows, years, cutoff_month)
    chart_svg = _share_trend_chart("Business vs. personal share by quarter", buckets)
    plottable_quarters = sum(1 for b in buckets if b.business_share is not None)
    return ShareTrend(
        chart_svg=chart_svg,
        coverage_note=COVERAGE_NOTE if clamped else None,
        plottable_quarters=plottable_quarters,
    )
