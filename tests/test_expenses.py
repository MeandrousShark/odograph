from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from fastapi import HTTPException

from app.expenses import (
    CATEGORY_LABELS,
    EXPENSE_CONFLICT_LABELS,
    EXPENSE_CATEGORIES,
    EXPENSE_TREATMENTS,
    TREATMENT_LABELS,
    build_expense_report,
    comparison_caveat_lines,
    comparison_status,
    expense_conflicts,
)
from app.main import make_templates
from app.rates import YearRate
from app.ui import _parse_expense_input

TZ = ZoneInfo("America/Los_Angeles")
MILE = 1609.344
RATES = {2026: YearRate(0.70)}


def _trip(vehicle_id=1, name="Truck", category="business", miles=1, month=6, exclusion=None):
    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": name,
        "started_at": datetime(2026, month, 15, 12, tzinfo=TZ),
        "display_distance_m": miles * MILE,
        "category": category,
        "exclusion": exclusion,
    }


def _expense(
    vehicle_id=1, name="Truck", category="fuel", amount="100.00",
    treatment="business_use_allocated", trip_id=None, **extra,
):
    row = {
        "vehicle_id": vehicle_id,
        "vehicle_name": name,
        "incurred_on": date(2026, 6, 1),
        "category": category,
        "amount": Decimal(amount),
        "treatment": treatment,
    }
    if trip_id is not None:
        row["trip_id"] = trip_id
    row.update(extra)
    return row


def _reading(when, miles, vehicle_id=1, name="Truck"):
    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": name,
        "recorded_at": when,
        "odometer_m": miles * MILE,
    }


def test_gps_fallback_counts_every_category_and_marks_comparison_provisional():
    trips = [
        _trip(category="business", miles=20),
        _trip(category="personal", miles=30),
        _trip(category="unclassified", miles=50),
    ]
    report = build_expense_report(2026, trips, [_expense(amount="1000")], [], RATES, TZ)
    line = report.comparisons[0]
    assert line.denominator_m == pytest.approx(100 * MILE)
    assert line.business_pct == Decimal("0.2")
    assert line.actual_total == Decimal("200.00")
    assert line.standard_total == Decimal("14.00")
    assert line.larger_estimate == "actual"
    assert line.provisional is True


def test_nearest_full_year_odometer_bracket_is_preferred():
    readings = [
        _reading(datetime(2025, 1, 1, tzinfo=TZ), 100),
        _reading(datetime(2025, 12, 31, tzinfo=TZ), 1000),
        _reading(datetime(2027, 1, 1, tzinfo=TZ), 1200),
        _reading(datetime(2027, 6, 1, tzinfo=TZ), 1300),
    ]
    report = build_expense_report(
        2026, [_trip(miles=50)], [_expense(amount="400")], readings, RATES, TZ
    )
    line = report.comparisons[0]
    assert line.denominator_source == "odometer"
    assert line.denominator_m == pytest.approx(200 * MILE)
    assert line.business_pct == Decimal("0.25")
    assert line.actual_total == Decimal("100.00")
    assert line.provisional is False


def test_invalid_full_year_odometer_span_falls_back_to_gps():
    readings = [
        _reading(datetime(2026, 1, 1, tzinfo=TZ), 1200),
        _reading(datetime(2027, 1, 1, tzinfo=TZ), 1100),
    ]
    line = build_expense_report(
        2026, [_trip(miles=10)], [_expense()], readings, RATES, TZ
    ).comparisons[0]
    assert line.denominator_source == "gps"
    assert line.invalid_odometer is True
    assert line.provisional is True


def test_positive_odometer_span_smaller_than_business_miles_falls_back_without_overallocation():
    trips = [
        _trip(category="business", miles=80),
        _trip(category="personal", miles=20),
    ]
    readings = [
        _reading(datetime(2026, 1, 1, tzinfo=TZ), 1000),
        _reading(datetime(2027, 1, 1, tzinfo=TZ), 1050),
    ]
    line = build_expense_report(
        2026, trips, [_expense(amount="1000")], readings, RATES, TZ
    ).comparisons[0]
    assert line.denominator_source == "gps"
    assert line.denominator_m == pytest.approx(100 * MILE)
    assert line.business_pct == Decimal("0.8")
    assert line.business_pct <= Decimal("1")
    assert line.actual_total == Decimal("800.00")
    assert line.actual_total <= line.allocated_expenses + line.fully_business_expenses
    assert line.invalid_odometer is True
    assert comparison_status(line) == "Provisional (odometer ignored)"
    assert any("smaller than recorded business miles" in caveat for caveat in comparison_caveat_lines([line]))


