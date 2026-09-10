from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates

TZ = timezone.utc


def _trip(**overrides) -> dict:
    trip = {
        "id": 42,
        "source": "detected",
        "started_at": datetime(2026, 7, 1, 9, 0, tzinfo=TZ),
        "ended_at": datetime(2026, 7, 1, 9, 20, tzinfo=TZ),
        "display_distance_m": 1609.344,
        "distance_m": 1609.344,
        "snap_status": "pending",
        "point_count": 20,
        "has_gap": False,
        "category": "unclassified",
        "exclusion": None,
        "purpose": None,
        "notes": None,
        "vehicle_id": None,
        "vehicle_name": None,
        "has_route_geometry": True,
        "start_place_name": None,
        "start_lat": 47.6,
        "start_lon": -122.3,
        "start_address": "123 Main St, Seattle, WA 98101",
        "end_place_name": "Home",
        "end_lat": 47.7,
        "end_lon": -122.4,
        "end_address": "456 Other Ave, Seattle, WA 98102",
    }
    trip.update(overrides)
    return trip


def _render(trip: dict, vehicles: list[dict] | None = None) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("_trip_archive_row.html").render(
        trip=trip, vehicles=vehicles if vehicles is not None else [], recent_purposes=[]
    )


def _render_detail(trip: dict, **overrides) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    context = {
        "trip": trip, "recent_purposes": [], "vehicles": [],
        "categories": ["unclassified", "business", "personal"],
        "has_prev_trip": True, "has_next_trip": True,
        "path_geojson": None, "path_snapped_geojson": None, "stay_centroids": "[]",
        "min_trip_distance_m": 100, "user": {"name": "Tester"}, "csrf_token": "test",
    }
    context.update(overrides)
    return templates.env.get_template("trip.html").render(**context)


def test_trip_list_shows_compact_route_and_keeps_full_route_tooltip():
    body = _render(_trip())

    assert 'class="trip-archive-row-route trip-route"' in body
    assert 'title="123 Main St, Seattle, WA 98101 to Home"' in body
    assert 'aria-label="Route: 123 Main St, Seattle, WA 98101 to Home"' in body
    # Compact start/end are joined by an arrow icon, not literal " to " text,
    # matching the dashboard row's own route treatment.
    assert "<span>123 Main St</span>" in body and "<span>Home</span>" in body
    assert 'class="icon icon-arrow-right' in body
    assert "123 Main St to Home" not in body


def test_archive_row_shows_a_custom_label_as_the_endpoint_name():
    # A custom label and a saved place can never coexist on one endpoint
    # (migrations/025_manual_trip_labels.sql), so TRIP_COLUMNS already
    # resolves start_place_name/end_place_name to the label ahead of any
    # saved-place name (app/ui/_common.py) before this template ever sees
    # the row -- the row template itself never has to learn a label exists.
    body = _render(_trip(source="manual", start_place_name="Grandma's house"))

    assert 'title="Grandma&#39;s house to Home"' in body
    assert "<span>Grandma&#39;s house</span>" in body


def test_trip_route_tooltip_and_accessible_name_are_html_escaped():
    body = _render(
        _trip(
            start_address='100 "A&B" Ave, Seattle',
            end_place_name='Client "North" & Co',
        )
    )

    assert 'title="100 &#34;A&amp;B&#34; Ave, Seattle to Client &#34;North&#34; &amp; Co"' in body
    assert 'aria-label="Route: 100 &#34;A&amp;B&#34; Ave, Seattle to Client &#34;North&#34; &amp; Co"' in body
    assert "<span>100 &#34;A&amp;B&#34; Ave</span>" in body


def test_manual_trip_can_be_selected_for_batch_edit():
    body = _render(_trip(source="manual"))

    assert '<input type="checkbox" class="merge-select" value="42">' in body
    # Selection has no mode any more, so the checkbox is always present, not
    # toggled visible/hidden by a "Select trips" state; it carries its own
    # accessible name since there's no longer a label to swap between
    # "Select" and "Selected" states.
    selector = body.split('<label class="trip-archive-row-selector"', 1)[1].split("</label>", 1)[0]
    assert "hidden" not in selector.split(">", 1)[0]
    assert '<span class="visually-hidden">Select this trip</span>' in selector
    assert '<span class="status-badge manual-badge">Manual</span>' in body


