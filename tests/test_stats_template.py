"""Template test for stats.html's ranking tables and filter bar.

Same make_templates()/.render() convention as tests/test_expenses.py.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates
from app.stats import Dashboard, build_dashboard
from app.stats_multiyear import MultiYearChart
from app.stats_trends import ShareTrend
from app.stats_vehicle import VehicleBreakdown, VehicleStats

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _dashboard(**overrides) -> Dashboard:
    defaults = dict(
        year=2026, trip_count=1, business_m=1.0, personal_m=0.0,
        unclassified_m=0.0, unclassified_trips=0, weekly_chart="", monthly_chart="",
        routes=[{"start_name": "Home", "end_name": "Work", "trip_count": 3, "total_m": 10.0}],
        places=[{"name": "Home", "visit_count": 2}],
        unnamed_trip_count=0,
    )
    defaults.update(overrides)
    return Dashboard(**defaults)


def _render(stats: Dashboard, **context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    ctx = dict(
        stats=stats, user=USER, csrf="token",
        filter_year=stats.year, filter_from="", filter_to="", filter_vehicle="",
        vehicles=[], next_year_disabled=False,
        period_start=date(stats.year, 1, 1), period_end=date(stats.year, 12, 31),
        multiyear=None, trend=None, vehicle_breakdown=None,
    )
    ctx.update(context)
    return templates.env.get_template("stats.html").render(**ctx)


def test_ranking_headings_sit_directly_before_their_tables():
    # "Top saved-place routes" and "Most-used places" are each immediately
    # followed by a bare <table>, which is why the global first-column
    # padding fix has to cover this page too. The report's tables no longer
    # share that shape: they sit inside scroll wrappers, so this page is now
    # the only place the bare heading-then-table adjacency is asserted.
    body = _render(_dashboard())
    assert "<h2>Top saved-place routes</h2>\n    \n    <table>" in body
    assert "<h2>Most-used places</h2>\n    \n    <table>" in body

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert "th:first-child, td:first-child { padding-left: 0; }" in css


def test_year_nav_links_carry_the_vehicle_filter_but_not_dates():
    body = _render(_dashboard(), filter_year=2026, filter_from="2026-06-01", filter_to="2026-08-31", filter_vehicle="3")
    assert '/stats?year=2025&vehicle=3' in body
    assert '/stats?year=2027&vehicle=3' in body
    assert "from=2026-06-01" not in body.split("<details", 1)[0]


def test_next_year_link_is_disabled_when_flagged():
    body = _render(_dashboard(), filter_year=2026, next_year_disabled=True)
    assert 'aria-disabled="true">2027' in body
    assert '/stats?year=2027' not in body


def test_next_year_link_is_present_when_not_disabled():
    body = _render(_dashboard(), filter_year=2025, next_year_disabled=False)
    assert '/stats?year=2026' in body


def test_filter_bar_date_inputs_reflect_current_filter_values():
    body = _render(_dashboard(), filter_from="2026-06-01", filter_to="2026-08-31")
    assert '<input type="date" name="from" value="2026-06-01">' in body
    assert '<input type="date" name="to" value="2026-08-31">' in body


def test_filter_bar_vehicle_select_marks_selected_vehicle():
    vehicles = [{"id": 1, "name": "Truck"}, {"id": 2, "name": "Sedan"}]
    body = _render(_dashboard(), vehicles=vehicles, filter_vehicle="2")
    assert '<option value="2" selected>Sedan</option>' in body
    assert '<option value="1" >Truck</option>' in body


def test_filter_bar_open_when_a_filter_is_active():
    body = _render(_dashboard(), filter_vehicle="2")
    assert '<details class="trip-page-disclosure trip-tools" open>' in body


def test_filter_bar_closed_when_no_filter_is_active():
    body = _render(_dashboard())
    assert '<details class="trip-page-disclosure trip-tools" >' in body


def test_heading_shows_date_range_when_date_filtered():
    body = _render(
        _dashboard(year=2026), filter_from="2026-06-01", filter_to="2026-08-31",
        period_start=date(2026, 6, 1), period_end=date(2026, 8, 31),
    )
    assert "<h2>Jun - Aug 2026 driving overview</h2>" in body


def test_heading_shows_plain_year_when_not_date_filtered():
    body = _render(_dashboard(year=2026))
    assert "<h2>2026 driving overview</h2>" in body


def test_stats_template_exposes_shared_presentation_hooks():
    body = _render(_dashboard())
    assert '<div class="stats-page">' in body
    assert '<div class="stats-page-header page-title">' in body
    assert '<p class="page-title-eyebrow">Stats</p>' in body
    assert '<h2 id="stats-page-title" class="page-title-heading">Driving insights</h2>' in body
    assert '<nav class="stats-year-nav" aria-label="Stats year">' in body
    assert '<div class="stats-summary" role="group" aria-label="Mileage summary">' in body
    assert 'class="stats-section stats-chart-section stats-weekly-section"' in body
    assert 'class="stats-ranking-card"' in body

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".stats-page .stats-summary {" in css
    assert ".stats-page .stats-section {" in css
    stats_mobile = css.index('@media (max-width: 760px) {\n  .stats-page-header')
    review_styles = css.index("/* Review keeps the task surface")
    assert stats_mobile < review_styles


def test_partials_absent_when_their_context_is_not_supplied():
    body = _render(_dashboard())
    assert "Year-over-year monthly miles" not in body
    assert "Business vs. personal share" not in body
    assert "Per-vehicle breakdown" not in body


def test_multiyear_partial_renders_when_present_with_multiple_years():
    multiyear = MultiYearChart(chart_svg="<svg>multiyear</svg>", years=[2025, 2026], coverage_note=None)
    body = _render(_dashboard(), multiyear=multiyear)
    assert "Year-over-year monthly miles" in body
    assert "<svg>multiyear</svg>" in body


def test_multiyear_partial_hidden_with_a_single_year():
    multiyear = MultiYearChart(chart_svg="<svg>multiyear</svg>", years=[2026], coverage_note=None)
    body = _render(_dashboard(), multiyear=multiyear)
    assert "Year-over-year monthly miles" not in body


def test_trend_partial_renders_when_present():
    trend = ShareTrend(chart_svg="<svg>trend</svg>", coverage_note=None, plottable_quarters=2)
    body = _render(_dashboard(), trend=trend)
    assert "Business vs. personal share" in body
    assert "<svg>trend</svg>" in body


def test_trend_heading_names_all_three_bands_when_nondeductible_is_present():
    trend = ShareTrend(
        chart_svg="<svg>trend</svg>", coverage_note=None,
        plottable_quarters=2, has_nondeductible=True,
    )
    body = _render(_dashboard(), trend=trend)
    assert "Classified mileage share" in body
    assert "Business vs. personal share" not in body


def test_trend_partial_hidden_with_fewer_than_two_plottable_quarters():
    trend = ShareTrend(chart_svg="<svg>trend</svg>", coverage_note=None, plottable_quarters=1)
    body = _render(_dashboard(), trend=trend)
    assert "Business vs. personal share" not in body


def test_vehicle_breakdown_partial_renders_when_present():
    vehicle_breakdown = VehicleBreakdown(
        vehicles=[
            VehicleStats(
                vehicle_id=1, vehicle_name="Truck", business_m=1000.0, personal_m=0.0,
                unclassified_m=0.0, total_m=1000.0, expense_total=0.0, deduction=None,
            )
        ],
        unassigned_trip_count=0, coverage_note=None, assigned_vehicle_count=1,
    )
    body = _render(_dashboard(), vehicle_breakdown=vehicle_breakdown)
    assert "Per-vehicle breakdown" in body
    assert "Truck" in body


def test_vehicle_breakdown_partial_hidden_with_only_unassigned_trips():
    vehicle_breakdown = VehicleBreakdown(
        vehicles=[
            VehicleStats(
                vehicle_id=None, vehicle_name="Unassigned", business_m=900.0, personal_m=0.0,
                unclassified_m=0.0, total_m=900.0, expense_total=0.0, deduction=None,
            )
        ],
        unassigned_trip_count=1, coverage_note=None, assigned_vehicle_count=0,
    )
    body = _render(_dashboard(), vehicle_breakdown=vehicle_breakdown)
    assert "Per-vehicle breakdown" not in body


def test_monthly_chart_drill_down_anchor_appears_when_link_fn_supplied():
    # Confirms the raw link_fn-generated <a href> markup survives the
    # template's `| safe` filter unescaped, not just that build_dashboard
    # produces it (already covered in tests/test_stats.py).
    dashboard = build_dashboard(
        2026, date(2026, 1, 1), date(2026, 1, 10),
        [("business", 1, 1609.34)], [],
        [(date(2026, 1, 1), "business", 1, 1609.34)],
        [], [], 0,
        lambda start, end, category: "/trips?category=" + category,
    )
    body = _render(dashboard)
    assert "<a href" in body