def test_internal_odometer_rollback_invalidates_positive_endpoint_delta():
    trips = [
        _trip(category="business", miles=40),
        _trip(category="personal", miles=60),
    ]
    readings = [
        _reading(datetime(2026, 1, 1, tzinfo=TZ), 1000),
        _reading(datetime(2026, 6, 1, tzinfo=TZ), 900),
        _reading(datetime(2027, 1, 1, tzinfo=TZ), 1100),
    ]
    line = build_expense_report(
        2026, trips, [_expense(amount="1000")], readings, RATES, TZ
    ).comparisons[0]
    assert line.denominator_source == "gps"
    assert line.denominator_m == pytest.approx(100 * MILE)
    assert line.business_pct == Decimal("0.4")
    assert line.actual_total == Decimal("400.00")
    assert line.invalid_odometer is True
    assert comparison_status(line) == "Provisional (odometer ignored)"
    assert any("readings did not increase" in caveat for caveat in comparison_caveat_lines([line]))


def test_rollbacks_outside_selected_closest_anchors_do_not_invalidate_bracket():
    trips = [_trip(category="business", miles=50)]
    readings = [
        _reading(datetime(2025, 1, 1, tzinfo=TZ), 2000),
        _reading(datetime(2025, 12, 31, tzinfo=TZ), 1000),
        _reading(datetime(2026, 6, 1, tzinfo=TZ), 1100),
        _reading(datetime(2027, 1, 1, tzinfo=TZ), 1200),
        _reading(datetime(2027, 6, 1, tzinfo=TZ), 1100),
    ]
    line = build_expense_report(
        2026, trips, [_expense(amount="400")], readings, RATES, TZ
    ).comparisons[0]
    assert line.denominator_source == "odometer"
    assert line.denominator_m == pytest.approx(200 * MILE)
    assert line.business_pct == Decimal("0.25")
    assert line.invalid_odometer is False


def test_fully_business_parking_is_added_equally_outside_both_methods():
    trips = [_trip(category="business", miles=10), _trip(category="personal", miles=10)]
    expenses = [
        _expense(amount="100", category="fuel"),
        _expense(amount="25", category="parking", treatment="fully_business"),
    ]
    line = build_expense_report(2026, trips, expenses, [], RATES, TZ).comparisons[0]
    assert line.standard_total == Decimal("32.00")
    assert line.actual_total == Decimal("75.00")
    assert line.fully_business_expenses == Decimal("25.00")


def test_entered_depreciation_is_an_allocated_expense_only():
    trips = [_trip(category="business", miles=1), _trip(category="personal", miles=3)]
    line = build_expense_report(
        2026, trips, [_expense(category="depreciation", amount="1000")], [], RATES, TZ
    ).comparisons[0]
    assert line.allocated_expenses == Decimal("1000.00")
    assert line.actual_total == Decimal("250.00")
    assert line.standard_total == Decimal("0.70")


def test_duplicate_vehicle_names_stay_isolated_by_id():
    trips = [_trip(vehicle_id=1, name="Car", miles=10), _trip(vehicle_id=2, name="Car", miles=2)]
    expenses = [_expense(vehicle_id=1, name="Car", amount="100")]
    lines = build_expense_report(2026, trips, expenses, [], RATES, TZ).comparisons
    assert [line.vehicle_id for line in lines] == [1, 2]
    assert lines[0].allocated_expenses == Decimal("100.00")
    assert lines[1].allocated_expenses == Decimal("0.00")


def test_zero_mile_expense_only_vehicle_keeps_ledger_but_comparison_unavailable():
    line = build_expense_report(
        2026, [], [_expense(category="tolls", amount="15", treatment="fully_business")],
        [], RATES, TZ,
    ).comparisons[0]
    assert line.business_pct is None
    assert line.actual_total is None
    assert line.fully_business_expenses == Decimal("15.00")
    assert line.standard_total == Decimal("15.00")
    assert line.larger_estimate is None


