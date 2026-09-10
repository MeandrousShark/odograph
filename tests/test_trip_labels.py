"""Unit coverage for the shared start/end trip-label normalizer
(app/ui/_common.py's `normalize_trip_label`) and the manual-trip create
route's route_mode gate, which rejects a nonblank label before any database
access happens (see app/ui/manual.py's add_manual_trip).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.ui import make_router
from app.ui._common import ManualTripValidationError, normalize_trip_label

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route missing: {method} {path}")


ADD = _endpoint("/trips/manual", "POST")

DEFAULT_FORM = {
    "date": "2026-07-14", "start_time": "09:00", "end_time": "10:00", "distance": "10",
    "category": "unclassified", "purpose": "", "notes": "", "vehicle_id": "",
    "route_mode": "none", "start_place": "", "end_place": "",
    "start_lat": "", "start_lon": "", "end_lat": "", "end_lon": "",
    "routed_distance": "", "exclusion": "", "start_label": "", "end_label": "",
}


def _request():
    # No pool/osrm_http_client: the route_mode label gate rejects a nonblank
    # label before add_manual_trip ever touches either, so a fake request
    # this bare is enough to prove it never reaches that code.
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(display_tz=TZ),
    )))


async def _add(**overrides):
    values = dict(DEFAULT_FORM)
    values.update(overrides)
    return await ADD(_request(), user=USER, **values)


def test_normalize_trip_label_trims_surrounding_whitespace():
    assert normalize_trip_label("  Grandma's house  ", "start_label") == "Grandma's house"


@pytest.mark.parametrize("value", ["", "   ", "\t\n  "])
def test_normalize_trip_label_blank_becomes_none(value):
    assert normalize_trip_label(value, "start_label") is None


def test_normalize_trip_label_preserves_unicode():
    assert normalize_trip_label("  Café René ☕  ", "end_label") == "Café René ☕"


def test_normalize_trip_label_accepts_exactly_100_characters():
    value = "x" * 100
    assert normalize_trip_label(value, "start_label") == value


def test_normalize_trip_label_rejects_101_characters():
    with pytest.raises(ManualTripValidationError) as exc:
        normalize_trip_label("x" * 101, "start_label")
    assert "start_label" in exc.value.errors


def test_normalize_trip_label_error_names_the_field_that_failed():
    with pytest.raises(ManualTripValidationError) as exc:
        normalize_trip_label("x" * 101, "end_label")
    assert "end_label" in exc.value.errors
    assert "start_label" not in exc.value.errors


@pytest.mark.parametrize("route_mode,field", [("places", "start_label"), ("map", "end_label")])
def test_create_rejects_nonblank_label_for_any_route_mode_other_than_none(route_mode, field):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_add(route_mode=route_mode, **{field: "Somewhere"}))
    assert exc.value.status_code == 400