def test_trip_card_shows_exclusion_badge_without_always_visible_selector():
    body = _render(_trip(exclusion="not_deductible"))

    assert "My vehicle, someone else drove" in body
    assert 'class="status-badge exclusion-badge trip-archive-row-exclusion"' in body
    assert 'hx-post="/trips/42/exclusion"' not in body
    assert 'name="exclusion"' not in body


def test_trip_card_shows_compact_linked_expense_indicator():
    body = _render(_trip(expense_count=2))
    assert '<span class="status-badge expense-badge">2 expenses</span>' in body


def test_trip_detail_collapses_and_pluralizes_expense_warnings():
    expense = {
        "incurred_on": _trip()["started_at"].date(), "category": "fuel",
        "amount": Decimal("12.00"), "treatment": "business_use_allocated",
        "notes": None, "conflicts": ["Vehicle warning", "Date warning"],
    }
    body = _render_detail(
        _trip(), expenses=[expense],
        expense_category_labels={"fuel": "Fuel"},
        expense_treatment_labels={"business_use_allocated": "Business-use allocated"},
        expense_categories=[], expense_treatments=[],
    )
    clear = _render_detail(
        _trip(), expenses=[{**expense, "conflicts": []}],
        expense_category_labels={"fuel": "Fuel"},
        expense_treatment_labels={"business_use_allocated": "Business-use allocated"},
        expense_categories=[], expense_treatments=[],
    )

    assert "<summary>2 warnings</summary>" in body
    assert "Vehicle warning" in body and "Date warning" in body
    assert '<details class="expense-warning-details">' not in clear


def test_trip_detail_expense_disclosure_renders_closed():
    body = _render_detail(_trip())

    assert '<details class="add-manual">' in body
    assert '<details class="add-manual" open>' not in body


def test_purpose_recent_select_clips_its_options():
    # Safari on iOS lays out this control's option text even though it renders
    # as a native picker, so a long recent purpose escaped the 1.75rem box and
    # widened the document until the whole page scrolled horizontally.
    css = (Path(__file__).parents[1] / "static/style.css").read_text()

    rule = css.split(".purpose-recent {", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" in rule


def test_purpose_recent_meets_the_touch_target_size():
    # 1.75rem is 28px, well under the 44px --control-height every other control
    # in this stylesheet uses. Widened at the touch breakpoint only.
    css = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert ".purpose-recent { width: 2.75rem; }" in css
    assert ".purpose-field input { padding-right: 3.25rem; }" in css


def test_detected_trip_detail_link_is_single_regardless_of_geometry():
    # The whole row is now a stretched link to the detail page (below), which
    # already covers the route view for any trip with geometry, so the
    # overflow menu must not regrow its own "View details" item pointing at
    # the same /trips/{id} destination.
    body = _render(_trip(source="detected"))

    assert "thumb.svg" not in body
    panel = body.split('class="trip-archive-row-more-panel"', 1)[1].split("</details>", 1)[0]
    assert "View details" not in panel
    assert "View route" not in panel
    assert panel.count('href="/trips/42"') == 0
    assert body.count('href="/trips/42"') == 1

    without_geometry = _render(_trip(source="detected", has_route_geometry=False))
    without_geometry_panel = without_geometry.split(
        'class="trip-archive-row-more-panel"', 1
    )[1].split("</details>", 1)[0]
    assert "View route" not in without_geometry_panel
    assert without_geometry_panel.count('href="/trips/42"') == 0
    assert without_geometry.count('href="/trips/42"') == 1


def test_manual_trip_has_no_thumbnail_and_no_duplicate_details_link():
    body = _render(_trip(source="manual", has_route_geometry=False))

    assert "thumb.svg" not in body
    assert "View route" not in body
    assert body.count('href="/trips/42"') == 1


def test_manual_trip_with_route_geometry_keeps_a_single_details_link():
    # A routed manual trip has a working detail-page map (verified directly
    # against trip.html elsewhere), so the archive row must offer a way to
    # reach it, the same as a detected trip does, without duplicating the
    # link.
    body = _render(_trip(source="manual", has_route_geometry=True))

    panel = body.split('class="trip-archive-row-more-panel"', 1)[1].split("</details>", 1)[0]
    assert "View route" not in panel
    assert panel.count('href="/trips/42"') == 0
    assert body.count('href="/trips/42"') == 1


def test_trip_row_action_cluster_has_edit_shortcut_and_full_more_menu():
    body = _render(_trip(source="detected"))
    row = body.split('<article id="trip-42"', 1)[1].split("</article>", 1)[0]
    actions = row.split('class="trip-archive-row-actions"', 1)[1]
    shortcut = actions.split("<details", 1)[0]

    assert 'class="trip-archive-row-icon-button"' in shortcut
    assert 'aria-label="Edit trip"' in shortcut
    assert 'hx-get="/trips/42/edit"' in shortcut
    assert 'hx-target="#trip-42"' in shortcut and 'hx-swap="outerHTML"' in shortcut
    # The overflow menu no longer carries its own "View details" item: the
    # whole row is now a stretched link to the same destination (covered by
    # test_row_click_reaches_detail_page_through_a_stretched_overlay_link
    # below), so a second link inside the menu would be a duplicate keyboard
    # path.
    assert "View details" not in actions
    assert "View route" not in actions
    assert "data-trip-delete-open" in actions


def test_review_card_keeps_full_addresses_outside_compact_trip_list():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("_review_card.html").render(
        trip=_trip(end_place_name=None), remaining=0, state="card", path_geojson=None,
        path_snapped_geojson=None, vehicles=[], filter_from="", filter_to="",
        filter_vehicle="", review_url="/review", recent_purposes=[],
    )

    assert "123 Main St, Seattle, WA 98101" in body
    assert "456 Other Ave, Seattle, WA 98102" in body


def test_trip_detail_keeps_full_addresses_outside_compact_trip_list():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trip.html").render(
        trip=_trip(end_place_name=None), recent_purposes=[], vehicles=[],
        categories=[], has_prev_trip=False, has_next_trip=False,
        path_geojson=None, path_snapped_geojson=None, stay_centroids="[]",
        min_trip_distance_m=100, user={"name": "Tester"}, csrf_token="test",
    )

    assert "123 Main St, Seattle, WA 98101" in body
    assert "456 Other Ave, Seattle, WA 98102" in body


