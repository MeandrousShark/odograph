"""Tests for OSRM road-snapping pure logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.snap import MatchPoint, downsample, parse_match_response, radiuses


def _pts(n: int, accuracy=10.0) -> list[MatchPoint]:
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [
        MatchPoint(t=t0 + timedelta(seconds=i * 15), lat=47.6 + i * 0.001, lon=-122.3, accuracy_m=accuracy)
        for i in range(n)
    ]


# ---- downsample ----

def test_downsample_unchanged_when_under_cap():
    pts = _pts(10)
    assert downsample(pts, 250) == pts


def test_downsample_keeps_first_and_last():
    pts = _pts(500)
    result = downsample(pts, 50)
    assert result[0] == pts[0]
    assert result[-1] == pts[-1]


def test_downsample_respects_cap():
    pts = _pts(500)
    result = downsample(pts, 50)
    assert len(result) <= 50


def test_downsample_stays_sorted_no_duplicate_timestamps():
    pts = _pts(103)  # a count that produces a step close to 1, prone to rounding collisions
    result = downsample(pts, 100)
    timestamps = [p.t for p in result]
    assert timestamps == sorted(timestamps)
    assert len(timestamps) == len(set(timestamps))


def test_downsample_degenerate_max_coords_returns_unchanged():
    pts = _pts(10)
    assert downsample(pts, 1) == pts
    assert downsample(pts, 0) == pts


# ---- radiuses ----

def test_radiuses_clamps_low_and_high():
    pts = [
        MatchPoint(t=datetime.now(timezone.utc), lat=0, lon=0, accuracy_m=0.5),   # below min
        MatchPoint(t=datetime.now(timezone.utc), lat=0, lon=0, accuracy_m=500.0),  # above max
        MatchPoint(t=datetime.now(timezone.utc), lat=0, lon=0, accuracy_m=20.0),   # within range
    ]
    result = radiuses(pts, min_r=5.0, max_r=50.0)
    assert result == [5.0, 50.0, 20.0]


def test_radiuses_none_accuracy_maps_to_max():
    pts = [MatchPoint(t=datetime.now(timezone.utc), lat=0, lon=0, accuracy_m=None)]
    assert radiuses(pts, min_r=5.0, max_r=50.0) == [50.0]


def test_radiuses_output_length_matches_input():
    pts = _pts(7)
    assert len(radiuses(pts)) == 7


def test_radiuses_default_floor_absorbs_centerline_offset():
    # Regression: a pinpoint-accurate fix must still get a search radius wide
    # enough to clear the OSM centerline/lane offset, or OSRM drops it as a
    # null tracepoint and truncates the route (trip 17). The default floor
    # is deliberately well above real GPS accuracy -- don't lower it to match
    # accuracy_m.
    pts = [MatchPoint(t=datetime.now(timezone.utc), lat=0, lon=0, accuracy_m=3.0)]
    assert radiuses(pts) == [20.0]


# ---- parse_match_response ----

def _matching(confidence=0.9, distance=1000.0, coords=None):
    return {
        "confidence": confidence,
        "distance": distance,
        "geometry": {"type": "LineString", "coordinates": coords or [[-122.3, 47.6], [-122.29, 47.61]]},
    }


def test_parse_ok_full_confidence_full_match():
    response = {
        "code": "Ok",
        "matchings": [_matching(confidence=0.95)],
        "tracepoints": [{}, {}, {}],
    }
    result = parse_match_response(response, min_confidence=0.5, input_count=3)
    assert result.status == "ok"
    assert result.reason is None
    assert result.distance_m == 1000.0
    assert result.path_geojson["type"] == "MultiLineString"


def test_parse_low_confidence_due_to_matching_confidence():
    response = {
        "code": "Ok",
        "matchings": [_matching(confidence=0.2)],
        "tracepoints": [{}, {}, {}],
    }
    result = parse_match_response(response, min_confidence=0.5, input_count=3)
    assert result.status == "low_confidence"
    assert "confidence" in result.reason


def test_parse_low_confidence_due_to_tracepoint_fraction():
    # High confidence, but only 1 of 3 tracepoints matched (<80%).
    response = {
        "code": "Ok",
        "matchings": [_matching(confidence=0.95)],
        "tracepoints": [{}, None, None],
    }
    result = parse_match_response(response, min_confidence=0.5, input_count=3)
    assert result.status == "low_confidence"
    assert "tracepoints" in result.reason


def test_parse_failed_on_bad_code():
    response = {"code": "NoMatch", "matchings": [], "tracepoints": []}
    result = parse_match_response(response, min_confidence=0.5, input_count=3)
    assert result.status == "failed"
    assert result.path_geojson is None
    assert result.distance_m is None


def test_parse_failed_on_empty_matchings_despite_ok_code():
    response = {"code": "Ok", "matchings": [], "tracepoints": []}
    result = parse_match_response(response, min_confidence=0.5, input_count=3)
    assert result.status == "failed"


def test_parse_stitches_two_matchings_as_multilinestring():
    response = {
        "code": "Ok",
        "matchings": [
            _matching(confidence=0.9, distance=500.0, coords=[[-122.3, 47.6], [-122.29, 47.61]]),
            _matching(confidence=0.4, distance=700.0, coords=[[-122.28, 47.62], [-122.27, 47.63]]),
        ],
        "tracepoints": [{}, {}, {}, {}],
    }
    result = parse_match_response(response, min_confidence=0.5, input_count=4)
    assert result.distance_m == 1200.0
    assert len(result.path_geojson["coordinates"]) == 2  # two separate lines, not joined
    # Worst-of-two confidence (0.4) is below min_confidence (0.5) -> low_confidence.
    assert result.status == "low_confidence"
