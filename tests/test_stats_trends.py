"""Presentation-model tests for the business vs. personal share trend chart."""
from __future__ import annotations

import re
from xml.etree import ElementTree

import pytest

from app.stats_trends import MIN_STEP, ShareBucket, _quarterly_buckets, _share_trend_chart, build_share_trend


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


def _bucket(buckets, label):
    return next(b for b in buckets if b.label == label)


def _chart_width(chart_svg: str) -> int:
    match = re.search(r'viewBox="0 0 (\d+) \d+"', chart_svg)
    assert match, "chart is missing a viewBox"
    return int(match.group(1))


def test_quarterly_buckets_compute_known_shares_and_chart_shows_both_colors():
    rows = [
        (2024, 2, "business", 4, 800.0),
        (2024, 2, "personal", 1, 200.0),
        (2024, 5, "business", 2, 500.0),
        (2024, 5, "personal", 2, 500.0),
        (2024, 8, "business", 1, 200.0),
        (2024, 8, "personal", 4, 800.0),
    ]
    buckets, clamped = _quarterly_buckets(rows, [2024], 12)

    assert clamped is False
    assert _bucket(buckets, "Q1 2024").business_share == pytest.approx(0.8)
    assert _bucket(buckets, "Q2 2024").business_share == pytest.approx(0.5)
    assert _bucket(buckets, "Q3 2024").business_share == pytest.approx(0.2)

    chart = _share_trend_chart("Business vs. personal share", buckets)
    assert "var(--cat-business)" in chart
    assert "var(--cat-personal)" in chart

    # Q1 2024 has an 80% business share (800m of 1000m). Business height is
    # drawn from the top of the plot area, so its rect's height attribute
    # should equal exactly 80% of plot_height (150, from _share_trend_chart).
    plot_height = 150
    q1_business_height = _bucket(buckets, "Q1 2024").business_share * plot_height
    assert f'height="{q1_business_height:.1f}"' in chart


def test_all_unclassified_quarter_has_no_bar_and_no_share():
    rows = [(2024, 2, "unclassified", 3, 500.0)]
    buckets, _clamped = _quarterly_buckets(rows, [2024], 12)

    q1 = _bucket(buckets, "Q1 2024")
    assert q1.business_share is None

    chart = _share_trend_chart("Business vs. personal share", buckets)
    assert "<rect" not in chart


def test_share_chart_root_and_data_bars_have_accessible_titles():
    buckets = [
        ShareBucket(
            "Q2 2025", 412.3 * 1609.344, 194.1 * 1609.344,
            0.68, 20 * 1609.344,
        )
    ]

    _assert_chart_accessibility_contract(
        _share_trend_chart("Classified mileage share by quarter", buckets)
    )


def test_nondeductible_is_a_third_classified_share_band():
    rows = [
        (2024, 2, "business", 1, 1000.0),
        (2024, 2, "personal", 1, 1000.0),
        (2024, 2, "nondeductible", 1, 2000.0),
    ]
    buckets, _ = _quarterly_buckets(rows, [2024], 12)
    q1 = _bucket(buckets, "Q1 2024")
    chart = _share_trend_chart("Business vs. personal share", buckets)

    assert q1.business_share == pytest.approx(0.25)
    assert q1.nondeductible_m == 2000.0
    assert "var(--overlay0)" in chart
    trend = build_share_trend(rows, [2024], 12)
    assert trend.has_nondeductible is True
    assert "Classified mileage share by quarter" in trend.chart_svg


def test_coverage_note_set_when_quarters_are_clamped():
    clamped_trend = build_share_trend([], [2024, 2025], cutoff_month=5)
    assert clamped_trend.coverage_note == (
        "Quarters that have not started yet are excluded. The current "
        "quarter is still in progress."
    )

    full_trend = build_share_trend([], [2024, 2025], cutoff_month=12)
    assert full_trend.coverage_note is None


@pytest.mark.parametrize(
    "cutoff_month, expected_2025_labels, expect_clamped",
    [
        (3, ["Q1 2025"], True),
        (4, ["Q1 2025", "Q2 2025"], True),
        (9, ["Q1 2025", "Q2 2025", "Q3 2025"], True),
        (10, ["Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"], False),
        (12, ["Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025"], False),
    ],
)
def test_max_year_quarters_at_cutoff_boundaries(
    cutoff_month, expected_2025_labels, expect_clamped
):
    buckets, clamped = _quarterly_buckets([], [2024, 2025], cutoff_month)

    labels_2024 = [b.label for b in buckets if b.label.endswith("2024")]
    assert labels_2024 == ["Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024"]

    labels_2025 = [b.label for b in buckets if b.label.endswith("2025")]
    assert labels_2025 == expected_2025_labels

    assert clamped is expect_clamped