def test_review_card_shows_a_custom_label_as_the_endpoint_name():
    # Same reasoning as test_archive_row_shows_a_custom_label_as_the_endpoint_name:
    # the resolved name in start_place_name is all this template ever sees.
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("_review_card.html").render(
        trip=_trip(source="manual", start_place_name="Grandma's house"),
        remaining=0, state="card", path_geojson=None,
        path_snapped_geojson=None, vehicles=[], filter_from="", filter_to="",
        filter_vehicle="", review_url="/review", recent_purposes=[],
    )

    assert "Grandma&#39;s house" in body
    assert "123 Main St, Seattle, WA 98101" not in body


def test_trip_detail_shows_a_custom_label_as_the_endpoint_name():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trip.html").render(
        trip=_trip(source="manual", start_place_name="Grandma's house"),
        recent_purposes=[], vehicles=[],
        categories=[], has_prev_trip=False, has_next_trip=False,
        path_geojson=None, path_snapped_geojson=None, stay_centroids="[]",
        min_trip_distance_m=100, user={"name": "Tester"}, csrf_token="test",
    )

    assert "Grandma&#39;s house" in body
    assert "123 Main St, Seattle, WA 98101" not in body


def test_missing_trip_badge_renders_on_flagged_row():
    body = _render(_trip(
        prev_end_gap_m=2000.0,
        prev_trip_ended_at=datetime(2026, 7, 1, 8, 10, tzinfo=TZ),
        prev_trip_end_lat=47.0, prev_trip_end_lon=-122.0,
        missing_trip_covered=False,
    ))

    assert '<span class="status-badge status-warning missing-trip-badge">Possible missing trip</span>' in body
    # trip.html deliberately never renders this badge or its bridge-prefill
    # link (app/missing_trip.py keeps the flag list-page only), so the row's
    # overflow menu is the only remaining way to reach the prefill link once
    # the row itself no longer expands an inline details panel.
    panel = body.split('class="trip-archive-row-more-panel"', 1)[1].split("</details>", 1)[0]
    assert "Add missing trip" in panel
    prefill_link = panel.split("Add missing trip", 1)[0].rsplit("<a", 1)[1]
    assert 'href="/trips/manual?manual_date=2026-07-01&amp;manual_start=08%3A10' in prefill_link
    assert "bridge_trip=42" in prefill_link


