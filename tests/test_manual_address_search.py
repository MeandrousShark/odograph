"""Manual trip form: the Search route mode (address autocomplete per endpoint).

Template shape and the server-side provider gate; stored-trip behavior is in
tests/test_manual_address_search_db.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from html.parser import HTMLParser
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.main import make_templates
from app.ui.manual import _resolve_manual_route_endpoints

TZ = ZoneInfo("America/Los_Angeles")
COORDS = dict(start_lat="47.6", start_lon="-122.3", end_lat="47.7", end_lon="-122.2")


def _render(geocode_enabled):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("manual_trip.html").render(
        vehicles=[], places=[], recent_purposes=[], user={"sub": "test"}, csrf="token",
        manual_prefill=None, csp_nonce="", geocode_enabled=geocode_enabled,
    )


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _parse(body):
    parser = _Collector()
    parser.feed(body)
    return parser.tags


def test_search_mode_is_offered_with_a_geocoder():
    tags = _parse(_render(True))

    radios = [a["value"] for t, a in tags if t == "input" and a.get("name") == "route_mode"]
    assert radios == ["none", "places", "map", "search"]
    assert any(t == "div" and a.get("data-route-panel") == "search" for t, a in tags)


def test_search_mode_is_absent_without_a_geocoder_and_other_modes_stay():
    tags = _parse(_render(False))

    radios = [a["value"] for t, a in tags if t == "input" and a.get("name") == "route_mode"]
    assert radios == ["none", "places", "map"]
    panels = {a["data-route-panel"] for t, a in tags if "data-route-panel" in a}
    assert panels == {"none", "places", "map"}
    assert not any("data-route-search" in a for _, a in tags)


def test_search_inputs_are_not_submitted_with_the_trip_form():
    tags = _parse(_render(True))
    ids = {a["id"] for _, a in tags if "id" in a}

    inputs = [a for t, a in tags if t == "input" and "data-route-search" in a]
    assert [a["data-route-search"] for a in inputs] == ["start", "end"]
    for attrs in inputs:
        # The query parameter stays q, but the input's form owner is an id that
        # does not exist, so it belongs to no form and is never submitted.
        assert attrs["name"] == "q"
        assert attrs["form"] not in ids
        assert attrs["hx-get"] == "/places/search"
        assert attrs["hx-trigger"] == "keyup delay:400ms"
        assert attrs["hx-sync"] == "this:replace"
        assert attrs["hx-target"] == f"#route-search-results-{attrs['data-route-search']}"
    results = {a["id"]: a for t, a in tags if "data-route-search-results" in a}
    assert set(results) == {"route-search-results-start", "route-search-results-end"}


@pytest.mark.parametrize("geocode_enabled", [True, False])
def test_hidden_coordinates_are_shared_and_not_inside_the_map_panel(geocode_enabled):
    body = _render(geocode_enabled)

    for name in ("start_lat", "start_lon", "end_lat", "end_lon"):
        assert body.count(f'name="{name}"') == 1
    map_panel = body.split('data-route-panel="map"', 1)[1].split("</div>", 1)[0]
    assert 'type="hidden"' not in map_panel


def test_search_panel_has_a_polite_status_and_44px_result_targets():
    body = _render(True)
    css = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert 'role="status" aria-live="polite" data-route-search-status' in body
    assert ".route-search-field .address-results button" in css
    assert "min-height: 44px" in css.split(".route-search-field .address-results button", 1)[1].split("}", 1)[0]


def test_search_mode_is_refused_without_a_geocoder_before_any_database_access():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resolve_manual_route_endpoints(
            None, "search", "", "", **COORDS, geocoding_available=False,
        ))
    assert exc.value.status_code == 400
    assert "47.6" not in exc.value.detail


def test_search_mode_defaults_to_refused():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resolve_manual_route_endpoints(None, "search", "", "", **COORDS))
    assert exc.value.status_code == 400


def test_search_mode_resolves_like_map_with_a_geocoder():
    endpoints = asyncio.run(_resolve_manual_route_endpoints(
        None, "search", "", "", **COORDS, geocoding_available=True,
    ))
    assert (endpoints.from_lat, endpoints.from_lon) == (47.6, -122.3)
    assert (endpoints.to_lat, endpoints.to_lon) == (47.7, -122.2)
    assert endpoints.start_place_id is None and endpoints.end_place_id is None


@pytest.mark.parametrize("override", [
    {"start_lat": "91"}, {"start_lon": "-181"}, {"end_lat": ""}, {"end_lon": "nope"},
    {"end_lat": "nan"},
])
def test_search_mode_rejects_bad_or_missing_coordinates_without_echo(override):
    values = {**COORDS, **override}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resolve_manual_route_endpoints(
            None, "search", "", "", **values, geocoding_available=True,
        ))
    assert exc.value.status_code == 400
    for value in override.values():
        assert value == "" or value not in exc.value.detail