def test_split_year_standard_estimate_prices_each_month():
    rates = {2026: YearRate(0.50, rate_h2_per_mi=0.75, h2_start_month=7)}
    trips = [_trip(miles=10, month=3), _trip(miles=10, month=9)]
    line = build_expense_report(2026, trips, [], [], rates, TZ).comparisons[0]
    assert line.standard_total == Decimal("12.50")


def test_standard_method_can_be_the_larger_estimate():
    trips = [_trip(category="business", miles=10), _trip(category="personal", miles=90)]
    line = build_expense_report(
        2026, trips, [_expense(amount="1.00")], [], RATES, TZ
    ).comparisons[0]
    assert line.standard_total == Decimal("7.00")
    assert line.actual_total == Decimal("0.10")
    assert line.larger_estimate == "standard"


def test_unassigned_trip_does_not_contribute_to_vehicle_comparison():
    trips = [_trip(vehicle_id=None, name=None, category="business", miles=100)]
    report = build_expense_report(2026, trips, [_expense(amount="10")], [], RATES, TZ)
    line = report.comparisons[0]
    assert line.vehicle_id == 1
    assert line.business_m == 0
    assert line.denominator_m == 0


def test_not_my_vehicle_trip_leaves_the_gps_total_denominator_entirely():
    trips = [
        _trip(category="business", miles=20),
        _trip(category="personal", miles=30, exclusion="not_my_vehicle"),
    ]
    report = build_expense_report(2026, trips, [_expense(amount="100")], [], RATES, TZ)
    line = report.comparisons[0]
    assert line.denominator_m == pytest.approx(20 * MILE)
    assert line.business_m == pytest.approx(20 * MILE)


def test_not_deductible_business_trip_stays_in_denominator_but_leaves_business_m():
    trips = [
        # Category "business" is deliberate: not_deductible must win even
        # when the trip's own category would otherwise count it.
        _trip(category="business", miles=20, exclusion="not_deductible"),
        _trip(category="personal", miles=10),
    ]
    report = build_expense_report(2026, trips, [_expense(amount="100")], [], RATES, TZ)
    line = report.comparisons[0]
    assert line.denominator_m == pytest.approx(30 * MILE)
    assert line.business_m == 0
    assert line.standard_total == Decimal("0.00")


def test_linked_expense_adds_trip_attribution_without_changing_method_arithmetic():
    trip = _trip(miles=20)
    expense = _expense(amount="100", trip_id=42, trip_vehicle_id=1,
                       trip_started_at=trip["started_at"], trip_exclusion=None)
    expense["incurred_on"] = trip["started_at"].date()
    report = build_expense_report(2026, [trip], [expense], [], RATES, TZ)
    line = report.comparisons[0]
    assert report.trip_expenses == {42: Decimal("100.00")}
    assert report.attributions[0].trip_id == 42
    assert report.attributions[0].conflicts == ()
    assert line.allocated_expenses == Decimal("100.00")
    assert line.actual_total == Decimal("100.00")


def test_linked_expense_reports_each_conflict_without_mutating_records():
    started = datetime(2026, 6, 15, 12, tzinfo=TZ)
    expense = _expense(
        vehicle_id=1, trip_id=42, trip_vehicle_id=None,
        trip_started_at=started, trip_exclusion="not_my_vehicle",
    )
    expense["incurred_on"] = date(2026, 6, 16)
    before = dict(expense)
    assert expense_conflicts(expense, TZ) == ("vehicle", "date", "not_my_vehicle")
    assert expense == before


def test_expense_report_derives_link_conflicts_from_trip_rows():
    trip = _trip(vehicle_id=None, exclusion="not_my_vehicle")
    trip["id"] = 42
    expense = _expense(trip_id=42)
    expense["incurred_on"] = date(2026, 6, 16)
    report = build_expense_report(2026, [trip], [expense], [], RATES, TZ)
    assert report.attributions[0].conflicts == ("vehicle", "date", "not_my_vehicle")


def test_other_expense_requires_explicit_treatment_at_server_boundary():
    with pytest.raises(HTTPException) as exc:
        _parse_expense_input("2026-01-01", "other", "1.00", "")
    assert exc.value.status_code == 400
    assert "require a tax treatment" in exc.value.detail

    parsed = _parse_expense_input(
        "2026-01-01", "other", "1.00", "business_use_allocated"
    )
    assert parsed[3] == "business_use_allocated"


