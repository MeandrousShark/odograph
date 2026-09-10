"""Presentation-model tests for the multi-year monthly comparison chart."""
from __future__ import annotations

import re
from xml.etree import ElementTree

from app.stats_multiyear import build_multiyear_chart


def _assert_chart_accessibility_contract(chart_svg: str) -> None:
    root = ElementTree.fromstring(chart_svg)
    assert root.tag == "svg"
    assert root.get("role") != "img"
    assert root.get("aria-label", "").strip()
    chart_title = root.find("title")
    assert chart_title is not None
    assert (chart_title.text or "").strip()
    rects = root.findall(".//rect")
    assert rects
    for rect in rects:
        bar_title = rect.find("title")
        assert bar_title is not None
        assert (bar_title.text or "").strip()


def _rows():
    return [
        (2024, 1, "business", 2, 1_609.344),
        (2024, 2, "personal", 1, 3_218.688),
        (2025, 1, "business", 3, 4_828.032),
        (2025, 3, "unclassified", 1, 1_609.344),
        (2026, 1, "business", 1, 1_609.344),
        (2026, 7, "personal", 2, 3_218.688),
        (2026, 8, "business", 1, 1_609.344),
    ]


def test_mid_month_cutoff_clamps_to_complete_months_with_coverage_note():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15)

    assert chart.chart_svg.count('text-anchor="middle"') == 7
    assert chart.coverage_note is not None
    assert "Jul" in chart.coverage_note


def test_year_end_cutoff_shows_full_year_with_no_coverage_note():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=12, cutoff_day=31)

    assert chart.chart_svg.count('text-anchor="middle"') == 12
    assert chart.coverage_note is None


def test_single_year_and_empty_rows_do_not_crash():
    empty = build_multiyear_chart([], [2026], cutoff_month=8, cutoff_day=15)
    assert empty.years == [2026]
    assert "<rect " not in empty.chart_svg

    single = build_multiyear_chart(_rows(), [2026], cutoff_month=8, cutoff_day=15)
    assert single.years == [2026]
    assert "<rect " in single.chart_svg


def test_multi_year_chart_root_and_data_bars_have_accessible_titles():
    chart = build_multiyear_chart(
        _rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15
    )

    _assert_chart_accessibility_contract(chart.chart_svg)


def test_svg_uses_per_year_color_custom_properties():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15)

    assert "var(--year-1)" in chart.chart_svg
    assert "var(--year-2)" in chart.chart_svg
    assert "var(--year-3)" in chart.chart_svg


def test_last_day_of_cutoff_month_includes_that_month():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=31)

    assert chart.chart_svg.count('text-anchor="middle"') == 8


def test_year_with_no_rows_appears_in_legend_but_draws_no_bars():
    rows = [
        (2024, 1, "business", 2, 1_609.344),
        (2026, 1, "business", 1, 1_609.344),
        (2026, 2, "personal", 1, 3_218.688),
    ]
    # 2025 (the middle year, series index 1 -> var(--year-2)) has no rows at
    # all, unlike the other tests' 2025 data.
    chart = build_multiyear_chart(rows, [2024, 2025, 2026], cutoff_month=8, cutoff_day=15)

    assert chart.years == [2024, 2025, 2026]
    assert "var(--year-2)" not in chart.chart_svg

    # 2024 (var(--year-1)) contributes one bar, 2026 (var(--year-3)) two.
    assert chart.chart_svg.count('fill="var(--year-1)"') == 1
    assert chart.chart_svg.count('fill="var(--year-3)"') == 2
    assert chart.chart_svg.count("<rect ") == 3


def test_multi_year_bar_chart_draws_gridlines_and_tick_labels():
    rows = [(2026, 1, "business", 1, 1_000.0)]
    chart = build_multiyear_chart(rows, [2026], cutoff_month=1, cutoff_day=31)

    assert chart.chart_svg.count('class="stats-gridline"') == 5
    assert chart.chart_svg.count('text-anchor="end"') == 5


