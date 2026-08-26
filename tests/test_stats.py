"""Presentation-model tests for the current-year stats dashboard."""
from __future__ import annotations

import re
from datetime import date

import pytest

from app.stats import axis_ticks, build_dashboard, nice_axis_max


def test_dashboard_combines_category_totals_and_builds_current_period_charts():
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 1),
        date(2026, 7, 13),
        [("business", 3, 16_093.44), ("personal", 2, 8_046.72), ("unclassified", 1, 1_609.344)],
        [
            (date(2026, 7, 6), "business", 1, 1_609.344),
            (date(2026, 7, 13), "personal", 1, 3_218.688),
        ],
        [
            (date(2026, 1, 1), "business", 1, 1_609.344),
            (date(2026, 7, 1), "unclassified", 1, 1_609.344),
        ],
        [{"start_name": "Home", "end_name": "Office", "trip_count": 4, "total_m": 12_874.752}],
        [{"name": "Home", "visit_count": 6}],
        2,
    )

    assert dashboard.trip_count == 6
    assert dashboard.total_m == 25_749.504
    assert dashboard.business_share == pytest.approx(2 / 3)
    assert dashboard.unclassified_trips == 1
    assert dashboard.routes[0]["start_name"] == "Home"
    assert dashboard.places[0]["visit_count"] == 6
    assert dashboard.unnamed_trip_count == 2
    assert dashboard.weekly_chart.count('text-anchor="middle"') == 12
    assert dashboard.monthly_chart.count('text-anchor="middle"') == 7
    assert "var(--cat-business)" in dashboard.weekly_chart
    assert "var(--warn)" in dashboard.monthly_chart


def test_dashboard_zero_data_is_safe_and_has_no_business_share():
    dashboard = build_dashboard(2026, date(2026, 1, 1), date(2026, 1, 2), [], [], [], [], [], 0)

    assert dashboard.trip_count == 0
    assert dashboard.total_m == 0
    assert dashboard.business_share is None
    assert "<rect " not in dashboard.weekly_chart
    assert "aria-label=\"Monthly mileage\"" in dashboard.monthly_chart


def test_dashboard_ignores_out_of_window_and_unknown_bucket_rows():
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 1),
        date(2026, 1, 2),
        [("business", 1, 1000)],
        [(date(2025, 3, 3), "business", 1, 500), (date(2026, 1, 5), "unknown", 1, 500)],
        [(date(2025, 12, 1), "business", 1, 500), (date(2026, 1, 1), "unknown", 1, 500)],
        [], [], 0,
    )

    assert dashboard.trip_count == 1
    assert "<rect " not in dashboard.weekly_chart
    assert "<rect " not in dashboard.monthly_chart


def test_dashboard_past_year_shows_all_twelve_months():
    dashboard = build_dashboard(
        2025, date(2025, 1, 1), date(2025, 12, 31), [], [], [], [], [], 0,
    )

    assert dashboard.monthly_chart.count('text-anchor="middle"') == 12


def test_dashboard_date_range_filter_limits_monthly_buckets():
    dashboard = build_dashboard(
        2026, date(2026, 6, 1), date(2026, 8, 31), [], [], [], [], [], 0,
    )

    assert dashboard.monthly_chart.count('text-anchor="middle"') == 3


def test_dashboard_weekly_buckets_clipped_to_period_start():
    dashboard = build_dashboard(
        2026, date(2026, 8, 10), date(2026, 8, 24), [], [], [], [], [], 0,
    )

    assert dashboard.weekly_chart.count('text-anchor="middle"') == 3


def test_dashboard_weekly_chart_includes_partial_first_week():
    # 2026-01-01 is a Thursday; its week bucket key is 2025-12-29 (Monday)
    dashboard = build_dashboard(
        2026, date(2026, 1, 1), date(2026, 1, 10),
        [("business", 1, 1609.34)],
        [(date(2025, 12, 29), "business", 1, 1609.34)],
        [(date(2026, 1, 1), "business", 1, 1609.34)],
        [], [], 0,
    )
    assert "<rect " in dashboard.weekly_chart


def test_dashboard_without_link_fn_has_no_anchors():
    dashboard = build_dashboard(
        2026, date(2026, 1, 1), date(2026, 1, 10),
        [("business", 1, 1609.34)],
        [(date(2025, 12, 29), "business", 1, 1609.34)],
        [(date(2026, 1, 1), "business", 1, 1609.34)],
        [], [], 0,
    )
    assert "<a href" not in dashboard.weekly_chart
    assert "<a href" not in dashboard.monthly_chart


def test_dashboard_link_fn_wraps_rects_with_per_bucket_dates():
    seen = []

    def link_fn(period_start, period_end, category):
        seen.append((period_start, period_end, category))
        return f"/trips?from={period_start.isoformat()}&to={period_end.isoformat()}&category={category}"

    dashboard = build_dashboard(
        2026, date(2026, 1, 1), date(2026, 1, 10),
        [("business", 1, 1609.34)],
        [(date(2025, 12, 29), "business", 1, 1609.34)],
        [(date(2026, 1, 1), "business", 1, 1609.34)],
        [], [], 0,
        link_fn,
    )

    assert "<a href" in dashboard.weekly_chart
    assert "<a href" in dashboard.monthly_chart
    assert (
        "/trips?from=2025-12-29&amp;to=2026-01-04&amp;category=business"
        in dashboard.weekly_chart
    )
    assert (
        "/trips?from=2026-01-01&amp;to=2026-01-31&amp;category=business"
        in dashboard.monthly_chart
    )
    assert (date(2025, 12, 29), date(2026, 1, 4), "business") in seen
    assert (date(2026, 1, 1), date(2026, 1, 31), "business") in seen


