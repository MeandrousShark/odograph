from __future__ import annotations

from datetime import datetime, timezone
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


def _render(trip: dict) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("_trip_card.html").render(
        trip=trip, vehicles=[], recent_purposes=[]
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

    assert 'class="trip-route trip-card-route"' in body
    assert 'title="123 Main St, Seattle, WA 98101 to Home"' in body
    assert 'aria-label="Route: 123 Main St, Seattle, WA 98101 to Home"' in body
    assert "123 Main St to Home" in body


def test_trip_route_tooltip_and_accessible_name_are_html_escaped():
    body = _render(
        _trip(
            start_address='100 "A&B" Ave, Seattle',
            end_place_name='Client "North" & Co',
        )
    )

    assert 'title="100 &#34;A&amp;B&#34; Ave, Seattle to Client &#34;North&#34; &amp; Co"' in body
    assert 'aria-label="Route: 100 &#34;A&amp;B&#34; Ave, Seattle to Client &#34;North&#34; &amp; Co"' in body
    assert '100 &#34;A&amp;B&#34; Ave to' in body


def test_manual_trip_can_be_selected_for_batch_edit():
    body = _render(_trip(source="manual"))

    assert '<input type="checkbox" class="merge-select" value="42">' in body
    heading = body.split('class="trip-card-heading"')[1].split("</div>\n  <strong", 1)[0]
    assert 'class="trip-card-selector" hidden' in heading
    assert '<span class="selection-label">Select</span>' in heading
    assert '<span class="selected-label">Selected</span>' in heading
    assert '<span class="status-badge manual-badge">Manual</span>' in body


def test_detected_trip_has_geometry_gated_route_link_without_thumbnail():
    body = _render(_trip(source="detected"))

    assert "thumb.svg" not in body
    assert '<a class="control control-secondary trip-card-route-action" href="/trips/42">View route</a>' in body

    without_geometry = _render(_trip(source="detected", has_route_geometry=False))
    assert "View route" not in without_geometry


def test_manual_trip_has_no_thumbnail_or_route_link_without_geometry():
    body = _render(_trip(source="manual", has_route_geometry=False))

    assert "thumb.svg" not in body
    assert "View route" not in body


def test_manual_trip_with_route_geometry_shows_view_route_link():
    # A routed manual trip has a working detail-page map (verified directly
    # against trip.html elsewhere), so the archive card must offer a way to
    # reach it, the same as a detected trip does.
    body = _render(_trip(source="manual", has_route_geometry=True))

    assert '<a class="control control-secondary trip-card-route-action" href="/trips/42">View route</a>' in body


def test_trip_actions_share_desktop_row_and_keep_mobile_details_full_width():
    body = _render(_trip(source="detected"))
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()
    actions = body.split('<div class="trip-card-actions">', 1)[1].split("</article>", 1)[0]

    assert actions.index("<details") < actions.index("View route") < actions.index("Edit</button>")
    assert 'class="control control-secondary trip-card-edit-action"' in actions
    assert "data-trip-delete-open" in actions.split("</details>", 1)[0]
    assert "grid-template-columns: 6rem max-content max-content minmax(0, 1fr)" in stylesheet
    assert "grid-template-rows: var(--control-height) auto" in stylesheet
    assert ".trip-card-route-action { grid-column: 2; }" in stylesheet
    assert ".trip-card-edit-action { grid-column: 3; }" in stylesheet
    assert ".trip-card-details { display: block; flex: 1 0 100%; }" in stylesheet
    assert ".trip-card-details > summary { width: 100%; }" in stylesheet


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


def test_missing_trip_badge_renders_on_flagged_row():
    body = _render(_trip(
        prev_end_gap_m=2000.0,
        prev_trip_ended_at=datetime(2026, 7, 1, 8, 10, tzinfo=TZ),
        prev_trip_end_lat=47.0, prev_trip_end_lon=-122.0,
        missing_trip_covered=False,
    ))

    collapsed = body.split('<details class="trip-card-details">')[0]
    expanded = body.split('<details class="trip-card-details">')[1]
    assert '<span class="status-badge status-warning missing-trip-badge">Possible missing trip</span>' in collapsed
    assert "missing-trip-badge\" href" not in collapsed
    assert "Possible missing trip" in body
    assert 'href="/trips?manual_date=2026-07-01&amp;manual_start=08%3A10' in expanded
    assert "bridge_trip=42" in expanded
    assert "#manual-trip" in expanded
    assert "Add a prefilled manual trip for the missing drive" in expanded
    assert 'class="trip-warning-callout missing-trip-explanation"' in expanded


def test_details_summary_is_text_only_and_expanded_content_is_complete():
    body = _render(_trip(
        display_distance_m=1800, distance_m=1600, purpose="Client planning",
        notes="Long notes", vehicle_name="Retired car", has_gap=True,
        snap_status="low_confidence",
    ))

    assert "<summary>Details</summary>" in body
    for label in ("Start", "End", "Duration", "Distance", "Raw GPS", "Category",
                  "Purpose", "Notes", "Vehicle", "Source", "Points", "Recording gap",
                  "Road snap"):
        assert label in body
    assert "Location updates paused during this trip" in body
    assert "displayed mileage may under-read" in body
    assert "Inspect the route and compare it with your odometer records" in body
    assert "overlapping manual trip" not in body
    assert 'class="trip-warning-callout recording-gap-explanation"' in body


def test_issue_explanations_share_theme_safe_non_color_warning_treatment():
    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert ".trip-warning-callout {" in stylesheet
    assert "border: 1px solid var(--warn)" in stylesheet
    assert "border-left-width: .3rem" in stylesheet
    assert "background: color-mix(in srgb, var(--warn) 6%, var(--surface-elevated))" in stylesheet
    assert "color: var(--danger)" in stylesheet
    assert ".trip-warning-callout a { font-weight: 600; text-decoration: underline; }" in stylesheet


def test_trip_card_has_no_vehicle_status_badge():
    body = _render(_trip(vehicle_id=7, vehicle_name="Truck"))

    badges = body.split('class="trip-card-badges"')[1].split("</div>", 1)[0]
    assert "Truck" not in badges
    assert '<span class="status-badge">' not in badges


def test_trip_card_controls_render_vehicle_select_and_purpose_field():
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("_trip_card.html").render(
        trip=_trip(vehicle_id=2, purpose="Client visit"),
        vehicles=[
            {"id": 1, "name": "Car A", "active": True, "is_default": False},
            {"id": 2, "name": "Car B", "active": True, "is_default": False},
        ],
        recent_purposes=["Client visit", "Errand"],
    )

    controls = body.split('<div class="trip-card-controls">')[1].split("</article>", 1)[0]
    assert 'hx-post="/trips/42/vehicle"' in controls
    assert 'hx-target="#trip-42"' in controls
    assert 'hx-swap="outerHTML"' in controls
    picker = controls.split('name="vehicle_id"')[1].split("</select>")[0]
    assert '<option value="">Not assigned</option>' in picker
    assert '<option value="1" >Car A</option>' in picker
    assert '<option value="2" selected>Car B</option>' in picker
    assert 'hx-post="/trips/42/purpose"' in controls
    assert 'value="Client visit"' in controls


def test_trip_card_shows_inactive_vehicle_fallback_option():
    body = _render(_trip(vehicle_id=99, vehicle_name="Retired car"))

    controls = body.split('<div class="trip-card-controls">')[1].split("</article>", 1)[0]
    picker = controls.split('name="vehicle_id"')[1].split("</select>")[0]
    assert '<option value="99" selected>Retired car (inactive)</option>' in picker


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

    assert body.index('<div id="map">') < body.index('<details class="advanced-tools">')
    assert body.index('<details class="advanced-tools">') < body.index(
        'class="trip-detail-delete"'
    )
    # Quick-edit fields sit between the map and the disclosure.
    assert body.index('<div id="map">') < body.index('name="notes"') < body.index(
        '<details class="advanced-tools">'
    )


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


def test_routed_manual_trip_detail_shows_map_but_no_advanced_tools():
    body = _render_detail(
        _trip(source="manual", has_route_geometry=True), has_prev_trip=False, has_next_trip=False,
    )

    assert '<div id="map">' in body
    assert 'class="advanced-tools"' not in body
    assert 'id="split-toggle"' not in body
    assert 'id="name-start"' not in body


def test_advanced_tools_summary_is_text_only_with_no_decorative_glyph():
    body = _render_detail(_trip(source="detected"))

    assert "<summary>Advanced trip tools</summary>" in body
    summary = body.split("<summary>Advanced trip tools", 1)[1].split("</summary>", 1)[0]
    for glyph in ("＋", "🚗", "✅", "📊", "🧾", "📈", "⚙", "🌙", "☀", "⬇", "🗑", "▾"):
        assert glyph not in summary
