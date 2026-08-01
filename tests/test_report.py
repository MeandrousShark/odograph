"""Tests for annual tax report aggregation and its generalization to
arbitrary single-year date ranges.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.rates import YearRate
from app.report import (
    NO_VEHICLE_LABEL,
    build_annual_report,
    build_range_report,
    default_report_year,
    quarter_bounds,
    range_label,
)

TZ = ZoneInfo("America/Los_Angeles")


def _trip(**overrides) -> dict:
    base = dict(
        started_at=datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc),  # local month = 6
        display_distance_m=1609.344,  # 1 mile
        category="business",
        purpose="Client visit",
        has_gap=False,
        snap_status="ok",
        source="detected",
    )
    base.update(overrides)
    return base


def _at(month: int, **overrides) -> dict:
    return _trip(started_at=datetime(2026, month, 15, 12, 0, tzinfo=timezone.utc), **overrides)


def test_single_rate_year_totals():
    rates = {2026: YearRate(0.700)}
    report = build_annual_report([_at(6, display_distance_m=1609.344 * 10)], rates, TZ, 2026)
    assert report.business_m == pytest.approx(1609.344 * 10)
    assert report.total_deduction == pytest.approx(7.0)
    assert report.business_pct == pytest.approx(100.0)
    assert len(report.months) == 1
    assert report.months[0].rate_per_mi == pytest.approx(0.700)


def test_midyear_split_prices_each_month_at_its_own_rate():
    # 2022-style: 58.5c/mi Jan-Jun, 62.5c/mi from Jul 1 — the load-bearing case.
    rates = {2026: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    trips = [
        _at(6, display_distance_m=1609.344),  # June: first-half rate
        _at(7, display_distance_m=1609.344),  # July: second-half rate
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    months_by_num = {m.month: m for m in report.months}
    assert months_by_num[6].rate_per_mi == pytest.approx(0.585)
    assert months_by_num[7].rate_per_mi == pytest.approx(0.625)
    assert report.total_deduction == pytest.approx(0.585 + 0.625)
    # Two distinct rate spans, not one flat rate for the year.
    assert report.rate_periods == [(0.585, 1, 6), (0.625, 7, 12)]


def test_personal_and_unclassified_excluded_from_business_split():
    rates = {2026: YearRate(0.700)}
    trips = [
        _at(6, category="business", display_distance_m=1609.344),
        _at(6, category="personal", display_distance_m=1609.344 * 2),
        _at(6, category="unclassified", display_distance_m=1609.344 * 5),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    assert report.business_m == pytest.approx(1609.344)
    assert report.personal_m == pytest.approx(1609.344 * 2)
    # total_m excludes the unclassified trip's distance entirely.
    assert report.total_m == pytest.approx(1609.344 * 3)
    assert report.business_pct == pytest.approx(100.0 / 3.0)
    assert report.caveats.unclassified_trips == 1


def test_caveat_counts():
    rates = {2026: YearRate(0.700)}
    trips = [
        _at(6, has_gap=True),
        _at(6, snap_status="low_confidence"),
        _at(6, source="manual"),
        _at(6, category="unclassified"),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    assert report.caveats.gap_trips == 1
    assert report.caveats.low_conf_trips == 1
    assert report.caveats.manual_trips == 1
    assert report.caveats.unclassified_trips == 1
    assert report.caveats.missing_rate is False
    assert report.caveats.any is True


def test_business_missing_purpose_caveat_ignores_other_categories_and_blank_space():
    rates = {2026: YearRate(0.700)}
    trips = [
        _at(6, category="business", purpose=None),
        _at(6, category="business", purpose="   "),
        _at(6, category="business", purpose="Deliver documents"),
        _at(6, category="personal", purpose=None),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    assert report.caveats.business_missing_purpose == 2
    assert report.caveats.any is True


def test_missing_rate_year_still_reports_miles():
    report = build_annual_report([_at(6, display_distance_m=1609.344)], {}, TZ, 2026)
    assert report.total_deduction is None
    assert report.caveats.missing_rate is True
    assert report.business_m == pytest.approx(1609.344)  # miles still shown


def test_empty_year_no_crash():
    report = build_annual_report([], {2026: YearRate(0.700)}, TZ, 2026)
    assert report.months == []
    assert report.total_m == 0.0
    assert report.business_pct is None
    assert report.total_deduction is None
    assert report.caveats.any is False


def test_year_attribution_at_utc_local_boundary():
    # 2026-01-01 04:00 UTC is still 2025-12-31 20:00 in Los Angeles — must be
    # excluded from the 2026 report (and would price at the 2025 rate if it
    # were included in a 2025 report), not silently counted into the wrong
    # year via UTC's own month/year.
    rates = {2025: YearRate(0.700), 2026: YearRate(0.725)}
    trip = _trip(
        started_at=datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc),
        display_distance_m=1609.344,
    )
    report_2026 = build_annual_report([trip], rates, TZ, 2026)
    assert report_2026.trip_count == 0
    assert report_2026.business_m == 0.0

    report_2025 = build_annual_report([trip], rates, TZ, 2025)
    assert report_2025.trip_count == 1
    assert report_2025.months[0].month == 12
    assert report_2025.months[0].rate_per_mi == pytest.approx(0.700)


def test_by_vehicle_buckets_business_miles_per_vehicle():
    rates = {2026: YearRate(0.700)}
    trips = [
        _at(6, vehicle_name="Truck", display_distance_m=1609.344 * 3),
        _at(6, vehicle_name="Sedan", display_distance_m=1609.344 * 2),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    by_name = {v.vehicle_name: v for v in report.by_vehicle}
    assert set(by_name) == {"Truck", "Sedan"}
    assert by_name["Truck"].business_m == pytest.approx(1609.344 * 3)
    assert by_name["Truck"].deduction == pytest.approx(0.700 * 3)
    assert by_name["Sedan"].business_m == pytest.approx(1609.344 * 2)
    assert by_name["Sedan"].deduction == pytest.approx(0.700 * 2)


def test_by_vehicle_null_vehicle_name_buckets_under_no_vehicle_label():
    rates = {2026: YearRate(0.700)}
    trips = [_at(6, vehicle_name=None, display_distance_m=1609.344)]
    report = build_annual_report(trips, rates, TZ, 2026)
    assert len(report.by_vehicle) == 1
    assert report.by_vehicle[0].vehicle_name == NO_VEHICLE_LABEL
    assert report.by_vehicle[0].business_m == pytest.approx(1609.344)


def test_by_vehicle_total_m_includes_personal_but_not_unclassified():
    rates = {2026: YearRate(0.700)}
    trips = [
        _at(6, vehicle_name="Truck", category="business", display_distance_m=1609.344),
        _at(6, vehicle_name="Truck", category="personal", display_distance_m=1609.344 * 2),
        _at(6, vehicle_name="Truck", category="unclassified", display_distance_m=1609.344 * 5),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    assert len(report.by_vehicle) == 1
    truck = report.by_vehicle[0]
    assert truck.business_m == pytest.approx(1609.344)
    assert truck.total_m == pytest.approx(1609.344 * 3)  # excludes the unclassified trip


def test_by_vehicle_split_year_two_vehicles_price_at_their_own_months_rate():
    # The load-bearing case: Truck only drives in H1 (58.5c/mi), Sedan only
    # drives in H2 (62.5c/mi) — each vehicle's deduction must reflect the
    # rate in force during *its own* months, not a blended year rate.
    rates = {2026: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    trips = [
        _at(3, vehicle_name="Truck", display_distance_m=1609.344 * 10),
        _at(9, vehicle_name="Sedan", display_distance_m=1609.344 * 10),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    by_name = {v.vehicle_name: v for v in report.by_vehicle}
    assert by_name["Truck"].deduction == pytest.approx(0.585 * 10)
    assert by_name["Sedan"].deduction == pytest.approx(0.625 * 10)
    # Neither vehicle's deduction leaks into the other's.
    assert by_name["Truck"].deduction != by_name["Sedan"].deduction


def test_by_vehicle_duplicate_names_stay_isolated_by_database_id():
    rates = {2026: YearRate(0.700)}
    trips = [
        _at(6, vehicle_id=1, vehicle_name="Car", display_distance_m=1609.344),
        _at(6, vehicle_id=2, vehicle_name="Car", display_distance_m=1609.344 * 2),
    ]
    report = build_annual_report(trips, rates, TZ, 2026)
    assert [(line.vehicle_id, line.business_m) for line in report.by_vehicle] == [
        (1, 1609.344), (2, 1609.344 * 2),
    ]


def test_default_report_year_jan_through_april_uses_prior_year():
    assert default_report_year(datetime(2026, 1, 15)) == 2025
    assert default_report_year(datetime(2026, 4, 30)) == 2025


def test_default_report_year_may_through_december_uses_current_year():
    assert default_report_year(datetime(2026, 5, 1)) == 2026
    assert default_report_year(datetime(2026, 12, 31)) == 2026


# --- build_range_report / quarter_bounds / range_label ---


def test_range_report_full_year_matches_annual_report():
    rates = {2026: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    trips = [
        _at(3, vehicle_name="Truck", display_distance_m=1609.344 * 10),
        _at(9, vehicle_name="Sedan", category="personal", display_distance_m=1609.344 * 4),
        _at(6, category="unclassified", has_gap=True),
    ]
    annual = build_annual_report(trips, rates, TZ, 2026)
    ranged = build_range_report(trips, rates, TZ, date(2026, 1, 1), date(2026, 12, 31))
    assert ranged.year == annual.year
    assert ranged.months == annual.months
    assert ranged.business_m == annual.business_m
    assert ranged.personal_m == annual.personal_m
    assert ranged.business_pct == annual.business_pct
    assert ranged.total_deduction == annual.total_deduction
    assert ranged.rate_periods == annual.rate_periods
    assert ranged.trip_count == annual.trip_count
    assert ranged.caveats == annual.caveats
    assert ranged.by_vehicle == annual.by_vehicle
    assert ranged.start == date(2026, 1, 1)
    assert ranged.end == date(2026, 12, 31)


def test_range_report_mid_year_split_straddle_prices_each_month_at_its_own_rate():
    # 2022-style split (Jul 1), a May 15 - Aug 15 range straddling it.
    rates = {2026: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    trips = [
        _at(5, display_distance_m=1609.344),  # May: first-half rate
        _at(6, display_distance_m=1609.344),  # June: first-half rate
        _at(7, display_distance_m=1609.344),  # July: second-half rate
        _at(8, display_distance_m=1609.344),  # August: second-half rate
    ]
    report = build_range_report(trips, rates, TZ, date(2026, 5, 15), date(2026, 8, 15))
    months_by_num = {m.month: m for m in report.months}
    assert months_by_num[5].rate_per_mi == pytest.approx(0.585)
    assert months_by_num[6].rate_per_mi == pytest.approx(0.585)
    assert months_by_num[7].rate_per_mi == pytest.approx(0.625)
    assert months_by_num[8].rate_per_mi == pytest.approx(0.625)
    assert report.total_deduction == pytest.approx(0.585 * 2 + 0.625 * 2)
    # rate_periods covers only the intersecting months (May-Aug), not all 12.
    assert report.rate_periods == [(0.585, 5, 6), (0.625, 7, 8)]


def test_range_report_local_midnight_boundary_inclusion():
    # 2026-05-15 00:30 Los Angeles is 2026-05-15 07:30 UTC — included when
    # start == 2026-05-15, excluded when start == 2026-05-16 (still the same
    # instant, only the local calendar date changed).
    trip = _trip(
        started_at=datetime(2026, 5, 15, 7, 30, tzinfo=timezone.utc),
        display_distance_m=1609.344,
    )
    included = build_range_report([trip], {2026: YearRate(0.700)}, TZ, date(2026, 5, 15), date(2026, 8, 15))
    assert included.trip_count == 1
    excluded = build_range_report([trip], {2026: YearRate(0.700)}, TZ, date(2026, 5, 16), date(2026, 8, 15))
    assert excluded.trip_count == 0

    # Same check at the *end* boundary: 2026-08-15 23:30 Los Angeles is
    # 2026-08-16 06:30 UTC — included when end == 2026-08-15 (local date),
    # excluded once the range ends the day before.
    trip2 = _trip(
        started_at=datetime(2026, 8, 16, 6, 30, tzinfo=timezone.utc),
        display_distance_m=1609.344,
    )
    included2 = build_range_report([trip2], {2026: YearRate(0.700)}, TZ, date(2026, 5, 15), date(2026, 8, 15))
    assert included2.trip_count == 1
    excluded2 = build_range_report([trip2], {2026: YearRate(0.700)}, TZ, date(2026, 5, 15), date(2026, 8, 14))
    assert excluded2.trip_count == 0


def test_range_report_partial_month_still_buckets_by_whole_month():
    # A range cutting mid-month (May 15) still attributes trips to their
    # actual month (May), not some fractional/partial bucket.
    trips = [_at(5, display_distance_m=1609.344 * 3)]
    report = build_range_report(trips, {2026: YearRate(0.700)}, TZ, date(2026, 5, 15), date(2026, 5, 31))
    assert len(report.months) == 1
    assert report.months[0].month == 5
    assert report.months[0].business_m == pytest.approx(1609.344 * 3)


def test_range_report_reversed_dates_raises_value_error():
    with pytest.raises(ValueError):
        build_range_report([], {}, TZ, date(2026, 8, 15), date(2026, 5, 15))


def test_range_report_cross_year_raises_value_error():
    with pytest.raises(ValueError):
        build_range_report([], {}, TZ, date(2025, 12, 15), date(2026, 1, 15))


def test_quarter_bounds_all_four_quarters():
    assert quarter_bounds(2026, 1) == (date(2026, 1, 1), date(2026, 3, 31))
    assert quarter_bounds(2026, 2) == (date(2026, 4, 1), date(2026, 6, 30))
    assert quarter_bounds(2026, 3) == (date(2026, 7, 1), date(2026, 9, 30))
    assert quarter_bounds(2026, 4) == (date(2026, 10, 1), date(2026, 12, 31))


def test_quarter_bounds_invalid_quarter_raises_value_error():
    with pytest.raises(ValueError):
        quarter_bounds(2026, 5)


def test_range_label_exact_quarter():
    assert range_label(date(2026, 4, 1), date(2026, 6, 30)) == "2026 Q2"


def test_range_label_exact_year():
    assert range_label(date(2026, 1, 1), date(2026, 12, 31)) == "2026 annual"


def test_range_label_arbitrary_span():
    assert range_label(date(2026, 5, 15), date(2026, 8, 15)) == "2026-05-15 – 2026-08-15"
