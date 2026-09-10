"""Presentation-model tests for the stats page's per-vehicle breakdown."""
from __future__ import annotations

from app.rates import YearRate
from app.report import sum_month_deductions
from app.stats_vehicle import build_vehicle_breakdown


def test_mixed_categories_produce_correct_per_vehicle_totals():
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1000.0),
        (1, "Truck", 2, "personal", 1, 500.0),
        (2, "Sedan", 1, "personal", 1, 2000.0),
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, {})

    by_id = {v.vehicle_id: v for v in breakdown.vehicles}
    assert by_id[1].business_m == 1000.0
    assert by_id[1].personal_m == 500.0
    assert by_id[1].unclassified_m == 0.0
    assert by_id[1].total_m == 1500.0
    assert by_id[2].business_m == 0.0
    assert by_id[2].personal_m == 2000.0
    assert by_id[2].total_m == 2000.0


def test_nondeductible_miles_remain_in_vehicle_total_without_deduction():
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1000.0),
        (1, "Truck", 1, "nondeductible", 1, 3000.0),
    ]
    breakdown = build_vehicle_breakdown(
        mileage_rows, [], 2026, {2026: YearRate(rate_per_mi=0.70)}
    )
    truck = breakdown.vehicles[0]

    assert truck.business_m == 1000.0
    assert truck.nondeductible_m == 3000.0
    assert truck.total_m == 4000.0
    assert truck.deduction == sum_month_deductions([(1, 1000.0)], 2026, {
        2026: YearRate(rate_per_mi=0.70)
    })


def test_deduction_matches_sum_month_deductions_directly():
    rates = {2026: YearRate(rate_per_mi=0.70)}
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1609.344),
        (1, "Truck", 3, "business", 1, 3218.688),
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, rates)

    expected = sum_month_deductions([(1, 1609.344), (3, 3218.688)], 2026, rates)
    truck = next(v for v in breakdown.vehicles if v.vehicle_id == 1)
    assert truck.deduction == expected
    assert truck.deduction is not None


def test_no_rate_on_file_yields_none_deduction_for_all_vehicles():
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1000.0),
        (2, "Sedan", 1, "business", 1, 500.0),
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, {})

    assert all(v.deduction is None for v in breakdown.vehicles)


def test_unassigned_trips_grouped_and_counted():
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1000.0),
        (None, "", 1, "personal", 50, 300.0),
        (None, "", 2, "business", 1, 400.0),
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, {})

    assert breakdown.unassigned_trip_count == 51
    assert breakdown.vehicles[-1].vehicle_id is None
    assert breakdown.vehicles[-1].vehicle_name == "Unassigned"
    assert breakdown.vehicles[-1].personal_m == 300.0
    assert breakdown.vehicles[-1].business_m == 400.0
    assert breakdown.coverage_note == (
        "51 trip(s) without a vehicle assignment are grouped under Unassigned."
    )


def test_no_unassigned_trips_has_no_coverage_note():
    mileage_rows = [(1, "Truck", 1, "business", 1, 1000.0)]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, {})

    assert breakdown.unassigned_trip_count == 0
    assert breakdown.coverage_note is None


def test_expense_totals_matched_to_vehicles_by_id():
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1000.0),
        (2, "Sedan", 1, "personal", 1, 500.0),
    ]
    expense_rows = [
        (1, "Truck", 250.0),
        (1, "Truck", 75.0),
        (2, "Sedan", 40.0),
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, expense_rows, 2026, {})

    by_id = {v.vehicle_id: v for v in breakdown.vehicles}
    assert by_id[1].expense_total == 325.0
    assert by_id[2].expense_total == 40.0


def test_vehicle_with_expenses_but_no_mileage_rows_still_appears():
    mileage_rows = [(1, "Truck", 1, "business", 1, 1000.0)]
    expense_rows = [
        (1, "Truck", 250.0),
        (2, "Sedan", 60.0),  # no mileage rows for vehicle 2 this period
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, expense_rows, 2026, {})

    by_id = {v.vehicle_id: v for v in breakdown.vehicles}
    assert 2 in by_id
    sedan = by_id[2]
    assert sedan.vehicle_name == "Sedan"
    assert sedan.business_m == 0.0
    assert sedan.personal_m == 0.0
    assert sedan.unclassified_m == 0.0
    assert sedan.total_m == 0.0
    assert sedan.expense_total == 60.0
    assert sedan.deduction is None


def test_assigned_vehicle_count_zero_when_only_unassigned_trips():
    mileage_rows = [(None, "", 1, "business", 1, 900.0)]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, {})

    assert breakdown.assigned_vehicle_count == 0


def test_assigned_vehicle_count_counts_real_vehicles_including_expense_only():
    mileage_rows = [
        (1, "Truck", 1, "business", 1, 1000.0),
        (None, "", 1, "personal", 1, 300.0),
    ]
    expense_rows = [(2, "Sedan", 60.0)]  # no mileage rows for vehicle 2
    breakdown = build_vehicle_breakdown(mileage_rows, expense_rows, 2026, {})

    assert breakdown.assigned_vehicle_count == 2


def test_sorting_assigned_by_total_descending_unassigned_last():
    mileage_rows = [
        (1, "Small", 1, "personal", 1, 100.0),
        (2, "Big", 1, "personal", 1, 5000.0),
        (3, "Medium", 1, "personal", 1, 1000.0),
        (None, "", 1, "personal", 1, 999999.0),
    ]
    breakdown = build_vehicle_breakdown(mileage_rows, [], 2026, {})

    ids = [v.vehicle_id for v in breakdown.vehicles]
    assert ids == [2, 3, 1, None]