def test_multi_year_bar_chart_plot_width_uses_independent_margins():
    # Left stays wide enough for tick labels (48); right has nothing drawn
    # in it, so it can be narrower (24). 720 - 48 - 24 = 648.
    rows = [(2026, 1, "business", 1, 1_000.0)]
    chart = build_multiyear_chart(rows, [2026], cutoff_month=1, cutoff_day=31)

    assert 'viewBox="0 0 720 220"' in chart.chart_svg
    assert 'x1="48" y1="170.0" x2="696"' in chart.chart_svg


def test_multi_year_bar_chart_rects_carry_year_month_tooltip_titles():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15)

    assert "</title></rect>" in chart.chart_svg
    assert "2024 Jan: 1.0 mi</title>" in chart.chart_svg
    assert "2024 Feb: 2.0 mi</title>" in chart.chart_svg
    assert "2025 Jan: 3.0 mi</title>" in chart.chart_svg
    assert "2026 Jan: 1.0 mi</title>" in chart.chart_svg
    assert "2026 Jul: 2.0 mi</title>" in chart.chart_svg


def test_multi_year_bar_chart_without_link_fn_has_no_anchors():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15)

    assert "<a href" not in chart.chart_svg


def test_multi_year_bar_chart_link_fn_wraps_rects_with_per_bar_year_and_month():
    seen = []

    def link_fn(year, month):
        seen.append((year, month))
        return f"/trips?year={year}&month={month}"

    chart = build_multiyear_chart(
        _rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15, link_fn=link_fn
    )

    assert "<a href" in chart.chart_svg
    assert "/trips?year=2024&amp;month=1" in chart.chart_svg
    assert "/trips?year=2025&amp;month=1" in chart.chart_svg
    assert "/trips?year=2026&amp;month=1" in chart.chart_svg
    assert (2024, 1) in seen
    assert (2025, 1) in seen
    assert (2026, 1) in seen


def test_multi_year_bar_chart_link_fn_title_stays_nested_inside_rect_inside_anchor():
    def link_fn(year, month):
        return f"/trips?year={year}&month={month}"

    chart = build_multiyear_chart(
        _rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15, link_fn=link_fn
    )

    assert (
        '<a href="/trips?year=2024&amp;month=1"><rect' in chart.chart_svg
    )
    assert "<title>2024 Jan: 1.0 mi</title></rect></a>" in chart.chart_svg


def test_multi_year_bar_chart_tick_labels_are_round_miles_not_round_meters():
    # 199_558.656 m is 124.0 mi exactly. Rounding that meter figure directly
    # (the old, buggy behavior) lands on a clean 200_000 m axis max, which
    # divides back down to a decidedly unclean 124 mi top label. Rounding in
    # miles first should instead produce a clean 0/50/100/150/200 axis.
    rows = [(2026, 1, "business", 1, 199_558.656)]
    chart = build_multiyear_chart(rows, [2026], cutoff_month=1, cutoff_day=31)

    tick_labels = re.findall(r'text-anchor="end">([^<]+)</text>', chart.chart_svg)
    assert tick_labels == ["0", "50", "100", "150", "200"]


def test_multi_year_bar_chart_scales_bars_against_nice_axis_max():
    chart = build_multiyear_chart(_rows(), [2024, 2025, 2026], cutoff_month=8, cutoff_day=15)

    # Tallest bucket is 2025 Jan at 4828.032 m (3.0 mi). Raw-max scaling would
    # draw it at the full 150 px plot height; nice_axis_max rounds the axis up
    # in miles (the displayed unit) to 5.0 mi, so the bar should read short
    # of full height: (3.0 / 5.0) * 150 = 90.0.
    assert 'height="90.0"' in chart.chart_svg
    assert 'height="150.0"' not in chart.chart_svg
