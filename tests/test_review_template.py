from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates

TZ = timezone.utc
ROOT = Path(__file__).parents[1]


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
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    defaults = {
        "trip": None, "remaining": 0, "state": "card",
        "path_geojson": None, "path_snapped_geojson": None,
        "vehicles": [], "filter_from": "", "filter_to": "", "filter_vehicle": "",
        "review_url": "/review", "undo_notice": "",
        "recent_purposes": ["Client meeting"],
    }
    defaults.update(context)
    return templates.env.get_template("_review_card.html").render(**defaults)


def _render_page(config=None, **context):
    templates = make_templates(config or SimpleNamespace(display_tz=TZ, app_version="test"))
    defaults = {
        "trip": None, "remaining": 0, "state": "card",
        "path_geojson": None, "path_snapped_geojson": None,
        "vehicles": [], "filter_from": "", "filter_to": "", "filter_vehicle": "",
        "review_url": "/review",
        "recent_purposes": ["Client meeting"],
        "user": {"name": "Tester"}, "csrf_token": "test", "csp_nonce": "", "undo_notice": "",
    }
    defaults.update(context)
    return templates.env.get_template("review.html").render(**defaults)


def test_manual_trip_card_renders_without_map_block():
    body = _render(trip=_trip("manual"))
    assert "manual-badge" in body
    assert "review-map" not in body


def test_no_script_in_swapped_partial():
    # The partial is what htmx swaps in with `outerHTML`; a swapped fragment
    # keeps the document's original CSP nonce, so any inline <script> here
    # would be a mismatched-nonce violation. Map init lives in review.html's
    # page-level script instead, which loads once with a matching nonce.
    body = _render(
        trip=_trip("detected"),
        path_geojson='{"type":"LineString","coordinates":[[1,2],[3,4]]}',
    )
    assert "<script" not in body


def test_detected_trip_card_map_carries_geometry_data_attributes():
    body = _render(
        trip=_trip("detected"),
        path_geojson='{"type":"LineString","coordinates":[[1,2],[3,4]]}',
        path_snapped_geojson='{"type":"MultiLineString","coordinates":[[[1,2],[3,4]]]}',
    )
    assert 'id="review-map"' in body
    assert 'data-start-lat="40.0"' in body
    assert 'data-start-lon="-74.0"' in body
    assert 'data-end-lat="40.1"' in body
    assert 'data-end-lon="-74.1"' in body
    assert 'data-path="{&#34;type&#34;:&#34;LineString&#34;' in body
    assert 'data-path-snapped="{&#34;type&#34;:&#34;MultiLineString&#34;' in body


def test_detected_trip_card_omits_path_attributes_when_no_geometry():
    body = _render(
        trip=_trip("detected"), path_geojson=None, path_snapped_geojson=None,
    )
    assert 'id="review-map"' in body
    assert "data-path=" not in body
    assert "data-path-snapped=" not in body


