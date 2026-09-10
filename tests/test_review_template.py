from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates
from tests.test_trip_edit_template import _render as _render_trip_edit
from tests.test_trip_row_template import _render_detail as _render_trip_detail
from tests.test_trip_row_template import _trip as _row_trip
from tests.test_trips_template import _css_rule

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
        "category": "unclassified",
        "exclusion": None,
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


def test_review_card_exclusion_selector_saves_without_swapping_or_becoming_undo():
    body = _render(trip=_trip("manual", exclusion="not_deductible"))

    assert '<label for="review-exclusion">Exclusion</label>' in body
    assert 'id="review-exclusion" name="exclusion"' in body
    assert 'hx-post="/review/42/exclusion"' in body
    assert 'hx-trigger="change" hx-swap="none"' in body
    assert '<option value="" >Normal trip</option>' in body
    assert '<option value="not_my_vehicle" >Not one of my vehicles</option>' in body
    assert '<option value="not_deductible" selected>My vehicle, someone else drove</option>' in body
    assert "review-not-my-vehicle" not in body
    assert "review-not-deductible" not in body


def test_review_category_group_offers_only_business_and_personal_unselected():
    # Review only ever holds unclassified trips, so it takes the two-option
    # variant: no Unset choice, and nothing selected on first paint. Skip is
    # the path that leaves a trip unclassified, and re-activating the chosen
    # option is what clears it again.
    body = _render(trip=_trip("manual"))
    group = body.split('<fieldset class="category-segmented category-segmented-pair">', 1)[1].split(
        "</fieldset>", 1
    )[0]

    assert "<legend>Category</legend>" in group
    assert group.count('type="radio"') == 2
    assert group.index('value="business"') < group.index('value="personal"')
    assert 'id="review-business" name="category" value="business"' in group
    assert 'id="review-personal" name="category" value="personal"' in group
    assert 'value="unclassified"' not in group
    assert ">Unset<" not in body
    assert "checked" not in group
    assert "hx-post" not in group
    assert "hx-trigger" not in group
    assert "Not one of my vehicles" in body
    assert "My vehicle, someone else drove" in body


def test_full_category_control_is_untouched_on_trip_detail_and_edit():
    # Both surfaces have to clear an existing category, so they keep all
    # three choices; only Review opts into the pair.
    detail = _render_trip_detail(_row_trip(source="detected", category="personal"))
    edit = _render_trip_edit()

    for body in (detail, edit):
        group = body.split('<fieldset class="category-segmented">', 1)[1].split(
            "</fieldset>", 1
        )[0]
        assert group.count('type="radio"') == 3
        assert group.index('value="personal"') < group.index('value="unclassified"')
        assert group.index('value="unclassified"') < group.index('value="business"')
        assert ">Unset<" in group
        assert "category-segmented-pair" not in body


def test_review_fields_follow_detail_hierarchy_inside_atomic_form():
    # Category left the field grid for the action region it drives, so the
    # grid now carries only the four editable fields.
    body = _render(trip=_trip("manual"))
    form = body.split('<form id="review-form"', 1)[1].split("</form>", 1)[0]
    fields = form.split('<div class="review-detail-fields">', 1)[1].split(
        '<div class="review-action-region">', 1
    )[0]

    vehicle = fields.index('name="vehicle_id"')
    exclusion = fields.index('name="exclusion"')
    purpose = fields.index('<span class="field-label">Purpose</span>')
    notes = fields.index('<span class="field-label">Notes</span>')

    assert "category-segmented" not in fields
    assert vehicle < exclusion < purpose < notes
    assert form.index('id="review-next"') > notes
    assert form.index('id="review-skip"') > notes
    assert form.index('id="review-undo"') > notes
    assert body.index('id="review-undo"') < body.index(
        '<div class="review-card-actions">'
    )
    assert body.index('<div class="review-card-actions">') < body.index(
        'href="/trips/42"'
    ) < body.index('data-trip-delete-open="trip-delete-review-42"')


