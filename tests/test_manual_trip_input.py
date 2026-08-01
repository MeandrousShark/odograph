from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.rates import METERS_PER_MILE
from app.ui import ManualTripValidationError, parse_manual_trip_input


TZ = ZoneInfo("America/Los_Angeles")


def test_manual_input_uses_display_timezone_and_converts_distance():
    started, ended, distance_m = parse_manual_trip_input(
        "2026-07-14", "09:15", "10:45", "12.5", TZ
    )

    assert started.tzinfo == TZ
    assert started.hour == 9 and ended.hour == 10
    assert distance_m == pytest.approx(12.5 * METERS_PER_MILE)


def test_manual_input_treats_non_later_end_as_overnight():
    started, ended, _ = parse_manual_trip_input(
        "2026-07-14", "23:30", "01:00", "1", TZ
    )

    assert ended - started == timedelta(hours=1, minutes=30)
    assert ended.date().isoformat() == "2026-07-15"


@pytest.mark.parametrize("date_value,start,end", [
    ("bad", "09:00", "10:00"),
    ("2026-07-14", "bad", "10:00"),
    ("2026-07-14", "09:00", "bad"),
])
def test_manual_input_rejects_invalid_dates_and_times(date_value, start, end):
    with pytest.raises(ManualTripValidationError) as exc:
        parse_manual_trip_input(date_value, start, end, "1", TZ)
    assert exc.value.errors


@pytest.mark.parametrize(
    "distance", ["nan", "inf", "-inf", "0", "-1", "1e308", "not-a-number"]
)
def test_manual_input_rejects_non_finite_or_non_positive_distance(distance):
    with pytest.raises(ManualTripValidationError) as exc:
        parse_manual_trip_input("2026-07-14", "09:00", "10:00", distance, TZ)
    assert "distance" in exc.value.errors
