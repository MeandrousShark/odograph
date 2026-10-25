"""Pure parts of API trip writes: instant and key parsing, the canonical
request digest and the error registry."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api_errors import ERRORS, ApiError
from app.api_trips import _idempotency_key, _instant, _write_values
from app.trip_writes import ManualTrip, manual_trip_digest

UTC = timezone.utc


@pytest.mark.parametrize("text,expected", [
    ("2026-04-14T15:00:00Z", datetime(2026, 4, 14, 15, tzinfo=UTC)),
    ("2026-04-14t15:00:00z", datetime(2026, 4, 14, 15, tzinfo=UTC)),
    ("2026-04-14T08:00:00-07:00", datetime(2026, 4, 14, 15, tzinfo=UTC)),
    ("2026-04-14T15:00:00.5Z", datetime(2026, 4, 14, 15, 0, 0, 500000, tzinfo=UTC)),
    ("2026-04-14T15:00:00.123456789Z", datetime(2026, 4, 14, 15, 0, 0, 123456, tzinfo=UTC)),
])
def test_instants_accept_rfc3339_with_an_offset(text, expected):
    assert _instant(text) == expected


@pytest.mark.parametrize("text", [
    "2026-04-14T15:00:00", "2026-04-14", "2026-04-14 15:00:00Z", "2026-04-14T15:00Z",
    "2026-13-14T15:00:00Z", "2026-04-14T24:00:00Z", "2026-04-14T15:00:60Z", "1776193200",
    "2026-04-14T15:00:00+0700", " 2026-04-14T15:00:00Z", "2026-04-14T15:00:00Z\n",
])
def test_instants_refuse_anything_else(text):
    assert _instant(text) is None


def test_write_values_refuse_nul_and_bad_instants_by_field():
    with pytest.raises(ApiError) as refused:
        _write_values({"notes": "a\x00b", "started_at": "yesterday", "purpose": "ok"})
    assert refused.value.code == "validation_failed"
    assert refused.value.fields == {"notes": "This value is not valid.",
                                    "started_at": "Enter a time with its offset."}
    parsed = _write_values({"ended_at": "2026-04-14T15:00:00Z", "category": "business"})
    assert parsed == {"ended_at": datetime(2026, 4, 14, 15, tzinfo=UTC), "category": "business"}


def test_idempotency_keys_are_canonical_uuids():
    key = uuid.uuid4()
    assert _idempotency_key(str(key)) == key
    assert _idempotency_key(str(key).upper()) == key
    for bad in ("", "x", key.hex, "{" + str(key) + "}", "urn:uuid:" + str(key), str(key) + " "):
        with pytest.raises(ApiError) as refused:
            _idempotency_key(bad)
        assert refused.value.fields == {"Idempotency-Key": "This value is not valid."}


def _trip(**values):
    start = datetime(2026, 4, 14, 15, tzinfo=UTC)
    return ManualTrip(**{"started_at": start, "ended_at": start + timedelta(minutes=30),
                         "distance_m": 1000.0, **values})


def test_the_digest_covers_the_normalized_request():
    base = manual_trip_digest(_trip())
    assert len(base) == 32
    shifted = datetime(2026, 4, 14, 8, tzinfo=timezone(timedelta(hours=-7)))
    assert manual_trip_digest(_trip(started_at=shifted)) == base
    for changed in (_trip(distance_m=1000.5), _trip(category="business"), _trip(purpose="p"),
                    _trip(notes="n"), _trip(vehicle_id=1), _trip(exclusion="not_deductible"),
                    _trip(start_label="a"), _trip(end_label="b"),
                    _trip(ended_at=datetime(2026, 4, 14, 15, 31, tzinfo=UTC))):
        assert manual_trip_digest(changed) != base
    with pytest.raises(ValueError):
        manual_trip_digest(_trip(path_geojson={"type": "LineString", "coordinates": []},
                                 start_point=(0, 0), end_point=(1, 1)))


def test_write_error_codes_are_registered():
    assert ERRORS["idempotency_key_reused"][0] == 409
    assert ERRORS["storage_full"][0] == 409
    assert ERRORS["trip_in_progress"][0] == 409
