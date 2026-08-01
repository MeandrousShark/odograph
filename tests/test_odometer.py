"""Pure tests for the odometer reconciliation core and reminder helpers."""
from __future__ import annotations

import pytest

from app.odometer import (
    METERS_PER_MILE,
    OdometerReading,
    VehicleCoverage,
    coverage_line,
    latest_quarter_start,
    reconcile,
    vehicle_coverage_for_report,
    vehicles_due_for_reminder,
)
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")


def _reading(day: int, mi: float, month: int = 1, year: int = 2026) -> OdometerReading:
    return OdometerReading(
        recorded_at=datetime(year, month, day, tzinfo=timezone.utc),
        odometer_m=mi * METERS_PER_MILE,
    )


def _trip(day: int, mi: float, month: int = 1, year: int = 2026):
    return (datetime(year, month, day, 12, tzinfo=timezone.utc), mi * METERS_PER_MILE)


# ---- reconcile ----

def test_reconcile_exact_coverage():
    readings = [_reading(1, 1000), _reading(10, 1100)]
    trips = [_trip(5, 100)]
    result = reconcile(readings, trips)
    assert len(result.intervals) == 1
    iv = result.intervals[0]
    assert iv.odometer_delta_m == pytest.approx(100 * METERS_PER_MILE)
    assert iv.detected_m == pytest.approx(100 * METERS_PER_MILE)
    assert iv.coverage == pytest.approx(1.0)
    assert iv.gap_m == pytest.approx(0.0, abs=1e-6)
    assert iv.data_error is False
    assert result.total_coverage == pytest.approx(1.0)


def test_reconcile_under_read_has_positive_gap():
    readings = [_reading(1, 1000), _reading(10, 1100)]
    trips = [_trip(5, 60)]
    result = reconcile(readings, trips)
    iv = result.intervals[0]
    assert iv.coverage == pytest.approx(0.6)
    assert iv.gap_m == pytest.approx(40 * METERS_PER_MILE)


def test_reconcile_over_read_coverage_not_clamped():
    readings = [_reading(1, 1000), _reading(10, 1100)]
    trips = [_trip(5, 150)]
    result = reconcile(readings, trips)
    iv = result.intervals[0]
    assert iv.coverage == pytest.approx(1.5)
    assert iv.gap_m == pytest.approx(-50 * METERS_PER_MILE)


def test_reconcile_odometer_rollback_is_data_error():
    readings = [_reading(1, 1100), _reading(10, 1000)]
    result = reconcile(readings, [])
    iv = result.intervals[0]
    assert iv.data_error is True
    assert iv.coverage is None
    # A data-error interval must not corrupt the aggregate totals.
    assert result.total_odometer_delta_m == 0.0
    assert result.total_coverage is None


def test_reconcile_duplicate_reading_same_value_is_data_error():
    readings = [_reading(1, 1000), _reading(1, 1000)]
    result = reconcile(readings, [])
    assert result.intervals[0].data_error is True


def test_reconcile_single_reading_has_no_intervals():
    result = reconcile([_reading(1, 1000)], [_trip(1, 5)])
    assert result.intervals == []
    assert result.total_odometer_delta_m == 0.0
    assert result.total_detected_m == 0.0
    assert result.total_coverage is None


def test_reconcile_no_readings_no_crash():
    result = reconcile([], [])
    assert result.intervals == []
    assert result.total_coverage is None


def test_reconcile_multiple_intervals_each_priced_independently():
    readings = [_reading(1, 1000), _reading(10, 1100), _reading(20, 1150)]
    trips = [_trip(5, 100), _trip(15, 25)]
    result = reconcile(readings, trips)
    assert len(result.intervals) == 2
    assert result.intervals[0].coverage == pytest.approx(1.0)
    assert result.intervals[1].coverage == pytest.approx(0.5)
    assert result.total_odometer_delta_m == pytest.approx(150 * METERS_PER_MILE)
    assert result.total_detected_m == pytest.approx(125 * METERS_PER_MILE)


def test_reconcile_unsorted_readings_are_sorted_first():
    readings = [_reading(10, 1100), _reading(1, 1000)]
    result = reconcile(readings, [_trip(5, 100)])
    assert len(result.intervals) == 1
    assert result.intervals[0].start == readings[1].recorded_at


def test_reconcile_boundary_trip_attributed_by_started_at():
    r1, r2, r3 = _reading(1, 1000), _reading(10, 1100), _reading(20, 1200)
    # A trip starting exactly at r2's timestamp belongs to the SECOND
    # interval ([r2, r3)), not the first ([r1, r2)) — start is inclusive on
    # its own interval only.
    boundary_trip = (r2.recorded_at, 30 * METERS_PER_MILE)
    result = reconcile([r1, r2, r3], [boundary_trip])
    assert result.intervals[0].detected_m == 0.0
    assert result.intervals[1].detected_m == pytest.approx(30 * METERS_PER_MILE)


def test_reconcile_ignores_trips_outside_any_vehicles_readings():
    # Vehicle isolation is a caller-side filter: reconcile() itself has no
    # vehicle concept, so passing only one vehicle's trips/readings in is
    # what actually isolates them.
    readings = [_reading(1, 1000), _reading(10, 1100)]
    other_vehicle_trip = _trip(30, 500)  # well outside the [r1, r2) interval
    result = reconcile(readings, [other_vehicle_trip])
    assert result.intervals[0].detected_m == 0.0


