"""Template tests for the quarterly/custom-range report UI:
the Q1-Q4 preset block in `report.html`, and `report_range.html`'s
deliberately narrower scope (no odometer/expense sections).
Same `make_templates`/`.render()` convention as `tests/test_expenses.py`.
"""
from __future__ import annotations

import re
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


def test_report_summary_renders_metrics_and_rates_and_notes_render_caveats():
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
    # Caveats moved out of the summary into the notes section at the end.
    assert "still unclassified" not in body
    body = _render("_report_notes.html", report=report)
    assert "4 trip(s) still unclassified" in body
    assert "5 business trip(s) have no purpose recorded." in body
    assert "1 trip(s) have a recording gap; distance may under-read." in body
    assert "2 trip(s) have a low-confidence road-snapped distance." in body
    assert "3 trip(s) were entered manually." in body
    assert "No IRS mileage rate on file for 2026; deduction is unavailable." in body
    assert '<a href="/settings">Add one</a>.' in body

    other_caveat_body = _render(
        "_report_notes.html",
        report=AnnualReport(
            year=2026, trip_count=1, caveats=ReportCaveats(gap_trips=1)
        ),
    )
    assert "1 trip(s) have a recording gap" in other_caveat_body
    assert 'href="/settings"' not in other_caveat_body


def test_report_pages_use_the_shared_title_header_and_scoped_report_hooks():
    annual = _render(
        "report.html",
        report=build_annual_report([_trip(1)], RATES, TZ, 2026),
        odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=True,
    )
    quarter = _render(
        "report_range.html",
        report=build_range_report(
            [_trip(4)], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30)
        ),
        user=USER, csrf="token",
    )

    for body, title in (
        (annual, "2026 annual mileage report"),
        (quarter, "2026 Q2 mileage report"),
    ):
        assert '<div class="page report-page">' in body
        assert '<div class="page-title page-header">' in body
        assert '<p class="page-title-eyebrow">Report</p>' in body
        assert f'class="page-title-heading">{title}</h2>' in body
    assert 'class="year-nav"' in annual
    assert 'href="/report/2026/export"' in annual
    assert 'href="/expenses?year=2026"' in annual
    assert 'class="filter-bar report-controls"' in annual
    assert '<div class="summary-tile"><span>Business miles</span>' in annual
    assert 'class="summary-tile summary-tile-total"><span>Total deduction' in annual
    assert 'class="report-table report-month-table"' in annual
    assert 'class="report-table report-vehicle-table"' in annual
    assert 'href="/report/2026"' in quarter
    assert 'href="/report/range/export?from=2026-04-01&to=2026-06-30"' in quarter

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".summary-tile-total" in css
    assert ".report-page .table-wrapper" in css


def test_report_html_renders_shared_summary_for_non_empty_report():
    report = build_annual_report([_trip(1)], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert body.count('<div class="summary-tiles">') == 1


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
    assert '<span class="year-nav-next" aria-disabled="true">2027 →</span>' in body
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
    assert '<span class="year-nav-next" aria-disabled="true">2031 →</span>' in body
    assert 'href="/report/2031"' not in body


def test_report_range_html_has_no_odometer_or_expense_sections():
    report = build_range_report(
        [_trip(4), _trip(5)], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30)
    )
    body = _render("report_range.html", report=report, user=USER, csrf="token")
    assert body.count('<div class="summary-tiles">') == 1
    assert "2026 Q2" in body
    assert "Odometer coverage" not in body
    assert "Odometer reconciliation" not in body
    assert "Standard vs. actual expense estimate" not in body


def test_report_range_html_empty_state_has_no_tables():
    report = build_range_report([], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30))
    body = _render("report_range.html", report=report, user=USER, csrf="token")
    assert "No trips recorded" in body
    assert "summary-tile" not in body
    assert "<table>" not in body


def test_report_html_empty_state_has_no_summary_or_tables():
    report = build_annual_report([], RATES, TZ, 2026)
    body = _render(
        "report.html", report=report, odometer_coverage=[], expenses=[],
        expense_report=SimpleNamespace(comparisons=[]), user=USER, csrf="token",
        next_year_disabled=False,
    )
    assert "No trips recorded in 2026." in body
    assert "summary-tile" not in body
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


def test_report_filters_render_inside_the_shared_closed_disclosure():
    body = _render(
        "report.html", report=build_annual_report([_trip(1)], RATES, TZ, 2026),
        odometer_coverage=[], expense_report=SimpleNamespace(comparisons=[]),
        user=USER, csrf="token", next_year_disabled=False,
    )
    start = body.index('<div class="trip-page-disclosures">')
    end = body.index("</details>", start)
    disclosure = body[start:end]
    assert '<details class="trip-page-disclosure trip-tools">' in disclosure
    assert "<summary>Filters: full year</summary>" in disclosure
    assert 'class="filter-bar report-controls"' in disclosure
    assert '/report/range?from=2026-04-01&to=2026-06-30' in disclosure
    assert '<form class="date-range report-date-range" method="get" action="/report/range">' in disclosure
    assert body.count("trip-page-disclosure trip-tools") == 1
    assert "<details class=\"trip-page-disclosure trip-tools\" open" not in body

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".report-page .trip-page-disclosure > summary {" in css