def test_expense_form_clears_required_treatment_when_other_is_selected():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("expenses.html").render(
        expenses=[],
        vehicles=[{
            "id": 1, "name": "Truck", "active": True, "is_default": True,
        }],
        selected_year=2026,
        selected_vehicle="",
        category_totals={},
        ledger_total=Decimal("0"),
        category_labels=CATEGORY_LABELS,
        treatment_labels=TREATMENT_LABELS,
        expense_categories=EXPENSE_CATEGORIES,
        expense_treatments=EXPENSE_TREATMENTS,
        user={"sub": "test"},
        csrf="token",
    )
    assert '<option value="" disabled>Choose treatment…</option>' in body
    assert '<option value="business_use_allocated" selected>Business-use allocated</option>' in body
    assert "categorySelect.value === 'other'" in body
    assert "treatment.value = '';" in body


def test_expenses_page_renders_trip_link_and_conflict_warning():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("expenses.html").render(
        expenses=[{
            "id": 4, "vehicle_id": 1, "vehicle_name": "Truck",
            "incurred_on": date(2026, 6, 1), "category": "fuel",
            "amount": Decimal("12.00"), "treatment": "business_use_allocated",
            "notes": None, "trip_id": 42,
            "conflicts": ["The expense date does not match the linked trip's local date."],
        }],
        trips=[],
        vehicles=[{"id": 1, "name": "Truck", "active": True, "is_default": True}],
        selected_year=2026, selected_vehicle="", category_totals={"fuel": Decimal("12")},
        ledger_total=Decimal("12"), category_labels=CATEGORY_LABELS,
        treatment_labels=TREATMENT_LABELS, expense_categories=EXPENSE_CATEGORIES,
        expense_treatments=EXPENSE_TREATMENTS, user={"sub": "test"}, csrf="token",
    )
    assert 'href="/trips/42">Trip 42</a>' in body
    assert "The expense date does not match the linked trip&#39;s local date." in body


def test_expense_template_collapses_and_pluralizes_persisted_warnings():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    base = {
        "id": 4, "vehicle_id": 1, "vehicle_name": "Truck",
        "incurred_on": date(2026, 6, 1), "category": "fuel",
        "amount": Decimal("12.00"), "treatment": "business_use_allocated",
        "notes": None, "trip_id": 42,
    }
    context = {
        "vehicles": [{"id": 1, "name": "Truck", "active": True, "is_default": True}],
        "trips": [], "selected_year": 2026, "selected_vehicle": "",
        "category_totals": {"fuel": Decimal("12")}, "ledger_total": Decimal("12"),
        "category_labels": CATEGORY_LABELS, "treatment_labels": TREATMENT_LABELS,
        "expense_categories": EXPENSE_CATEGORIES, "expense_treatments": EXPENSE_TREATMENTS,
        "expense_conflict_labels": EXPENSE_CONFLICT_LABELS,
        "user": {"sub": "test"}, "csrf": "token",
    }
    singular = templates.env.get_template("expenses.html").render(
        expenses=[{**base, "conflicts": [EXPENSE_CONFLICT_LABELS["vehicle"]]}], **context
    )
    plural = templates.env.get_template("expenses.html").render(
        expenses=[{**base, "conflicts": list(EXPENSE_CONFLICT_LABELS.values())}], **context
    )
    clear = templates.env.get_template("expenses.html").render(
        expenses=[{**base, "conflicts": []}], **context
    )

    assert "<summary>1 warning</summary>" in singular
    assert "<summary>3 warnings</summary>" in plural
    assert "<details class=\"expense-warning-details\">" not in clear
    assert "The expense vehicle does not match the linked trip vehicle." in singular