def test_detected_trip_card_tile_layer_follows_configured_map_tile_url():
    # No literal tile.openstreetmap.org left in the template -- both the
    # tile URL and its attribution come from config so the CSP img-src and
    # the rendered map can never name two different hosts.
    body = _render_page(
        config=SimpleNamespace(
            display_tz=TZ, map_tile_url="https://tiles.example.net/{z}/{x}/{y}.png",
            map_tile_attribution="Example attribution", app_version="test",
        ),
        trip=_trip("detected"),
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
    body = _render_page(
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
        filter_to="2026-03-31", filter_vehicle="7", filter_q="zephyr",
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
    assert '<input type="hidden" name="q" value="zephyr">' in detected
    # The atomic tag/skip form (not the delete dialog) also carries the
    # search term, so Business/Personal/Skip round-trip it too.
    assert 'name="q" value="zephyr"' in detected


def test_review_keyboard_shortcuts_have_no_delete_key_and_pause_for_open_dialog():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "document.querySelector('dialog[open]')" in body
    assert "e.key === 'd'" not in body
    assert "e.key === 'D'" not in body


def test_undo_key_is_z_without_modifiers_and_is_suppressed_by_guards():
    # The dialog-open and input/textarea/select guards must run before the
    # 'z' branch, not just exist somewhere in the file, so undo can never
    # fire while a dialog is open or a form control has focus.
    body = _render_page(trip=_trip("manual"), remaining=1)

    dialog_guard = body.index("document.querySelector('dialog[open]')")
    focus_guard = body.index("tag === 'select'")
    undo_branch = body.index("e.key === 'z'")
    assert dialog_guard < undo_branch
    assert focus_guard < undo_branch
    assert "triggerReviewUndo();" in body
    assert "!e.ctrlKey && !e.metaKey && !e.altKey" in body
    assert "e.key === 'u'" not in body


def test_undo_with_nothing_remembered_is_a_client_side_no_op():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "function triggerReviewUndo()" in body
    assert "if (!lastReviewAction || undoRequestInFlight) return;" in body
    # A failed request preserves the action; successful undo clears it only
    # when htmx reports success.
    assert "if (e.detail.successful) lastReviewAction = null;" in body


def test_undo_reads_kind_and_filters_from_the_acting_button_not_the_post_swap_dom():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "elt.id === 'review-skip'" in body
    assert "elt.id === 'review-business'" in body
    assert "elt.id === 'review-personal'" in body
    assert "elt.closest('form')" in body
    assert "form.elements.from.value" in body
    assert "form.elements.to.value" in body
    assert "form.elements.vehicle.value" in body
    assert "form.elements.q.value" in body


def test_undo_request_targets_review_card_with_outer_html_swap():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "htmx.ajax('POST', '/review/' + action.tripId + '/undo'" in body
    assert "target: '#review-card'" in body
    assert "swap: 'outerHTML'" in body
    assert "kind: action.kind" in body


def test_review_uses_compact_button_shortcut_labels_without_legend_card():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "Keyboard shortcuts" not in body
    assert 'id="review-undo" disabled>Undo (z)</button>' in body
    assert "Business (b)" in body
    assert "Personal (p)" in body
    assert "Skip (s)" in body


def test_review_action_group_stays_together_on_narrow_screens():
    # The four decisions are held on one line by .tags' nowrap, so at 320px the
    # only way to keep the fourth inside the viewport is narrower button
    # padding. That narrowing must stay inside the narrow-width query: applied
    # globally it would cramp the same buttons on desktop, so asserting the
    # rule's presence is not enough without asserting where it lives.
    stylesheet = (ROOT / "static/style.css").read_text()

    assert ".tags { white-space: nowrap; }" in stylesheet

    narrow = stylesheet.split("@media (max-width: 420px) {")[1].split("\n}")[0]
    assert ".tags button { padding-inline: .2rem; }" in narrow
    assert stylesheet.count(".tags button { padding-inline: .2rem; }") == 1


def test_done_state_renders_start_over_link_with_filters():
    body = _render(trip=None, state="done", remaining=0, review_url="/review?vehicle=3")
    assert "No more unclassified trips in this pass" in body
    assert 'href="/review?vehicle=3"' in body
    assert "review-map" not in body
    assert 'id="review-undo" disabled>Undo (z)</button>' in body


def test_undo_notice_is_visible_and_announced():
    body = _render(trip=_trip("manual"), undo_notice="Previous action undone. Trip restored.")
    assert 'role="status"' in body
    assert "Previous action undone. Trip restored." in body


def test_unassigned_review_trip_visually_selects_active_default_vehicle():
    body = _render(
        trip=_trip("manual"),
        vehicles=[{"id": 7, "name": "Default", "is_default": True, "active": True}],
    )
    assert '<option value="7" selected>Default</option>' in body


def test_empty_state_has_no_start_over_link():
    body = _render(trip=None, state="empty", remaining=0)
    assert "Nothing to review" in body
    assert "Start over" not in body
    assert "review-map" not in body


def test_review_page_script_carries_nonce_and_initializes_map():
    body = _render_page(
        trip=_trip("detected"), remaining=1,
        path_geojson='{"type":"LineString","coordinates":[[1,2],[3,4]]}',
        csp_nonce="test-nonce-123",
    )
    assert '<script nonce="test-nonce-123">' in body
    assert "function initReviewMap" in body
    assert "htmx:afterSettle" in body