# ---- vehicle_coverage_for_report ----

def test_vehicle_coverage_for_report_omits_vehicles_with_fewer_than_two_readings():
    year_start = datetime(2026, 1, 1, tzinfo=TZ)
    next_year_start = datetime(2027, 1, 1, tzinfo=TZ)
    readings_by_vehicle = {"Truck": [_reading(1, 1000)], "Sedan": [_reading(1, 500), _reading(20, 550)]}
    lines = vehicle_coverage_for_report(readings_by_vehicle, {}, year_start, next_year_start)
    assert [line.vehicle_name for line in lines] == ["Sedan"]


def test_vehicle_coverage_for_report_flags_partial_year_span():
    year_start = datetime(2026, 1, 1, tzinfo=TZ)
    next_year_start = datetime(2027, 1, 1, tzinfo=TZ)
    readings_by_vehicle = {"Truck": [_reading(1, 1000, month=3), _reading(1, 1050, month=9)]}
    trips_by_vehicle = {"Truck": [_trip(15, 30, month=3)]}
    lines = vehicle_coverage_for_report(year_start=year_start, next_year_start=next_year_start,
                                         readings_by_vehicle=readings_by_vehicle,
                                         trips_by_vehicle=trips_by_vehicle)
    assert len(lines) == 1
    assert lines[0].fully_bracketed is False
    assert lines[0].coverage == pytest.approx(30 / 50)


def test_vehicle_coverage_for_report_omits_vehicle_whose_only_interval_is_a_data_error():
    year_start = datetime(2026, 1, 1, tzinfo=TZ)
    next_year_start = datetime(2027, 1, 1, tzinfo=TZ)
    readings_by_vehicle = {"Truck": [_reading(1, 1000), _reading(10, 900)]}  # went backwards
    lines = vehicle_coverage_for_report(readings_by_vehicle, {}, year_start, next_year_start)
    assert lines == []


def test_vehicle_coverage_for_report_keeps_duplicate_names_isolated_by_key():
    year_start = datetime(2026, 1, 1, tzinfo=TZ)
    next_year_start = datetime(2027, 1, 1, tzinfo=TZ)
    readings = {
        (1, "Car"): [_reading(1, 1000), _reading(10, 1100)],
        (2, "Car"): [_reading(1, 500), _reading(10, 550)],
    }
    trips = {
        (1, "Car"): [_trip(5, 80)],
        (2, "Car"): [_trip(5, 10)],
    }
    lines = vehicle_coverage_for_report(readings, trips, year_start, next_year_start)
    assert len(lines) == 2
    assert [line.coverage for line in lines] == pytest.approx([0.8, 0.2])


def test_coverage_line_formats_percent_and_gap():
    line = VehicleCoverage(
        vehicle_name="Truck",
        span_start=datetime(2026, 3, 1, tzinfo=TZ),
        span_end=datetime(2026, 9, 1, tzinfo=TZ),
        coverage=0.75,
        gap_m=25 * METERS_PER_MILE,
        fully_bracketed=False,
    )
    text = coverage_line(line)
    assert text.startswith("Truck: GPS captured 75.0% of odometer miles (25.0 mi unaccounted)")
    assert "partial-year" in text


def test_coverage_line_handles_none_coverage():
    line = VehicleCoverage(
        vehicle_name="Truck", span_start=datetime(2026, 1, 1, tzinfo=TZ),
        span_end=datetime(2026, 1, 1, tzinfo=TZ), coverage=None, gap_m=0.0, fully_bracketed=True,
    )
    assert "—" in coverage_line(line)


# ---- latest_quarter_start ----

def test_latest_quarter_start_within_a_quarter_returns_that_quarters_start():
    now = datetime(2026, 8, 15, 12, tzinfo=TZ)
    assert latest_quarter_start(now, 9) == datetime(2026, 7, 1, 9, tzinfo=TZ)


def test_latest_quarter_start_on_boundary_day_after_hour():
    now = datetime(2026, 7, 1, 9, 1, tzinfo=TZ)
    assert latest_quarter_start(now, 9) == datetime(2026, 7, 1, 9, tzinfo=TZ)


def test_latest_quarter_start_on_boundary_day_before_hour_uses_prior_quarter():
    now = datetime(2026, 7, 1, 8, 59, tzinfo=TZ)
    assert latest_quarter_start(now, 9) == datetime(2026, 4, 1, 9, tzinfo=TZ)


def test_latest_quarter_start_crosses_year_boundary():
    now = datetime(2026, 1, 1, 8, 0, tzinfo=TZ)
    assert latest_quarter_start(now, 9) == datetime(2025, 10, 1, 9, tzinfo=TZ)


def test_latest_quarter_start_requires_timezone_aware_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_quarter_start(datetime(2026, 7, 1, 9), 9)


# ---- vehicles_due_for_reminder ----

def test_vehicles_due_for_reminder_excludes_vehicles_already_logged():
    active = [(1, "Truck"), (2, "Sedan")]
    due = vehicles_due_for_reminder(active, {1})
    assert due == ["Sedan"]


def test_vehicles_due_for_reminder_all_logged_returns_empty():
    active = [(1, "Truck"), (2, "Sedan")]
    assert vehicles_due_for_reminder(active, {1, 2}) == []


def test_vehicles_due_for_reminder_sorted_by_name():
    active = [(1, "Zed"), (2, "Alpha")]
    assert vehicles_due_for_reminder(active, set()) == ["Alpha", "Zed"]