def test_expenses_template_exposes_shared_presentation_surfaces():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("expenses.html").render(
        expenses=[{
            "id": 4, "vehicle_id": 1, "vehicle_name": "Truck",
            "incurred_on": date(2026, 6, 1), "category": "fuel",
            "amount": Decimal("12.00"), "treatment": "business_use_allocated",
            "notes": "receipt", "trip_id": None, "conflicts": [],
        }],
        trips=[],
        vehicles=[{"id": 1, "name": "Truck", "active": True, "is_default": True}],
        selected_year=2026, selected_vehicle="", category_totals={"fuel": Decimal("12")},
        ledger_total=Decimal("12"), category_labels=CATEGORY_LABELS,
        treatment_labels=TREATMENT_LABELS, expense_categories=EXPENSE_CATEGORIES,
        expense_treatments=EXPENSE_TREATMENTS,
        expense_conflict_labels=EXPENSE_CONFLICT_LABELS,
        user={"sub": "test"}, csrf="token",
    )
    assert '<div class="expenses-page">' in body
    assert '<div class="expenses-page-header page-title">' in body
    assert '<p class="page-title-eyebrow">Expenses</p>' in body
    assert '<h2 id="expenses-page-title" class="page-title-heading">2026 vehicle expense ledger</h2>' in body
    assert '<nav class="expenses-year-nav" aria-label="Expenses year">' in body
    assert '<form class="filter-bar expenses-controls" method="get">' in body
    assert '<details class="add-manual" open>' in body
    assert '<div class="report-summary expenses-summary">' in body
    assert '<div class="table-wrapper expenses-table-wrapper">' in body

    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".expenses-page .expenses-summary > div" in stylesheet
    assert ".expenses-page .expense-editor-panel" in stylesheet
    assert ".expenses-page .expense-warning-details > summary" in stylesheet
    expenses_mobile = stylesheet.index('@media (max-width: 760px) {\n  .expenses-page-header')
    review_styles = stylesheet.index("/* Review keeps the task surface")
    assert expenses_mobile < review_styles


def test_expense_editor_layout_and_live_warning_metadata_are_rendered():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("expenses.html").render(
        expenses=[{
            "id": 4, "vehicle_id": 1, "vehicle_name": "Truck",
            "incurred_on": date(2026, 6, 15), "category": "fuel",
            "amount": Decimal("12.00"), "treatment": "business_use_allocated",
            "notes": "receipt", "trip_id": 42, "conflicts": [],
        }],
        trips=[{
            "id": 42, "started_at": datetime(2026, 6, 15, 12, tzinfo=TZ),
            "vehicle_id": 1, "vehicle_name": "Truck", "exclusion": "not_my_vehicle",
        }],
        vehicles=[{"id": 1, "name": "Truck", "active": True, "is_default": True}],
        selected_year=2026, selected_vehicle="", category_totals={"fuel": Decimal("12")},
        ledger_total=Decimal("12"), category_labels=CATEGORY_LABELS,
        treatment_labels=TREATMENT_LABELS, expense_categories=EXPENSE_CATEGORIES,
        expense_treatments=EXPENSE_TREATMENTS,
        expense_conflict_labels=EXPENSE_CONFLICT_LABELS,
        user={"sub": "test"}, csrf="token",
    )
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert 'class="expense-editor-row"' in body
    assert '<table class="expense-ledger">' in body
    assert '<col class="expense-ledger-warnings">' in body
    assert '<col class="expense-ledger-actions">' in body
    assert '<td colspan="9">' in body
    assert 'data-expense-editor-toggle="expense-editor-4"' in body
    assert '<tr id="expense-editor-4" class="expense-editor-row" hidden>' in body
    assert body.index('data-expense-editor-toggle="expense-editor-4"') < body.index('id="expense-editor-4"')
    assert 'class="expense-editor-panel"' in body
    assert 'class="expense-editor-grid"' in body
    assert "expense-editor-notes" in body
    assert 'class="expense-editor-actions"' in body
    assert 'data-trip-vehicle-id="1"' in body
    assert 'data-trip-date="2026-06-15"' in body
    assert 'data-trip-exclusion="not_my_vehicle"' in body
    assert 'data-expense-warning="vehicle"' in body
    assert 'data-expense-warning="date"' in body
    assert 'data-expense-warning="not-my-vehicle"' in body
    assert "updateExpenseWarnings(form)" in body
    assert "option.dataset.tripVehicleId !== vehicleSelect.value" in body
    assert "option.dataset.tripDate !== dateInput.value" in body
    assert "option.dataset.tripExclusion === 'not_my_vehicle'" in body
    assert "panel.hidden = !disclosure.open" in body
    assert ".expense-editor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in stylesheet
    assert ".expense-editor-notes { grid-column: 1 / -1; }" in stylesheet
    assert ".expense-editor-actions { display: flex; gap: var(--space-2); }" in stylesheet
    assert ".expense-editor-notes { grid-column: auto; }" in stylesheet
    assert ".expense-ledger { table-layout: fixed; min-width: 56rem; }" in stylesheet
    assert ".expense-ledger-warnings { width: 15%; }" in stylesheet
    assert ".expense-ledger-actions { width: 11%; }" in stylesheet