def test_midline_drawn_at_fifty_percent_height():
    buckets = [ShareBucket("Q1 2024", 5.0, 5.0, 0.5)]
    chart = _share_trend_chart("Business vs. personal share", buckets)

    assert 'y1="95.0"' in chart
    assert 'y2="95.0"' in chart


def test_empty_input_is_safe():
    trend = build_share_trend([], [], cutoff_month=8)

    assert "<rect" not in trend.chart_svg
    assert trend.coverage_note is None


def test_plottable_quarters_zero_when_all_rows_unclassified():
    rows = [(2024, 2, "unclassified", 3, 500.0)]
    trend = build_share_trend(rows, [2024], cutoff_month=8)

    assert trend.plottable_quarters == 0


def test_plottable_quarters_one_for_a_single_classified_quarter():
    rows = [
        (2024, 2, "business", 4, 800.0),
        (2024, 2, "personal", 1, 200.0),
    ]
    trend = build_share_trend(rows, [2024], cutoff_month=8)

    assert trend.plottable_quarters == 1


def test_plottable_quarters_counts_multiple_classified_quarters():
    rows = [
        (2024, 2, "business", 4, 800.0),
        (2024, 2, "personal", 1, 200.0),
        (2024, 5, "business", 2, 500.0),
        (2024, 5, "personal", 2, 500.0),
    ]
    trend = build_share_trend(rows, [2024], cutoff_month=8)

    assert trend.plottable_quarters > 1


def test_percentage_gridlines_and_tick_labels_present():
    buckets = [ShareBucket("Q1 2024", 5.0, 5.0, 0.5)]
    chart = _share_trend_chart("Business vs. personal share", buckets)

    assert chart.count('class="stats-gridline"') == 5
    for pct in ("0%", "25%", "50%", "75%", "100%"):
        assert f">{pct}</text>" in chart


def test_rect_titles_include_quarter_category_mileage_and_percentage():
    buckets = [ShareBucket("Q2 2025", 412.3 * 1609.344, 194.1 * 1609.344, 0.68)]
    chart = _share_trend_chart("Business vs. personal share", buckets)

    assert "<title>Q2 2025 business: 412.3 mi (68%)</title>" in chart
    assert "<title>Q2 2025 personal: 194.1 mi (32%)</title>" in chart
    # One chart-level <title> plus one per rect (business, personal); no
    # rect should be left self-closing without its own tooltip.
    assert chart.count("<title>") == 3
    assert "<rect />" not in chart


def test_chart_width_scales_with_bucket_count():
    small_buckets = [ShareBucket(f"Q{i % 4 + 1} 2024", 5.0, 5.0, 0.5) for i in range(4)]
    large_buckets = [ShareBucket(f"Q{i % 4 + 1} 2020", 5.0, 5.0, 0.5) for i in range(20)]

    small_chart = _share_trend_chart("Business vs. personal share", small_buckets)
    large_chart = _share_trend_chart("Business vs. personal share", large_buckets)

    assert _chart_width(small_chart) == 720
    assert _chart_width(large_chart) > _chart_width(small_chart)


def test_step_stays_at_or_above_min_step_for_large_bucket_count():
    buckets = [ShareBucket(f"Q{i % 4 + 1} 2020", 5.0, 5.0, 0.5) for i in range(20)]
    chart = _share_trend_chart("Business vs. personal share", buckets)

    width = _chart_width(chart)
    step = (width - 48 - 24) / 20
    assert step >= MIN_STEP


def test_chart_has_no_inline_style_attribute():
    # A static stylesheet rule now sizes every stats chart uniformly; an
    # inline style here would make this chart the odd one out again.
    small_buckets = [ShareBucket(f"Q{i % 4 + 1} 2024", 5.0, 5.0, 0.5) for i in range(4)]
    large_buckets = [ShareBucket(f"Q{i % 4 + 1} 2020", 5.0, 5.0, 0.5) for i in range(20)]

    small_chart = _share_trend_chart("Business vs. personal share", small_buckets)
    large_chart = _share_trend_chart("Business vs. personal share", large_buckets)

    assert "style=" not in small_chart
    assert "style=" not in large_chart