@pytest.mark.parametrize(
    "value, expected",
    [
        (847.3, 1000),
        (1200, 2000),
        (30, 50),
        (1000, 1000),
    ],
)
def test_nice_axis_max_rounds_up_to_a_readable_bound(value, expected):
    assert nice_axis_max(value) == expected


def test_nice_axis_max_guards_zero_and_negative_input():
    assert nice_axis_max(0) == 1.0
    assert nice_axis_max(-5) == 1.0


def test_axis_ticks_spans_zero_to_axis_max_with_requested_count():
    ticks = axis_ticks(847.3, count=4)

    assert len(ticks) == 5
    assert ticks[0] == (0.0, 0.0)
    assert ticks[-1] == (1000.0, 1.0)
    assert ticks[2] == (500.0, 0.5)


def test_axis_ticks_default_count_is_four_ticks_plus_baseline():
    assert len(axis_ticks(200)) == 5


def test_stacked_bar_chart_draws_gridlines_and_tick_labels():
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 1),
        date(2026, 1, 2),
        [("business", 1, 1000)],
        [(date(2026, 1, 1), "business", 1, 1000)],
        [(date(2026, 1, 1), "business", 1, 1000)],
        [], [], 0,
    )

    assert dashboard.monthly_chart.count('class="stats-gridline"') == 5
    assert dashboard.monthly_chart.count('text-anchor="end"') == 5


def test_stacked_bar_chart_plot_width_uses_independent_margins():
    # Left stays wide enough for tick labels (48); right has nothing drawn
    # in it, so it can be narrower (24). 720 - 48 - 24 = 648.
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 1),
        date(2026, 1, 2),
        [("business", 1, 1000)],
        [(date(2026, 1, 1), "business", 1, 1000)],
        [(date(2026, 1, 1), "business", 1, 1000)],
        [], [], 0,
    )

    assert 'viewBox="0 0 720 220"' in dashboard.monthly_chart
    assert 'x1="48" y1="170.0" x2="696"' in dashboard.monthly_chart


def test_stacked_bar_chart_tick_labels_are_round_miles_not_round_meters():
    # 199_558.656 m is 124.0 mi exactly. Rounding that meter figure directly
    # (the old, buggy behavior) lands on a clean 200_000 m axis max, which
    # divides back down to a decidedly unclean 124 mi top label. Rounding in
    # miles first should instead produce a clean 0/50/100/150/200 axis.
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 1),
        date(2026, 1, 2),
        [("business", 1, 199_558.656)],
        [(date(2026, 1, 1), "business", 1, 199_558.656)],
        [(date(2026, 1, 1), "business", 1, 199_558.656)],
        [], [], 0,
    )

    tick_labels = re.findall(r'text-anchor="end">([^<]+)</text>', dashboard.monthly_chart)
    assert tick_labels == ["0", "50", "100", "150", "200"]


def test_stacked_bar_chart_rects_carry_tooltip_titles():
    # 2026-01-01 is a Thursday; its week bucket key is 2025-12-29 (Monday).
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 1),
        date(2026, 1, 10),
        [("business", 1, 1609.344), ("personal", 1, 1609.344)],
        [
            (date(2025, 12, 29), "business", 1, 1609.344),
            (date(2025, 12, 29), "personal", 1, 1609.344),
        ],
        [
            (date(2026, 1, 1), "business", 1, 1609.344),
            (date(2026, 1, 1), "personal", 1, 1609.344),
        ],
        [], [], 0,
    )

    assert "<rect " in dashboard.weekly_chart
    assert "</title></rect>" in dashboard.weekly_chart
    assert "business: 1.0 mi</title>" in dashboard.weekly_chart
    assert "personal: 1.0 mi</title>" in dashboard.weekly_chart
    assert "Jan business: 1.0 mi</title>" in dashboard.monthly_chart


def test_stacked_bar_chart_link_fn_rects_still_carry_tooltip_titles():
    def link_fn(period_start, period_end, category):
        return f"/trips?from={period_start.isoformat()}&to={period_end.isoformat()}&category={category}"

    dashboard = build_dashboard(
        2026, date(2026, 1, 1), date(2026, 1, 10),
        [("business", 1, 1609.34)],
        [(date(2025, 12, 29), "business", 1, 1609.34)],
        [(date(2026, 1, 1), "business", 1, 1609.34)],
        [], [], 0,
        link_fn,
    )

    assert "<a href" in dashboard.weekly_chart
    assert "<a href" in dashboard.monthly_chart
    # The rect-level <title> tooltip must stay nested inside the drill-down
    # <a>, not just present anywhere in the chart (the chart already has its
    # own top-level <title> for accessibility).
    assert "</title></rect></a>" in dashboard.weekly_chart