def test_review_actions_are_next_skip_undo_and_skip_excludes_draft_category():
    body = _render(trip=_trip("manual"))
    form = body.split('<form id="review-form"', 1)[1].split("</form>", 1)[0]
    actions = form.split('<div class="review-actions">', 1)[1].split("</div>", 1)[0]

    next_button = actions.index('id="review-next"')
    skip_button = actions.index('id="review-skip"')
    undo_button = actions.index('id="review-undo"')

    assert next_button < skip_button < undo_button
    # Next carries the shared primary-control classes plus the layout hook
    # that gives it visual weight as the primary action; Skip and Undo stay
    # on the plain secondary control class.
    next_button_markup = actions[next_button:skip_button]
    skip_button_markup = actions[skip_button:undo_button]
    undo_button_markup = actions[undo_button:]
    assert 'class="control control-primary review-primary-action"' in next_button_markup
    assert 'class="control control-secondary"' in skip_button_markup
    assert 'class="control control-secondary"' in undo_button_markup
    assert 'hx-post="/review/42/tag"' in actions
    assert "disabled>Next</button>" in actions
    assert 'hx-post="/review/42/skip" hx-params="not category">Skip</button>' in actions
    assert "disabled>Undo Last</button>" in actions
    assert "Skip (s)" not in body
    assert "Undo (z)" not in body


def test_action_region_groups_category_with_every_advancing_control():
    # One region, inside the one advancing form: Next and Skip still submit
    # the visible fields without a second copy at the narrow breakpoint.
    body = _render(trip=_trip("manual"))
    form = body.split('<form id="review-form"', 1)[1].split("</form>", 1)[0]
    region = form.split('<div class="review-action-region">', 1)[1]

    assert '<fieldset class="category-segmented category-segmented-pair">' in region
    assert 'id="review-next"' in region
    assert 'id="review-skip"' in region
    assert 'id="review-undo"' in region
    assert region.index("category-segmented-pair") < region.index('id="review-next"')


def test_review_page_renders_one_set_of_controls_with_unique_ids():
    body = _render_page(trip=_trip("manual"), remaining=1)
    ids = re.findall(r'\bid="([^"]+)"', body)

    assert [value for value, count in Counter(ids).items() if count > 1] == []
    for control in (
        "review-form", "review-next", "review-skip", "review-undo",
        "review-exclusion", "review-business", "review-personal",
    ):
        assert ids.count(control) == 1


def test_review_field_row_becomes_a_two_column_grid_at_narrow_widths():
    # Purpose, vehicle, exclusion, and notes are direct children of
    # .review-detail-fields (no wrapper div), placed purely by its
    # grid-area rules -- so the narrow-width layout below lives entirely on
    # .review-detail-fields itself.
    stylesheet = (ROOT / "static/style.css").read_text()
    # Review has its own dedicated "max-width: 760px" block, appended after
    # the shared dashboard/trip-detail one earlier in the file. rsplit's
    # last match lands on Review's block; a plain split would instead grab
    # that earlier, unrelated block.
    narrow = stylesheet.rsplit("@media (max-width: 760px) {", 1)[1].split(
        "@media (max-width: 420px) {", 1
    )[0]

    assert ".review-detail-fields > .review-detail-field .purpose-field," in stylesheet
    assert ".review-detail-fields > .review-detail-field .purpose-field input { width: 100%; }" in stylesheet

    detail_fields_rule = _css_rule(narrow, ".review-detail-fields")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in detail_fields_rule
    # Category is placed by the action region now, not by this grid. Purpose
    # and Notes each span the full row; Vehicle and Exclusion share one.
    assert (
        'grid-template-areas: "purpose purpose" "vehicle exclusion" "notes notes";'
        in detail_fields_rule
    )
    assert "gap: var(--space-2);" in detail_fields_rule

    # An #review-next id rule here would out-specificity .review-primary-action
    # and silently break the action layout below.
    assert ".review-actions #review-next" not in stylesheet


def test_narrow_action_region_stays_in_flow_in_the_card():
    stylesheet = (ROOT / "static/style.css").read_text()
    narrow = stylesheet.rsplit("@media (max-width: 760px) {", 1)[1].split(
        "@media (max-width: 420px) {", 1
    )[0]
    region = _css_rule(narrow, ".review-action-region")

    # A fixed tray over the bottom navigation failed real-device QA. The
    # action region now renders in the card at every width, so nothing here
    # takes it out of normal flow.
    assert "position: fixed;" not in region
    assert "position:" not in region
    assert "bottom:" not in region
    assert "z-index" not in region
    # No reserved scroll space, because nothing floats over the card content
    # to reserve room for.
    assert ".review-page { padding-bottom: calc(" not in narrow
    # No un-pinning script left to toggle, because there is nothing fixed to
    # un-pin.
    assert ".review-action-region.is-unpinned" not in narrow
    # display: contents dissolves the element's box, which would take the
    # region's own background and padding with it (and is guarded against
    # stylesheet-wide elsewhere).
    assert "display: contents" not in stylesheet


