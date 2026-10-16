"""Pure /ingest value checks that keep malformed fixes from reaching storage."""
from __future__ import annotations

import json
import math
import time

import pytest

from app.ingest import _int, _jsonb_encode, _num, _validate_location


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400), math.inf, -math.inf, math.nan, 1e10, -1e10, True, "5", None])
def test_reading_outside_bounds_becomes_none(value):
    assert _num(value) is None


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (12, 12.0), (-20.5, -20.5), (1e9, 1e9)])
def test_reading_inside_bounds_is_kept(value, expected):
    assert _num(value) == expected


@pytest.mark.parametrize("value", [99999, -1, 101, -0.5, 100.5, 10 ** 400, math.inf, math.nan, True])
def test_battery_outside_percent_range_becomes_none(value):
    assert _int(value) is None


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (100, 100), (55.7, 55)])
def test_battery_inside_percent_range_is_kept(value, expected):
    assert _int(value) == expected


def test_huge_integer_tst_is_a_drop_reason():
    payload = {"_type": "location", "lat": 47.6, "lon": -122.3, "tst": 10 ** 400}
    assert _validate_location(payload) == "tst out of range"
    payload["tst"] = int(time.time()) - 60
    assert _validate_location(payload) is None


def test_nesting_deeper_than_the_encoders_is_a_drop_reason():
    payload = json.loads('{"x":' + "[" * 2000 + "]" * 2000 + "}")
    assert _jsonb_encode(payload) == (None, "payload nested too deeply")
