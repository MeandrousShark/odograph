"""Template tests for the quarterly/custom-range report UI:
the Q1-Q4 preset block in `report.html`, and `report_range.html`'s
deliberately narrower scope (no odometer/expense sections).
Same `make_templates`/`.render()` convention as `tests/test_expenses.py`.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates
from app.rates import YearRate
from app.report import AnnualReport, ReportCaveats, build_annual_report, build_range_report

TZ = ZoneInfo("America/Los_Angeles")
MILE = 1609.344
RATES = {2026: YearRate(0.7250)}
USER = {"sub": "test"}


def _trip(month: int, category: str = "business", miles: float = 1) -> dict:
    return {
        "started_at": datetime(2026, month, 15, 12, tzinfo=TZ),
        "display_distance_m": miles * MILE,
        "category": category,
        "purpose": "Client visit",
        "has_gap": False,
        "snap_status": "ok",
        "source": "detected",
        "vehicle_name": "Truck",
    }


def _render(name: str, **context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template(name).render(**context)


def test_report_summary_partial_renders_metrics_rates_and_caveats():
    report = AnnualReport(
        year=2026,
        business_m=2 * MILE,
        personal_m=MILE,
        business_pct=200 / 3,
        total_deduction=1.45,
        rate_periods=[(0.67, 1, 6), (0.70, 7, 12)],
        trip_count=7,
        caveats=ReportCaveats(
            gap_trips=1,
            low_conf_trips=2,
            manual_trips=3,
            unclassified_trips=4,
            business_missing_purpose=5,
            missing_rate=True,
        ),
    )
    body = _render("_report_summary.html", report=report)

    for expected in (
        "Business miles", "2.0 mi", "Personal miles", "1.0 mi",
        "Business share", "66.7%", "Total deduction", "$1.45",
    ):
        assert expected in body
    assert "Rates applied: $0.6700/mi Jan-Jun, $0.7000/mi Jul-Dec" in body
    assert "4 trip(s) still unclassified" in body
    assert "5 business trip(s) have no purpose recorded." in body
    assert "1 trip(s) have a recording gap; distance may under-read." in body
    assert "2 trip(s) have a low-confidence road-snapped distance." in body
    assert "3 trip(s) were entered manually." in body
    assert "No IRS mileage rate on file for 2026; deduction is unavailable." in body
    assert '<a href="/settings">Add one</a>.' in body

    other_caveat_body = _render(
        "_report_summary.html",
        report=AnnualReport(
            year=2026, trip_count=1, caveats=ReportCaveats(gap_trips=1)
        ),
    )
    assert 'href="/settings"' not in other_caveat_body


def test_report_html_renders_shared_summary_for_non_empty_report():
    report = build_annual_report([_trip(1)], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert body.count('<div class="report-summary">') == 1


def test_report_html_has_q1_through_q4_preset_links_for_displayed_year():
    report = build_annual_report([], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert '/report/range?from=2026-01-01&to=2026-03-31' in body
    assert '/report/range?from=2026-04-01&to=2026-06-30' in body
    assert '/report/range?from=2026-07-01&to=2026-09-30' in body
    assert '/report/range?from=2026-10-01&to=2026-12-31' in body


def test_report_html_next_year_link_enabled_for_a_past_year():
    report = build_annual_report([], RATES, TZ, 2020)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert '<a href="/report/2021">2021 →</a>' in body
    assert 'aria-disabled="true"' not in body


def test_report_html_next_year_control_disabled_in_place_for_the_current_year():
    report = build_annual_report([], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=True,
    )
    assert '<span class="report-year-next" aria-disabled="true">2027 →</span>' in body
    assert 'href="/report/2027"' not in body
    # Previous-year link is unguarded and stays a real link either way.
    assert '<a href="/report/2025">← 2025</a>' in body


def test_report_html_next_year_control_disabled_for_a_url_reached_future_year():
    report = build_annual_report([], RATES, TZ, 2030)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=True,
    )
    assert '<span class="report-year-next" aria-disabled="true">2031 →</span>' in body
    assert 'href="/report/2031"' not in body


def test_report_range_html_has_no_odometer_or_expense_sections():
    report = build_range_report(
        [_trip(4), _trip(5)], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30)
    )
    body = _render("report_range.html", report=report, user=USER, csrf="token")
    assert body.count('<div class="report-summary">') == 1
    assert "2026 Q2" in body
    assert "Odometer coverage" not in body
    assert "Odometer reconciliation" not in body
    assert "Standard vs. actual expense estimate" not in body


def test_report_range_html_empty_state_has_no_tables():
    report = build_range_report([], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30))
    body = _render("report_range.html", report=report, user=USER, csrf="token")
    assert "No trips recorded" in body
    assert "report-summary" not in body
    assert "<table>" not in body


def test_report_html_empty_state_has_no_summary_or_tables():
    report = build_annual_report([], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert "No trips recorded in 2026." in body
    assert "report-summary" not in body
    assert "<table>" not in body


def test_report_tables_use_local_horizontal_scroll_wrappers():
    # Both report tables overflowed the page at 320px rather than scrolling
    # inside their own box. The wrapper is the app's existing convention for
    # that, and it does not disturb the first-column padding fix below, which
    # keys off the cells rather than off the table's parent.
    report = build_annual_report([_trip(1), _trip(2)], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert body.count('<div class="table-wrapper">') == 2
    assert "<h2>By vehicle</h2>\n<div class=\"table-wrapper\">" in body
    assert body.count("</table>\n</div>") == 2

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".table-wrapper { overflow-x: auto; }" in css
    assert "th:first-child, td:first-child { padding-left: 0; }" in css