def test_row_click_reaches_detail_page_through_a_stretched_overlay_link():
    # The row no longer expands an inline details panel (that information --
    # duration, notes, points, road snap, ... -- lives on trip.html, which
    # this stretched overlay link reaches); the row itself only needs to
    # keep the plain status signals dashboard rows already show, like the
    # recording-gap badge.
    body = _render(_trip(
        display_distance_m=1800, distance_m=1600, purpose="Client planning",
        notes="Long notes", vehicle_name="Retired car", has_gap=True,
        snap_status="low_confidence",
    ))

    # The overlay link is the row's single keyboard tab stop to the detail
    # page: a stretched <a> covering the row, with only an accessible name,
    # placed ahead of every interactive control so it's the first thing a
    # keyboard user reaches.
    row = body.split('<article id="trip-42"', 1)[1].split("</article>", 1)[0]
    overlay = row.split('<a class="trip-archive-row-link"', 1)[1].split("</a>", 1)[0]
    assert 'href="/trips/42"' in row.split('<a class="trip-archive-row-link"', 1)[1].split(">", 1)[0]
    assert '<span class="visually-hidden">View trip details</span>' in overlay
    assert row.index('trip-archive-row-link') < row.index('trip-archive-row-selector')

    # No second link to the same destination remains in the overflow menu.
    panel = body.split('class="trip-archive-row-more-panel"', 1)[1].split("</details>", 1)[0]
    assert "View details" not in panel
    assert panel.count('href="/trips/42"') == 0
    assert body.count('href="/trips/42"') == 1

    assert '<span class="status-badge status-warning">Recording gap</span>' in body
    assert "Location updates paused during this trip" not in body
    assert "trip-warning-callout" not in body


def test_issue_explanations_share_theme_safe_non_color_warning_treatment():
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert ".trip-warning-callout {" in stylesheet
    assert "border: 1px solid var(--warn)" in stylesheet
    assert "border-left-width: .3rem" in stylesheet
    assert "background: color-mix(in srgb, var(--warn) 6%, var(--surface-elevated))" in stylesheet
    assert "color: var(--danger)" in stylesheet
    assert ".trip-warning-callout a { font-weight: 600; text-decoration: underline; }" in stylesheet


def test_trip_card_has_no_vehicle_status_badge():
    body = _render(_trip(vehicle_id=7, vehicle_name="Truck", has_gap=True))

    statuses = body.split('class="trip-archive-row-statuses"')[1].split("</div>", 1)[0]
    assert "Truck" not in statuses
    assert '<span class="status-badge">' not in statuses


def test_standalone_trip_card_does_not_render_dashboard_category_controls():
    body = _render(_trip())

    assert "dashboard-category-form" not in body
    assert "dashboard_week" not in body


VEHICLES = [
    {"id": 1, "name": "Car A", "active": True, "is_default": False},
    {"id": 2, "name": "Car B", "active": True, "is_default": True},
]


def test_named_vehicle_shows_inline():
    body = _render(_trip(vehicle_id=1, vehicle_name="Car A"), vehicles=VEHICLES)

    meta = body.split('class="trip-archive-row-meta"', 1)[1].split("</div>", 1)[0]
    assert "Vehicle: Car A" in meta


def test_unassigned_vehicle_shows_not_assigned_inline():
    body = _render(_trip(vehicle_id=None, vehicle_name=None), vehicles=VEHICLES)

    meta = body.split('class="trip-archive-row-meta"', 1)[1].split("</div>", 1)[0]
    assert "Vehicle: Not assigned" in meta


def test_default_vehicle_is_hidden_inline_but_stays_accessible():
    # Matches the dashboard row's own default-vehicle handling: the common
    # case (the one vehicle most trips use) doesn't need to repeat its name
    # on every row, but the value must still reach screen readers and
    # find-in-page.
    body = _render(_trip(vehicle_id=2, vehicle_name="Car B"), vehicles=VEHICLES)

    # No purpose and the vehicle is the default, so there's nothing left for
    # the inline meta row to show at all.
    assert "trip-archive-row-meta" not in body
    assert '<span class="trip-archive-row-vehicle visually-hidden">Vehicle: Car B</span>' in body


def test_inactive_vehicle_still_renders_its_assigned_name():
    # TRIP_COLUMNS carries the name of a deactivated vehicle already assigned
    # to a trip (app/ui/trips.py's _fetch_trip_card_context docstring), and
    # it's absent from `vehicles` (the active-only picker list), so it must
    # never be mistaken for the default and hidden.
    body = _render(_trip(vehicle_id=99, vehicle_name="Retired car"), vehicles=VEHICLES)

    meta = body.split('class="trip-archive-row-meta"', 1)[1].split("</div>", 1)[0]
    assert "Vehicle: Retired car" in meta