def test_narrow_actions_place_skip_and_undo_beside_next_on_one_row():
    # Skip, Undo Last, and Next used to occupy two stacked rows (Skip above
    # Undo, opposite a full-height Next); they still share one row here,
    # independent of the action region now living in normal flow rather
    # than a fixed tray, so no rule here may still span two grid rows.
    stylesheet = (ROOT / "static/style.css").read_text()
    narrow = stylesheet.rsplit("@media (max-width: 760px) {", 1)[1].split(
        "@media (max-width: 420px) {", 1
    )[0]

    actions_rule = _css_rule(narrow, ".review-actions")
    assert "grid-template-columns: 2fr 1fr;" in actions_rule
    assert "gap: var(--space-2);" in actions_rule

    secondary_rule = _css_rule(narrow, ".review-secondary-actions")
    assert "grid-column: 1;" in secondary_rule
    assert "grid-row: 1;" in secondary_rule
    assert "flex-direction: row;" in secondary_rule
    assert "flex-wrap: nowrap;" in secondary_rule
    assert "span" not in secondary_rule

    secondary_children_rule = _css_rule(narrow, ".review-secondary-actions > *")
    assert "flex: 1 1 0;" in secondary_children_rule
    assert "min-width: 0;" in secondary_children_rule

    primary_rule = _css_rule(narrow, ".review-primary-action")
    assert "grid-column: 2;" in primary_rule
    assert "grid-row: 1;" in primary_rule
    assert "width: auto;" in primary_rule
    assert "min-width: 0;" in primary_rule
    assert "span" not in primary_rule


def test_two_option_category_pair_keeps_the_shared_joined_control_geometry():
    stylesheet = (ROOT / "static/style.css").read_text()
    pair = stylesheet.split(".category-segmented-pair .category-segmented-options {", 1)[1].split(
        "}", 1
    )[0]

    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in pair
    # The joined edges and the collapsed inner border are shared rules that
    # hold at either option count, so the variant must not restate them.
    assert ".category-segmented-option:first-of-type" in stylesheet
    assert ".category-segmented-option:last-of-type" in stylesheet
    assert (
        ".category-segmented-option + .category-segmented-input + .category-segmented-option"
        in stylesheet
    )


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
    # search term, so Next and Skip round-trip it too.
    assert 'name="q" value="zephyr"' in detected


def test_review_has_no_custom_keyboard_shortcuts_or_hints():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "document.addEventListener('keydown'" not in body
    assert "e.key ===" not in body
    assert "Keyboard shortcuts" not in body
    assert "Skip (s)" not in body
    assert "Undo (z)" not in body


def test_review_next_tracks_draft_category_and_rechecks_after_card_swaps():
    body = _render_page(trip=_trip("manual"), remaining=1)

    # The enablement rule itself is proven by tests/js against
    # static/review_state.js; this guards the seam between it and the page.
    assert '<script src="/static/review_state.js"></script>' in body
    assert "var reviewState = window.ReviewState;" in body
    assert "function updateReviewNextButton()" in body
    assert 'form.querySelector(\'input[name="category"]:checked\')' in body
    assert "button.disabled = !reviewState.canAdvance(selected ? selected.value : null);" in body
    assert "e.target.matches('#review-form input[name=\"category\"]')" in body
    assert "updateReviewNextButton();" in body
    assert "document.body.addEventListener('htmx:afterSettle', updateReviewNextButton);" in body


def test_reactivating_the_committed_category_clears_it_without_key_handling():
    body = _render_page(trip=_trip("manual"), remaining=1)

    # A radio cannot be unchecked by re-activation, so the committed value is
    # tracked and compared in the click path, and re-derived from whatever
    # card is on screen after every swap.
    assert "var committedReviewCategory = null;" in body
    assert "function syncReviewCategorySelection()" in body
    assert (
        "reviewState.categoryAfterActivation(committedReviewCategory, e.target.value) !== null"
        in body
    )
    assert "e.target.checked = false;" in body
    assert (
        "document.body.addEventListener('htmx:afterSettle', syncReviewCategorySelection);"
        in body
    )
    assert "document.addEventListener('keydown'" not in body


def test_no_unpinning_script_remains_for_the_action_region():
    body = _render_page(trip=_trip("manual"), remaining=1)

    # The action region is never fixed, so there is nothing left to un-stick
    # while a text field has focus.
    assert "isReviewTextEntry" not in body
    assert "setReviewTrayUnpinned" not in body
    assert "is-unpinned" not in body
    assert "document.addEventListener('focusin'" not in body
    assert "document.addEventListener('focusout'" not in body