def _comparison(**overrides):
    from decimal import Decimal

    from app.expenses import ExpenseComparison

    values = dict(
        vehicle_id=1, vehicle_name="Truck", business_m=MILE, denominator_m=2 * MILE,
        denominator_source="gps", provisional=True, invalid_odometer=False,
        business_pct=Decimal("0.5"), allocated_expenses=Decimal("10.00"),
        fully_business_expenses=Decimal("2.00"), standard_total=Decimal("0.73"),
        actual_total=Decimal("7.00"), difference=Decimal("6.27"), larger_estimate="actual",
    )
    values.update(overrides)
    return ExpenseComparison(**values)


def _coverage():
    from app.odometer import VehicleCoverage

    return VehicleCoverage(
        vehicle_name="Truck", span_start=datetime(2026, 1, 1, tzinfo=TZ),
        span_end=datetime(2026, 12, 31, tzinfo=TZ), coverage=0.9, gap_m=MILE,
        fully_bracketed=True,
    )


def _full_annual(**overrides):
    context = dict(
        report=build_annual_report([_trip(1), _trip(2, "personal")], RATES, TZ, 2026),
        odometer_coverage=[_coverage()],
        expense_report=SimpleNamespace(comparisons=[_comparison()]),
        user=USER, csrf="token", next_year_disabled=False,
    )
    context.update(overrides)
    return _render("report.html", **context)


def test_report_sections_are_single_cards_with_consistent_headings():
    body = _full_annual()
    sections = re.findall(r'<section class="report-section ([a-z-]+)"[^>]*>\s*<h2[^>]*>([^<]+)</h2>', body)
    assert sections == [
        ("report-month-section", "By month"),
        ("report-vehicle-section", "By vehicle"),
        ("report-coverage", "Odometer coverage"),
        ("report-expense-comparison", "Standard vs. actual expense estimate"),
    ]
    assert body.count('<section class="report-section') == len(sections)
    # Summary first, then the tables.
    assert body.index('<div class="summary-tiles">') < body.index('<section class="report-section')
    # Every table sits directly in a section card's scroll wrapper.
    assert body.count("<table") == body.count('<div class="table-wrapper">') == 3

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    wrapper = css[css.index(".report-page .table-wrapper {"):]
    wrapper = wrapper[:wrapper.index("}")]
    assert "overscroll-behavior-inline" in wrapper
    for surface in ("border", "box-shadow", "background"):
        assert surface not in wrapper
    assert ".report-page .report-section, .stats-page .stats-section {" in css
    for removed in (".report-coverage {", ".report-expense-comparison {"):
        assert removed not in css


def test_report_caveats_merge_into_one_notes_section_at_the_end():
    report = build_annual_report([{**_trip(1), "source": "manual"}], RATES, TZ, 2026)
    body = _full_annual(report=report)
    report_line = "1 trip(s) were entered manually."
    comparison_line = "The larger estimate is informational, not a filing recommendation"
    provisional_line = "Provisional comparisons use GPS-detected total miles"

    assert body.count('<section class="report-notes"') == 1
    assert "report-caveats" not in body
    notes = body[body.index('<section class="report-notes"'):]
    notes = notes[:notes.index("</section>")]
    assert '<h2 id="report-notes-heading">Review before filing</h2>' in notes
    for expected in (
        report_line, comparison_line, provisional_line,
        'href="https://www.irs.gov/publications/p463"',
        'href="https://www.irs.gov/publications/p946"',
    ):
        assert expected in notes
        assert body.count(expected) == 1
    # Last thing on the page, after the comparison table.
    assert body.index("report-expense-comparison") < body.index('<section class="report-notes"')
    assert body.index("</section>", body.index('<section class="report-notes"')) > body.rindex("</table>")

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".report-page .report-notes {" in css
    assert ".report-caveats" not in css


def test_report_notes_show_comparison_caveats_even_without_trips():
    body = _full_annual(report=build_annual_report([], RATES, TZ, 2026))
    assert "No trips recorded in 2026." in body
    assert body.count('<section class="report-notes"') == 1
    assert "The larger estimate is informational" in body


def test_report_notes_absent_without_caveats():
    body = _full_annual(expense_report=SimpleNamespace(comparisons=[]))
    assert "By month" in body
    assert "report-notes" not in body
    assert "Review before filing" not in body


def test_range_report_notes_hold_report_caveats_only():
    report = build_range_report(
        [{**_trip(4), "source": "manual"}], RATES, TZ, date(2026, 4, 1), date(2026, 6, 30)
    )
    body = _render(
        "report_range.html", report=report, user=USER, csrf="token",
        expense_report=SimpleNamespace(comparisons=[_comparison()]),
    )
    assert body.count('<section class="report-notes"') == 1
    assert "1 trip(s) were entered manually." in body
    assert "The larger estimate is informational" not in body
    assert "irs.gov" not in body
    assert body.index('<section class="report-section') < body.index('<section class="report-notes"')