def test_unclassified_row_shows_neither_half_selected():
    body = _render(_trip(category="unclassified"))

    quick = body.split('class="trip-quick-actions"', 1)[1].split("</div>", 1)[0]
    personal_button = quick.split('value="personal"', 1)[1].split("</form>", 1)[0]
    business_button = quick.split('value="business"', 1)[1].split("</form>", 1)[0]
    assert "trip-quick-button-selected" not in personal_button
    assert "trip-quick-button-selected" not in business_button
    assert "aria-pressed" not in quick
    # No per-row "Classify"/"Category" word any more; an unclassified trip
    # is flagged with a "Needs category" chip in the status row instead.
    assert "trip-archive-row-classify-label" not in body
    assert '<span class="status-badge badge-accent">Needs category</span>' in body
    panel = body.split('class="trip-archive-row-more-panel"', 1)[1].split("</details>", 1)[0]
    assert "Clear category" not in panel


def test_committed_row_shows_one_half_selected_and_offers_clear_category():
    body = _render(_trip(category="business"))

    quick = body.split('class="trip-quick-actions"', 1)[1].split("</div>", 1)[0]
    business_button = quick.split('value="business"', 1)[1].split("</form>", 1)[0]
    personal_button = quick.split('value="personal"', 1)[1].split("</form>", 1)[0]
    assert 'class="trip-quick-button category-business trip-quick-button-selected"' in business_button
    assert 'aria-pressed="true"' in business_button
    assert "trip-quick-button-selected" not in personal_button
    assert "aria-pressed" not in personal_button
    # Committed rows drop the "Category" label entirely: the self-naming
    # Business/Personal buttons plus aria-pressed already say which category
    # is set, and the decorative desktop column header already says
    # "Category" once.
    assert "Category" not in body.split('<article id="trip-42"', 1)[1].split(
        "trip-quick-actions", 1
    )[0]
    # The "Needs category" chip is only for unclassified trips; a committed
    # row is already unambiguous from the selected Business/Personal button.
    assert "Needs category" not in body

    panel = body.split('class="trip-archive-row-more-panel"', 1)[1].split("</details>", 1)[0]
    assert "Clear category" in panel
    clear_button = panel.split("Clear category", 1)[0].rsplit("<button", 1)[1]
    assert 'hx-post="/trips/42/tag"' in clear_button
    assert '"category": "unclassified"' in clear_button
    assert 'hx-target="#trip-42"' in clear_button
    assert 'hx-swap="outerHTML"' in clear_button


def test_category_cell_has_no_label_word_in_either_state():
    # Neither state renders a classify-label element any more (the
    # unclassified case moved that word to the "Needs category" status chip
    # instead), so the category cell holds only the Business/Personal pair
    # and starts flush-left the same way regardless of category.
    unclassified = _render(_trip(category="unclassified"))
    committed = _render(_trip(category="business"))

    unclassified_cell = unclassified.split('class="trip-archive-row-category"', 1)[1].split(
        'class="trip-archive-row-distance"', 1
    )[0]
    committed_cell = committed.split('class="trip-archive-row-category"', 1)[1].split(
        'class="trip-archive-row-distance"', 1
    )[0]
    assert 'class="trip-archive-row-classify-label"' not in unclassified_cell
    assert 'class="trip-archive-row-classify-label"' not in committed_cell
    assert 'class="trip-quick-actions"' in unclassified_cell
    assert 'class="trip-quick-actions"' in committed_cell


def test_not_my_vehicle_row_stays_visible_muted_badged_and_classifiable():
    body = _render(_trip(exclusion="not_my_vehicle", category="unclassified"))

    assert 'class="trip-archive-item trip-archive-row trip-archive-row-not-my-vehicle"' in body
    assert '<span class="status-badge exclusion-badge trip-archive-row-exclusion">Not one of my vehicles</span>' in body
    quick = body.split('class="trip-quick-actions"', 1)[1].split("</div>", 1)[0]
    assert 'hx-post="/trips/42/tag"' in quick
    # An excluded trip is out of deductions, so its category doesn't matter;
    # the chip would just be noise here.
    assert "Needs category" not in body