def test_undo_with_nothing_remembered_is_a_client_side_no_op():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "function triggerReviewUndo()" in body
    assert "if (!lastReviewAction || undoRequestInFlight) return;" in body
    # A failed request preserves the action; successful undo clears it only
    # when htmx reports success.
    assert "if (e.detail.successful) lastReviewAction = null;" in body


def test_review_writes_replay_from_live_sources_in_one_lane():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "reviewState.createWriteCoordinator()" in body
    assert "reviewWriteCoordinator.offer(entry)" in body
    assert "e.preventDefault();" in body
    assert "htmx.ajax('POST', next.path, options);" in body
    assert "detail.requestConfig && detail.requestConfig.parameters" in body
    assert "if (e.defaultPrevented) return;" in body
    assert "function releaseReviewWrite(entry)" in body


def test_undo_reads_kind_and_filters_from_the_acting_button_not_the_post_swap_dom():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "elt.id === 'review-skip'" in body
    assert "elt.id === 'review-next'" in body
    assert "elt.id === 'review-business'" not in body
    assert "elt.id === 'review-personal'" not in body
    assert "elt.closest('form')" in body
    assert "form.elements.from.value" in body
    assert "form.elements.to.value" in body
    assert "form.elements.vehicle.value" in body
    assert "form.elements.q.value" in body
    assert "review-not-my-vehicle" not in body
    assert "review-not-deductible" not in body
    assert "(?:tag|skip|exclusion)" not in body


def test_undo_request_targets_review_card_with_outer_html_swap():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert "htmx.ajax('POST', '/review/' + action.tripId + '/undo'" in body
    assert "target: '#review-card'" in body
    assert "swap: 'outerHTML'" in body
    assert "kind: action.kind" in body


def test_category_segments_remain_equal_and_show_non_color_selected_state():
    stylesheet = (ROOT / "static/style.css").read_text()

    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in stylesheet
    assert "min-height: var(--control-height)" in stylesheet
    assert ".category-segmented-personal" in stylesheet
    assert ".category-segmented-unclassified" in stylesheet
    assert ".category-segmented-business" in stylesheet
    checked = stylesheet.split(
        ".category-segmented-input:checked + .category-segmented-option {", 1
    )[1].split("}", 1)[0]
    assert "border-width: 2px" in checked
    assert "font-weight: 700" in checked
    assert "box-shadow: inset" in checked
    assert ".category-segmented-input:focus-visible + .category-segmented-option" in stylesheet


def test_done_state_renders_start_over_link_with_filters():
    body = _render(trip=None, state="done", remaining=0, review_url="/review?vehicle=3")
    assert "No more unclassified trips in this pass" in body
    assert 'href="/review?vehicle=3"' in body
    assert "review-map" not in body
    assert "disabled>Undo Last</button>" in body


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


def test_review_page_header_has_eyebrow_heading_subtitle_and_close_link():
    body = _render_page(trip=_trip("manual"), remaining=1)

    assert '<section class="review-page" aria-labelledby="review-page-title">' in body
    assert '<p class="page-title-eyebrow">Review</p>' in body
    assert '<h2 id="review-page-title" class="page-title-heading">Unclassified trips</h2>' in body
    assert 'class="page-title-subtitle">Classify each trip' in body
    assert '<a class="icon-button review-close" href="/trips" aria-label="Close review" title="Close review">' in body


def test_review_remaining_count_is_a_live_status_region():
    body = _render(trip=_trip("manual"), remaining=5)

    assert '<p class="review-remaining" role="status" aria-live="polite">' in body
    assert "<strong>5</strong> remaining" in body


def test_review_card_shows_both_route_endpoints():
    body = _render(trip=_trip("manual"))

    assert '<div class="review-route-endpoints" aria-label="Route endpoints">' in body
    assert body.count('class="review-endpoint"') == 2
    assert 'class="review-endpoint-marker review-endpoint-marker-end"' in body
    # No place name or address on this trip, so the endpoint falls back to
    # describe_endpoint's rounded-coordinate label for start and end.
    assert "40.0000,-74.0000" in body
    assert "40.1000,-74.1000" in body


def test_done_state_offers_undo_start_over_and_back_to_trip_list():
    body = _render(trip=None, state="done", remaining=0, review_url="/review")

    assert "disabled>Undo Last</button>" in body
    assert '<a class="control control-secondary" href="/review">Start over</a>' in body
    assert '<a class="control control-quiet" href="/trips">Back to trip list</a>' in body


def test_empty_state_offers_only_back_to_trip_list():
    body = _render(trip=None, state="empty", remaining=0)

    assert '<a class="control control-secondary" href="/trips">Back to trip list</a>' in body
    assert "Undo Last" not in body
    assert "Start over" not in body
