"""Stored routes become MultiLineString parts without joining gaps."""
from __future__ import annotations

import json

import pytest

from app.trip_reads import route_parts


def test_line_string_becomes_one_part():
    line = [[-122.33, 47.6], [-122.32, 47.61]]
    assert route_parts(json.dumps({"type": "LineString", "coordinates": line})) == [line]


def test_multi_line_string_keeps_gap_parts_separate():
    parts = [[[-122.33, 47.6], [-122.325, 47.605]], [[-122.315, 47.615], [-122.31, 47.62]]]
    assert route_parts(json.dumps({"type": "MultiLineString", "coordinates": parts})) == parts


def test_missing_route_is_none_and_other_geometry_is_refused():
    assert route_parts(None) is None
    with pytest.raises(ValueError):
        route_parts(json.dumps({"type": "Point", "coordinates": [0, 0]}))