def test_excluded_unclassified_trip_hides_needs_category_chip():
    # Same rationale as the not_my_vehicle case above, covering the other
    # exclusion value: any exclusion suppresses the chip, not just one kind.
    body = _render(_trip(exclusion="not_deductible", category="unclassified"))

    assert "Needs category" not in body
    assert 'class="trip-quick-actions"' in body


def test_row_selection_hooks_match_what_trips_html_script_binds_to():
    # trips.html's selection script queries these exact class names
    # (updateSelectionShell/selectionSelectAll); if the row silently drops
    # one, Select all and the batch dialogs all break with no template test
    # failing to catch it. The checkbox is always present now (no selection
    # mode to gate it behind a hidden attribute).
    body = _render(_trip(source="manual"))

    assert 'id="trip-42"' in body
    assert "trip-archive-item" in body.split(">", 1)[0]
    assert '<label class="trip-archive-row-selector">' in body
    assert '<input type="checkbox" class="merge-select" value="42">' in body


def test_row_is_one_source_of_markup_with_no_duplicate_ids():
    body = _render(_trip())

    assert body.count('id="trip-42"') == 1
    assert "<table" not in body
    assert "display: contents" not in body


def test_missing_trip_badge_absent_without_predecessor_gap():
    body = _render(_trip())  # no prev_end_gap_m key at all

    assert "missing-trip-badge" not in body


def test_missing_trip_badge_absent_when_covered():
    body = _render(_trip(
        prev_end_gap_m=2000.0,
        prev_trip_ended_at=datetime(2026, 7, 1, 8, 10, tzinfo=TZ),
        prev_trip_end_lat=47.0, prev_trip_end_lon=-122.0,
        missing_trip_covered=True,
    ))

    assert "missing-trip-badge" not in body


def test_merge_forms_include_vehicle_picker_defaulted_to_keep():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("trip.html").render(
        trip=_trip(), recent_purposes=[],
        vehicles=[
            {"id": 1, "name": "Car A", "active": True, "is_default": False},
            {"id": 2, "name": "Car B", "active": True, "is_default": True},
        ],
        categories=["unclassified", "business", "personal"],
        has_prev_trip=True, has_next_trip=True,
        path_geojson=None, path_snapped_geojson=None, stay_centroids="[]",
        min_trip_distance_m=100, user={"name": "Tester"}, csrf_token="test",
    )

    merge_prev_form = body.split('id="merge-prev"')[1].split("</details>")[0]
    merge_next_form = body.split('id="merge-next"')[1].split("</details>")[0]
    for form in (merge_prev_form, merge_next_form):
        picker = form.split('name="vehicle_id"')[1].split("</select>")[0]
        assert '<option value="keep" selected>Keep</option>' in picker
        assert '<option value="">--</option>' in picker
        assert '<option value="1">Car A</option>' in picker
        assert '<option value="2">Car B</option>' in picker


def test_detected_trip_detail_shows_map_before_advanced_tools_disclosure():
    body = _render_detail(_trip(source="detected"))

    assert body.index('<div id="map" class="trip-detail-map">') < body.index(
        '<details class="advanced-tools">'
    )
    assert body.index('<details class="advanced-tools">') < body.index(
        'class="trip-detail-delete"'
    )
    # Every quick-edit field sits between the map and the action row.
    assert body.index('<div id="map" class="trip-detail-map">') < body.index('name="notes"') < body.index(
        '<details class="advanced-tools">'
    )


def test_trip_detail_summary_keeps_source_route_and_map_hooks_scoped():
    body = _render_detail(_trip(source="detected", snap_status="low_confidence", has_gap=True))
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert 'class="trip-detail-page"' in body
    assert 'class="trip-detail-overview"' in body
    assert "Detected trip" in body
    assert "Recording gap" in body
    assert "Road-snapped, low confidence" in body
    assert "<strong>1.0</strong>" in body and "(1.6 km)" in body
    assert "09:00" in body and "20m" in body
    assert '<span class="visually-hidden">Start: </span>' in body
    assert '<span class="visually-hidden">End: </span>' in body
    assert '<div id="map" class="trip-detail-map">' in body
    assert "<style>#map" not in body
    assert ".trip-detail-page .trip-detail-map" in stylesheet
    assert ".trip-detail-overview-no-map" in stylesheet
    assert "height: 480px" not in stylesheet


