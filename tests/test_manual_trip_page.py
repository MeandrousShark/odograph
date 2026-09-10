from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.main import make_templates
from app.ui import make_router

ROOT = Path(__file__).parents[1]
TZ = ZoneInfo("America/Los_Angeles")
PLACES = [
    {"id": 1, "name": "Home", "kind": "home"},
    {"id": 2, "name": "Office", "kind": "work"},
]


def _render_manual(prefill=None, nonce=""):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("manual_trip.html").render(
        vehicles=[{"id": 1, "name": "Car", "is_default": True}],
        recent_purposes=["Client visit"], places=PLACES, user={"sub": "test"},
        csrf="token", manual_prefill=prefill, csp_nonce=nonce,
    )


def _render_archive():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("trips.html").render(
        months=[], vehicles=[], recent_purposes=[], user={"sub": "test"},
        csrf="token", notice="", filter_category="", filter_from="",
        filter_to="", filter_vehicle="", filter_q="", filter_exclusion="",
        export_url=lambda *args, **kwargs: "/", review_url="/review",
        ytd_year=2026, ytd_deduction=None,
    )


def _route(path, method):
    for route in make_router().routes:
        if route.path == path and method in (route.methods or set()):
            return route
    raise AssertionError(f"missing {method} {path}")


def test_manual_page_contains_form_fields_prefill_and_assets():
    body = _render_manual({
        "date": "2026-07-01", "start_time": "08:10",
        "notes": "bridge: Home to Office", "osrm_hint": "~1.5 mi by road",
    }, nonce="manual-nonce")
    assert '<div class="manual-trip-page" id="manual-trip">' in body
    assert '<form class="manual-trip-form" hx-post="/trips/manual" id="manual-trip-form">' in body
    assert 'value="2026-07-01"' in body
    assert 'name="start_time" value="08:10"' in body
    assert 'value="bridge: Home to Office"' in body
    assert "~1.5 mi by road" in body
    assert 'name="route_mode" value="none" checked' in body
    assert 'name="route_mode" value="places"' in body
    assert 'name="route_mode" value="map"' in body
    assert '<link rel="stylesheet" href="/static/vendor/leaflet/leaflet.css">' in body
    assert '<script src="/static/vendor/leaflet/leaflet.js"></script>' in body
    assert '<script nonce="manual-nonce">' in body
    assert "function clearDistanceIfPreviewOwned()" in body
    assert "function onRoutePickerMapClick(event)" in body


def test_manual_page_location_name_inputs_are_visible_and_enabled_for_no_route():
    # route_mode defaults to "none" (see the checked radio), so the
    # No-route-only location name inputs must start visible and enabled --
    # not merely present in the markup somewhere.
    body = _render_manual()
    panel = body.split('data-route-panel="none">', 1)[1].split("</div>", 1)[0]

    assert "Start location name" in panel
    assert "End location name" in panel
    assert 'name="start_label" maxlength="100">' in panel
    assert 'name="end_label" maxlength="100">' in panel
    assert "disabled" not in panel
    assert "hidden" not in panel


def test_archive_owns_no_manual_form_script_or_leaflet_assets():
    body = _render_archive()
    assert 'href="/trips/manual"' in body
    assert 'id="manual-trip-form"' not in body
    assert 'class="route-picker"' not in body
    assert "function clearDistanceIfPreviewOwned()" not in body
    assert "leaflet.js" not in body
    assert "leaflet.css" not in body


def test_manual_page_literal_route_precedes_trip_detail_and_requires_auth():
    routes = make_router().routes
    manual_index = next(
        i for i, route in enumerate(routes)
        if route.path == "/trips/manual" and "GET" in route.methods
    )
    detail_index = next(
        i for i, route in enumerate(routes)
        if route.path == "/trips/{trip_id}" and "GET" in route.methods
    )
    assert manual_index < detail_index
    route = _route("/trips/manual", "GET")
    assert {dependency.call.__name__ for dependency in route.dependant.dependencies} == {"require_user"}


def test_archive_manual_query_redirects_to_canonical_page():
    route = _route("/trips", "GET")
    request = SimpleNamespace()
    response = asyncio.run(route.endpoint(
        request, {"sub": "test"}, manual_date="2026-07-01",
        manual_start="08:10", manual_notes="bridge: Home", bridge_trip="42",
        category="business", q="ignored archive search",
    ))
    assert response.status_code == 302
    location = response.headers["location"]
    assert urlsplit(location).path == "/trips/manual"
    assert urlsplit(location).fragment == "manual-trip"
    assert "manual_date=2026-07-01" in location
    assert "manual_start=08%3A10" in location
    assert "bridge_trip=42" in location
    assert "manual_open" not in location
    assert "business" not in location
    assert "ignored" not in location


def test_archive_manual_open_redirects_without_prefill_query():
    route = _route("/trips", "GET")
    response = asyncio.run(route.endpoint(
        SimpleNamespace(), {"sub": "test"}, manual_date="", manual_start="",
        manual_notes="", bridge_trip="", manual_open="true",
    ))
    assert response.status_code == 302
    assert response.headers["location"] == "/trips/manual#manual-trip"
