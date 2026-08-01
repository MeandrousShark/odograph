from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.main import make_templates

TZ = timezone.utc


def _trip(source: str, **overrides) -> dict:
    trip = {
        "id": 42,
        "source": source,
        "started_at": datetime(2026, 3, 1, 9, 0, tzinfo=TZ),
        "ended_at": datetime(2026, 3, 1, 9, 20, tzinfo=TZ),
        "display_distance_m": 8046.72,
        "has_gap": False,
        "notes": None,
        "purpose": None,
        "vehicle_id": None,
        "vehicle_name": None,
        "start_place_name": None,
        "start_lat": 40.0,
        "start_lon": -74.0,
        "start_address": None,
        "end_place_name": None,
        "end_lat": 40.1,
        "end_lon": -74.1,
        "end_address": None,
    }
    trip.update(overrides)
    return trip


def _render(**context):
    templates = make_templates(SimpleNamespace(display_tz=TZ))
    defaults = {
        "trip": None, "remaining": 0, "state": "card",
        "path_geojson": None, "path_snapped_geojson": None,
        "vehicles": [], "filter_from": "", "filter_to": "", "filter_vehicle": "",
        "review_url": "/review",
        "recent_purposes": ["Client meeting"],
    }
    defaults.update(context)
    return templates.env.get_template("_review_card.html").render(**defaults)


def test_manual_trip_card_renders_without_map_block():
    body = _render(trip=_trip("manual"))
    assert "manual-badge" in body
    assert "review-map" not in body
    assert "L.map(" not in body


def test_detected_trip_card_renders_map_block():
    body = _render(trip=_trip("detected"), path_geojson='{"type":"LineString","coordinates":[[1,2],[3,4]]}')
    assert 'id="review-map"' in body
    assert "L.map(" in body


def test_detected_trip_card_tile_layer_follows_configured_map_tile_url():
    # No literal tile.openstreetmap.org left in the template -- both the
    # tile URL and its attribution come from config so the CSP img-src and
    # the rendered map can never name two different hosts.
    templates = make_templates(SimpleNamespace(
        display_tz=TZ, map_tile_url="https://tiles.example.net/{z}/{x}/{y}.png",
        map_tile_attribution="Example attribution",
    ))
    body = templates.env.get_template("_review_card.html").render(
        trip=_trip("detected"), remaining=0, state="card",
        path_geojson=None, path_snapped_geojson=None, vehicles=[],
        filter_from="", filter_to="", filter_vehicle="", review_url="/review",
        recent_purposes=[],
    )
    assert "L.tileLayer(\"https://tiles.example.net/{z}/{x}/{y}.png\"" in body
    assert '"Example attribution"' in body
    assert "tile.openstreetmap.org" not in body


def test_detected_trip_card_draws_single_segment_snapped_path():
    # Regression test for a bug where a single-segment snapped MultiLineString
    # (the normal case -- no recording gap) fell through to the "no path"
    # dashed straight-line fallback, because the old code gated the draw on
    # display.coordinates.length > 1 -- correct for a raw LineString's point
    # count, but wrong for a MultiLineString's segment count (1 segment is
    # the common case, not "no path").
    body = _render(
        trip=_trip("detected"),
        path_snapped_geojson='{"type":"MultiLineString","coordinates":[[[1,2],[3,4]]]}',
    )
    assert "var hasRaw = path && path.coordinates" in body
    assert "display.coordinates.length > 1" not in body


def test_purpose_is_in_atomic_tag_and_skip_form_with_recent_values():
    body = _render(trip=_trip("manual", purpose="Site visit"))
    assert 'name="purpose" value="Site visit"' in body
    assert 'autocomplete="off" data-purpose-input' in body
    assert 'data-purpose-selector' in body
    assert 'hx-post="/review/42/tag"' in body
    assert 'hx-post="/review/42/skip"' in body
    assert '<option value="Client meeting">Client meeting</option>' in body
    assert "datalist" not in body


def test_delete_dialog_is_source_aware_and_preserves_review_filters():
    detected = _render(
        trip=_trip("detected"), filter_from="2026-03-01",
        filter_to="2026-03-31", filter_vehicle="7",
    )
    manual = _render(trip=_trip("manual"))

    for body in (detected, manual):
        assert 'data-trip-delete-open="trip-delete-review-42"' in body
        assert '<dialog id="trip-delete-review-42"' in body
        assert 'data-trip-delete-cancel' in body
        assert 'hx-post="/review/42/delete"' in body
        assert 'hx-target="#review-card"' in body
        assert 'hx-swap="outerHTML"' in body
        assert "Delete trip" in body
        assert "hx-confirm" not in body

    assert "Stored location data is kept" in detected
    assert "restore the trip from Settings" in detected
    assert "permanently deletes the manual trip" in manual
    assert "It cannot be restored" in manual
    assert '<input type="hidden" name="from" value="2026-03-01">' in detected
    assert '<input type="hidden" name="to" value="2026-03-31">' in detected
    assert '<input type="hidden" name="vehicle" value="7">' in detected


def test_review_keyboard_shortcuts_have_no_delete_key_and_pause_for_open_dialog():
    templates = make_templates(SimpleNamespace(display_tz=TZ))
    body = templates.env.get_template("review.html").render(
        trip=_trip("manual"), remaining=1, state="card", path_geojson=None,
        path_snapped_geojson=None, vehicles=[], filter_from="", filter_to="",
        filter_vehicle="", review_url="/review", recent_purposes=[],
        user={"name": "Tester"}, csrf_token="test",
    )

    assert "document.querySelector('dialog[open]')" in body
    assert "e.key === 'd'" not in body
    assert "e.key === 'D'" not in body


def test_done_state_renders_start_over_link_with_filters():
    body = _render(trip=None, state="done", remaining=0, review_url="/review?vehicle=3")
    assert "No more unclassified trips in this pass" in body
    assert 'href="/review?vehicle=3"' in body


def test_empty_state_has_no_start_over_link():
    body = _render(trip=None, state="empty", remaining=0)
    assert "Nothing to review" in body
    assert "Start over" not in body