def test_trip_detail_label_editor_sits_between_metadata_and_expenses():
    body = _render_detail(
        _trip(
            source="manual", has_route_geometry=False,
            start_lat=None, start_lon=None, end_lat=None, end_lon=None,
            start_place_id=None, end_place_id=None,
        ),
        has_prev_trip=False, has_next_trip=False,
    )

    fields = body.index('<div class="trip-detail-fields">')
    labels = body.index('id="trip-label-editor-42"')
    expenses = body.index('<section class="trip-expenses"')

    assert fields < labels < expenses
    label_form = body.split('id="trip-label-editor-42"', 1)[1].split("</section>", 1)[0]
    assert 'hx-post="/trips/42/labels"' in label_form
    assert 'hx-target="#trip-label-editor-42"' in label_form
    assert 'maxlength="100"' in label_form
    assert 'class="control control-primary">Save</button>' in label_form


def test_trip_detail_without_map_uses_single_column_overview():
    body = _render_detail(
        _trip(
            source="manual", has_route_geometry=False,
            start_lat=None, start_lon=None, end_lat=None, end_lon=None,
        ),
        has_prev_trip=False, has_next_trip=False,
    )

    overview = body.split('<section class="trip-detail-overview', 1)[1].split(">", 1)[0]
    assert "trip-detail-overview-no-map" in overview
    assert 'id="map"' not in body
    assert "Manual trip" in body


def test_trip_detail_category_group_posts_without_swapping_archive_card():
    body = _render_detail(_trip(source="detected", category="personal"))
    row = body.split('<div class="trip-detail-row">', 1)[1].split(
        '<details class="advanced-tools">', 1
    )[0]
    group = row.split('<fieldset class="category-segmented">', 1)[1].split(
        "</fieldset>", 1
    )[0]

    assert "<legend>Category</legend>" in group
    assert group.count('type="radio"') == 3
    assert group.index('value="personal"') < group.index('value="unclassified"')
    assert group.index('value="unclassified"') < group.index('value="business"')
    assert 'value="personal"\n           checked' in group
    assert group.count('hx-post="/trips/42/tag"') == 3
    assert group.count('hx-swap="none"') == 3
    assert 'name="vehicle_id"' in row
    assert 'name="exclusion"' in row


def test_trip_detail_fields_render_in_approved_order():
    body = _render_detail(_trip(source="detected"))
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()
    fields = body.split('<div class="trip-detail-fields">', 1)[1].split(
        '<div class="trip-detail-actions">', 1
    )[0]

    category = fields.index('<fieldset class="category-segmented">')
    vehicle = fields.index('name="vehicle_id"')
    exclusion = fields.index('name="exclusion"')
    purpose = fields.index('<span class="field-label">Purpose</span>')
    notes = fields.index('<span class="field-label">Notes</span>')

    assert category < vehicle < exclusion < purpose < notes
    assert ".trip-detail-fields > .trip-detail-field .purpose-field," in stylesheet
    assert ".trip-detail-fields > .trip-detail-field .purpose-field input { width: 100%; }" in stylesheet


def test_trip_detail_actions_align_advanced_tools_left_and_delete_right():
    body = _render_detail(_trip(source="detected"))
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()
    actions = body.split('<div class="trip-detail-actions">', 1)[1].split(
        '<script nonce="', 1
    )[0]

    assert actions.index('<details class="advanced-tools">') < actions.index(
        '<div class="trip-detail-delete">'
    )
    assert ".trip-detail-actions {" in stylesheet
    assert "display: flex; gap: var(--space-3); align-items: flex-start" in stylesheet
    assert ".trip-detail-actions > .advanced-tools {" in stylesheet
    assert "flex: 1 1 auto; width: 100%; min-width: 0; margin: 0" in stylesheet
    assert ".trip-detail-delete { flex: 0 0 auto; margin-left: auto; }" in stylesheet


def test_trip_detail_actions_stack_at_narrow_width():
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()
    narrow = stylesheet.split("@media (max-width: 760px) {", 1)[1].split(
        "@media (max-width: 420px) {", 1
    )[0]

    assert ".trip-detail-actions { flex-direction: column; align-items: stretch; }" in narrow
    assert ".trip-detail-delete { width: 100%; margin-left: 0; }" in narrow
    assert ".trip-detail-delete .trip-delete-trigger { width: 100%; }" in narrow
    assert ".trip-detail-actions > .advanced-tools > summary { width: 100%; }" in narrow


