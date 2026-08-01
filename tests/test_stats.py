"""Presentation-model tests for the current-year stats dashboard."""
from __future__ import annotations

from datetime import date

import pytest

from app.stats import build_dashboard


def test_dashboard_combines_category_totals_and_builds_current_period_charts():
    dashboard = build_dashboard(
        2026,
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
    assert dashboard.weekly_chart.count("<text ") == 12
    assert dashboard.monthly_chart.count("<text ") == 7
    assert "var(--cat-business)" in dashboard.weekly_chart
    assert "var(--warn)" in dashboard.monthly_chart


def test_dashboard_zero_data_is_safe_and_has_no_business_share():
    dashboard = build_dashboard(2026, date(2026, 1, 2), [], [], [], [], [], 0)

    assert dashboard.trip_count == 0
    assert dashboard.total_m == 0
    assert dashboard.business_share is None
    assert "<rect " not in dashboard.weekly_chart
    assert "aria-label=\"Monthly mileage\"" in dashboard.monthly_chart


def test_dashboard_ignores_out_of_window_and_unknown_bucket_rows():
    dashboard = build_dashboard(
        2026,
        date(2026, 1, 2),
        [("business", 1, 1000)],
        [(date(2025, 3, 3), "business", 1, 500), (date(2026, 1, 5), "unknown", 1, 500)],
        [(date(2025, 12, 1), "business", 1, 500), (date(2026, 1, 1), "unknown", 1, 500)],
        [], [], 0,
    )

    assert dashboard.trip_count == 1
    assert "<rect " not in dashboard.weekly_chart
    assert "<rect " not in dashboard.monthly_chart
