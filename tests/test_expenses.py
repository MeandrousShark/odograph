from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from fastapi import HTTPException

from app.expenses import (
    CATEGORY_LABELS,
    EXPENSE_CATEGORIES,
    EXPENSE_TREATMENTS,
    TREATMENT_LABELS,
    build_expense_report,
    comparison_caveat_lines,
    comparison_status,
)
from app.main import make_templates
from app.rates import YearRate
from app.ui import _parse_expense_input

TZ = ZoneInfo("America/Los_Angeles")
MILE = 1609.344
RATES = {2026: YearRate(0.70)}


def _trip(vehicle_id=1, name="Truck", category="business", miles=1, month=6):
    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": name,
        "started_at": datetime(2026, month, 15, 12, tzinfo=TZ),
        "display_distance_m": miles * MILE,
        "category": category,
    }


def _expense(
    vehicle_id=1, name="Truck", category="fuel", amount="100.00",
    treatment="business_use_allocated",
):
    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": name,
        "incurred_on": date(2026, 6, 1),
        "category": category,
        "amount": Decimal(amount),
        "treatment": treatment,
    }


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
