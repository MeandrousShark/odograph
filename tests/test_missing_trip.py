from __future__ import annotations

from datetime import datetime, timezone

from app.missing_trip import missing_trip_badge

TZ = timezone.utc


def _trip(**overrides) -> dict:
    trip = {
        "id": 99,
        "prev_end_gap_m": 2000.0,
        "prev_trip_ended_at": datetime(2026, 7, 1, 8, 10, tzinfo=TZ),
        "prev_trip_end_lat": 47.0,
        "prev_trip_end_lon": -122.0,
        "prev_trip_end_place_name": None,
        "missing_trip_covered": False,
        "start_place_name": None,
        "start_lat": 47.02,
        "start_lon": -122.0,
        "start_address": None,
    }
    trip.update(overrides)
    return trip


def test_flags_when_gap_exceeds_threshold():
    badge = missing_trip_badge(_trip(), threshold_m=1000.0, tz=TZ)
    assert badge is not None
    assert badge.gap_m == 2000.0


def test_no_badge_when_gap_at_or_below_threshold():
    assert missing_trip_badge(_trip(prev_end_gap_m=1000.0), threshold_m=1000.0, tz=TZ) is None
    assert missing_trip_badge(_trip(prev_end_gap_m=999.0), threshold_m=1000.0, tz=TZ) is None


def test_zero_threshold_disables_feature_entirely():
    assert missing_trip_badge(_trip(), threshold_m=0, tz=TZ) is None
    assert missing_trip_badge(_trip(), threshold_m=-1, tz=TZ) is None


def test_no_badge_without_a_predecessor_gap():
    assert missing_trip_badge(_trip(prev_end_gap_m=None), threshold_m=1000.0, tz=TZ) is None
    assert missing_trip_badge(_trip(prev_trip_ended_at=None), threshold_m=1000.0, tz=TZ) is None


def test_covering_manual_trip_suppresses_badge():
    assert missing_trip_badge(
        _trip(missing_trip_covered=True), threshold_m=1000.0, tz=TZ,
    ) is None


def test_prefill_url_carries_date_start_time_notes_hint_and_bridge_trip():
    badge = missing_trip_badge(_trip(), threshold_m=1000.0, tz=TZ)
    assert badge is not None
    assert badge.prefill_url.startswith("/trips/manual?")
    assert "#manual-trip" not in badge.prefill_url
    assert "manual_date=2026-07-01" in badge.prefill_url
    assert "manual_start=08%3A10" in badge.prefill_url
    assert "bridge_trip=99" in badge.prefill_url
    assert "manual_notes=bridge%3A" in badge.prefill_url


def test_prefill_notes_hint_prefers_named_place_over_coordinates():
    badge = missing_trip_badge(
        _trip(prev_trip_end_place_name="Work", start_place_name="Home"),
        threshold_m=1000.0, tz=TZ,
    )
    assert badge is not None
    assert "Work" in badge.prefill_url
    assert "Home" in badge.prefill_url