def test_advanced_tools_disclosure_contains_merge_split_and_place_naming_controls():
    body = _render_detail(_trip(source="detected", start_place_name=None, end_place_name=None))

    tools = body.split('<details class="advanced-tools">')[1].split(
        '<div class="trip-detail-delete">'
    )[0]

    assert 'id="merge-prev"' in tools
    assert 'id="merge-next"' in tools
    assert 'id="split-toggle"' in tools
    assert 'id="split-confirm"' in tools
    assert 'id="name-start"' in tools
    assert 'id="name-end"' in tools
    assert tools.index('id="merge-prev"') < tools.index('id="merge-next"') < tools.index(
        'id="split-toggle"'
    ) < tools.index('id="name-start"')


def test_imported_detected_trip_detail_has_no_advanced_tools():
    """An imported trip keeps source == 'detected' (it's a fact about the
    source instance) but has no backing points here, so merge and split --
    the reason this disclosure exists -- would always be refused. Gated off
    entirely rather than left to fail per-action."""
    body = _render_detail(
        _trip(source="detected", imported=True), has_prev_trip=True, has_next_trip=True,
    )

    assert 'class="advanced-tools"' not in body
    assert 'id="merge-prev"' not in body
    assert 'id="split-toggle"' not in body


def test_manual_trip_detail_has_no_map_or_advanced_tools():
    body = _render_detail(
        _trip(source="manual", has_route_geometry=False), has_prev_trip=False, has_next_trip=False,
    )

    assert 'id="map"' not in body
    assert 'class="advanced-tools"' not in body
    assert 'id="split-toggle"' not in body
    assert 'id="name-start"' not in body
    assert 'name="notes"' in body
    assert 'class="trip-detail-delete"' in body


def test_unrouted_manual_trip_detail_exposes_escaped_label_editor():
    body = _render_detail(
        _trip(
            source="manual", has_route_geometry=False,
            start_lat=None, start_lon=None, end_lat=None, end_lon=None,
            start_place_id=None, end_place_id=None,
            start_label='Grandma\'s <house>', end_label='Work & office',
        ),
        has_prev_trip=False, has_next_trip=False,
    )

    assert 'id="trip-label-editor-42"' in body
    assert 'hx-post="/trips/42/labels"' in body
    assert 'name="start_label" value="Grandma&#39;s &lt;house&gt;" maxlength="100"' in body
    assert 'name="end_label" value="Work &amp; office" maxlength="100"' in body
    assert '<button type="submit" class="control control-primary">Save</button>' in body


def test_detected_and_routed_manual_detail_hide_label_editor():
    detected = _render_detail(_trip(source="detected"))
    routed_manual = _render_detail(_trip(source="manual", has_route_geometry=True))

    assert 'id="trip-label-editor-42"' not in detected
    assert 'id="trip-label-editor-42"' not in routed_manual


def test_label_editor_error_preserves_typed_value_and_describes_field():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("_trip_label_form.html").render(
        trip=_trip(
            source="manual", has_route_geometry=False,
            start_lat=None, start_lon=None, end_lat=None, end_lon=None,
            start_place_id=None, end_place_id=None,
            start_label="Old", end_label="Office",
        ),
        values={"start_label": "x" * 101, "end_label": "Office"},
        errors={"start_label": "Keep it to 100 characters or fewer."},
    )

    assert 'role="alert"' in body
    assert 'aria-invalid="true"' in body
    assert 'aria-describedby="detail-start-label-42-error"' in body
    assert 'id="detail-start-label-42-error">Keep it to 100 characters or fewer.</span>' in body
    assert 'value="' + ("x" * 101) + '"' in body


def test_routed_manual_trip_detail_shows_map_but_no_advanced_tools():
    body = _render_detail(
        _trip(source="manual", has_route_geometry=True), has_prev_trip=False, has_next_trip=False,
    )

    assert '<div id="map" class="trip-detail-map">' in body
    assert 'class="advanced-tools"' not in body
    assert 'id="split-toggle"' not in body
    assert 'id="name-start"' not in body


def test_advanced_tools_summary_is_text_only_with_no_decorative_glyph():
    body = _render_detail(_trip(source="detected"))

    assert "<summary>Advanced trip tools</summary>" in body
    summary = body.split("<summary>Advanced trip tools", 1)[1].split("</summary>", 1)[0]
    for glyph in ("＋", "🚗", "✅", "📊", "🧾", "📈", "⚙", "🌙", "☀", "⬇", "🗑", "▾"):
        assert glyph not in summary
