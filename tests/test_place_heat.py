"""Places heatmap: the grid, parameter validation, route order and lane.

Database reads, row security, filters and the response headers are in
tests/test_place_heat_db.py.
"""
from __future__ import annotations

import math
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from starlette.routing import Match

from app.detector.core import haversine_m
from app.place_heat import CELL_M, COORD_DECIMALS, LAT_STEP, cell_centre, cell_index, parse_range
from app.ui import make_router

TZ = ZoneInfo("America/Los_Angeles")


def _snap(lat, lon):
    return cell_centre(*cell_index(lat, lon))


@pytest.mark.parametrize("lat", [0.0, 37.77, -33.87, 60.17, 78.22])
def test_cells_are_about_fifty_metres_each_way(lat):
    lon = 12.34
    row, col = cell_index(lat, lon)
    centre = cell_centre(row, col)
    # Rows each have their own longitude step, so measure north along a meridian.
    north = cell_centre(row + 1, col)[0]
    east = cell_centre(row, col + 1)
    assert haversine_m(centre[0], lon, north, lon) == pytest.approx(CELL_M, rel=0.01)
    assert haversine_m(*centre, *east) == pytest.approx(CELL_M, rel=0.01)


@pytest.mark.parametrize("lat, lon", [
    (37.774929, -122.419416), (-33.868820, 151.209296), (60.169857, 24.938379), (0.0, 0.0),
])
def test_a_point_snaps_to_its_own_cell_centre(lat, lon):
    centre = _snap(lat, lon)
    assert centre != (lat, lon)
    assert cell_index(*centre) == cell_index(lat, lon)
    assert _snap(*centre) == centre
    # Inside its cell: within half a diagonal of the centre.
    assert haversine_m(lat, lon, *centre) <= CELL_M * math.sqrt(2) / 2 * 1.01
    assert all(round(value, COORD_DECIMALS) == value for value in centre)


def test_nearby_points_share_a_cell_and_snapping_is_deterministic():
    row, col = cell_index(37.7749, -122.4194)
    lat, lon = cell_centre(row, col)
    a = (lat + LAT_STEP / 4, lon)
    b = (lat - LAT_STEP / 4, lon)
    assert cell_index(*a) == cell_index(*b) == (row, col)
    assert _snap(*a) == _snap(*b) == (lat, lon)


def test_the_poles_stay_inside_valid_rows():
    for lat in (90.0, -90.0):
        north, _ = _snap(lat, 10.0)
        assert -90.0 < north < 90.0


def test_range_defaults_to_this_year_through_today():
    span = parse_range("", "", TZ)
    assert span.start == date(span.end.year, 1, 1)
    assert span.start <= span.end
    assert parse_range("2025-03-01", "", TZ).end == span.end
    assert parse_range("", "2026-12-31", TZ).start == span.start


def test_range_bounds_are_local_midnights_with_to_inclusive():
    start, end = parse_range("2025-03-01", "2025-03-31", TZ).bounds(TZ)
    assert start.isoformat() == "2025-03-01T00:00:00-08:00"
    assert end.isoformat() == "2025-04-01T00:00:00-07:00"
    assert parse_range("2025-06-01", "2025-06-01", TZ).start == date(2025, 6, 1)


@pytest.mark.parametrize("from_, to", [
    ("2025-03-02", "2025-03-01"),
    ("2025-3-1", "2025-03-31"),
    ("20250301", "2025-03-31"),
    ("2025-02-30", "2025-03-31"),
    ("2025-03-01", "yesterday"),
    ("2025-03-01", "9999-12-31"),
    ("0000-01-01", "2025-03-31"),
])
def test_bad_ranges_are_refused(from_, to):
    with pytest.raises(HTTPException) as caught:
        parse_range(from_, to, TZ)
    assert caught.value.status_code == 422


def _first_matching_route(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path}
    for route in make_router().routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_heat_is_not_captured_by_the_place_page():
    assert _first_matching_route("GET", "/places/heat").path == "/places/heat"
    assert _first_matching_route("GET", "/places/7").path == "/places/{place_id}"


def test_heat_has_the_foreground_lane():
    lanes = {
        (method, route.path): route.capacity_lane
        for route in make_router().routes for method in route.methods
    }
    assert lanes[("GET", "/places/heat")] == "foreground"
