from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates
from tests.test_trip_row_template import _render, _trip

TZ = timezone.utc


def _templates():
    return make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))


def _render_detail(source: str, **trip_overrides) -> str:
    return _templates().env.get_template("trip.html").render(
        trip=_trip(source=source, **trip_overrides), recent_purposes=[], vehicles=[], categories=[],
        has_prev_trip=False, has_next_trip=False, path_geojson=None,
        path_snapped_geojson=None, stay_centroids="[]", min_trip_distance_m=100,
        user={"name": "Tester"}, csrf_token="test",
    )


def _delete_component(body: str, dialog_id: str) -> str:
    control = body.index(f'aria-controls="{dialog_id}"')
    start = body.rfind("<button", 0, control)
    end = body.index("</dialog>", control) + len("</dialog>")
    return body[start:end]


def _assert_accessible_delete_component(
    body: str, dialog_id: str, action_url: str, expected_copy: str
) -> None:
    component = _delete_component(body, dialog_id)
    assert 'aria-haspopup="dialog"' in component
    assert 'aria-label="Delete trip"' in component
    assert f'aria-controls="{dialog_id}"' in component
    assert f'<dialog id="{dialog_id}"' in component
    assert f'aria-labelledby="{dialog_id}-title"' in component
    assert f'aria-describedby="{dialog_id}-description"' in component
    assert "hx-confirm" not in component
    assert "Cancel" in component
    assert "data-trip-delete-cancel" in component
    assert "autofocus" in component
    assert f'hx-post="{action_url}"' in component
    assert "Delete trip" in component
    assert expected_copy in component


def test_native_summary_controls_share_the_control_minimum_height():
    # Settings' disclosures are bare <summary> elements with no control class,
    # so they depend entirely on the global rule for their touch target. The
    # centering is part of that contract rather than cosmetic: min-height alone
    # leaves the label at the top of the taller box, because align-content
    # starts rather than centers on a block box.
    body = _templates().env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[],
        rules=[], geocode_enabled=False, user={"name": "Tester"}, csrf_token="test",
    )
    assert "<summary>Add place</summary>" in body
    assert "<summary>Add rule</summary>" in body

    stylesheet = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert "button, input, select, summary { min-height: var(--control-height); }" in stylesheet
    assert "summary { cursor: pointer; align-content: center; }" in stylesheet


def test_trip_rows_offer_in_place_delete_for_detected_and_manual_trips():
    detected = _render(_trip(source="detected"))
    manual = _render(_trip(source="manual"))

    _assert_accessible_delete_component(
        detected, "trip-delete-archive-42", "/trips/42/delete",
        "Stored location data is kept",
    )
    _assert_accessible_delete_component(
        manual, "trip-delete-archive-42", "/trips/42/delete",
        "This permanently deletes the manual trip",
    )
    assert "hx-confirm" not in detected
    assert "hx-confirm" not in manual
    assert 'name="fragment" value="true"' in detected
    assert 'hx-target="#trip-42"' in detected
    assert 'hx-swap="delete"' in detected
    assert "It cannot be restored" in manual
    for body in (detected, manual):
        # The trigger only becomes reachable once the overflow menu itself
        # is open, same as every other overflow action.
        collapsed, expanded = body.split('<details class="trip-archive-row-more">', 1)
        details_body = expanded.split("</details>", 1)[0]
        assert "data-trip-delete-open" not in collapsed
        assert "data-trip-delete-open" in details_body


def test_trip_detail_has_named_delete_action_with_source_appropriate_confirmation():
    detected = _render_detail("detected")
    manual = _render_detail("manual")

    _assert_accessible_delete_component(
        detected, "trip-delete-detail-42", "/trips/42/delete",
        "Stored location data is kept",
    )
    _assert_accessible_delete_component(
        manual, "trip-delete-detail-42", "/trips/42/delete",
        "This permanently deletes the manual trip",
    )


def test_manual_trip_detail_has_no_map_script_and_no_none_literals():
    body = _render_detail("manual", has_route_geometry=False)

    assert 'id="map"' not in body
    assert "L.map(" not in body
    assert "None" not in body
    assert "place-naming" not in body


def test_detected_trip_detail_still_renders_map():
    body = _render_detail("detected")

    assert 'id="map"' in body
    assert "L.map('map')" in body
    assert "place-naming" in body


def test_detected_trip_detail_tile_layer_follows_configured_map_tile_url():
    templates = make_templates(SimpleNamespace(
        display_tz=TZ, map_tile_url="https://tiles.example.net/{z}/{x}/{y}.png",
        map_tile_attribution="Example attribution", app_version="test",
    ))
    body = templates.env.get_template("trip.html").render(
        trip=_trip(source="detected"), recent_purposes=[], vehicles=[], categories=[],
        has_prev_trip=False, has_next_trip=False, path_geojson=None,
        path_snapped_geojson=None, stay_centroids="[]", min_trip_distance_m=100,
        user={"name": "Tester"}, csrf_token="test",
    )
    assert "L.tileLayer(\"https://tiles.example.net/{z}/{x}/{y}.png\"" in body
    assert '"Example attribution"' in body
    assert "tile.openstreetmap.org" not in body


def test_base_dialog_behavior_opens_closes_and_restores_trigger_focus():
    source = _templates().env.get_template("base.html").render(
        user={"name": "Tester"}, csrf_token="test",
    )

    assert "dialog.showModal()" in source
    assert 'cancel.closest("dialog")?.close("cancel")' in source
    assert 'dialog.addEventListener("close"' in source
    assert "trigger.focus()" in source


def test_settings_presents_discard_as_restorable_deleted_trip():
    body = _templates().env.get_template("settings.html").render(
        boundary_overrides=[{
            "id": 9,
            "device": "phone",
            "kind": "discard",
            "range_start": datetime(2026, 7, 1, 9, tzinfo=TZ),
            "range_end": datetime(2026, 7, 1, 10, tzinfo=TZ),
            "point_id": None,
            "created_at": datetime(2026, 7, 1, 11, tzinfo=TZ),
        }],
        rates=[], vehicles=[], odometer=[], places=[], rules=[],
        geocode_enabled=False, user={"name": "Tester"}, csrf_token="test",
    )

    assert "Manual trip edits (1)" in body
    assert "Deleted" in body
    assert 'title="Restore deleted trip"' in body
    assert 'hx-post="/settings/boundary_overrides/9/delete"' in body
    assert "Restore this deleted trip?" in body
    assert "stored location data will be reprocessed" in body


def _override(id_, kind, point_id=None):
    return {
        "id": id_,
        "device": "phone",
        "kind": kind,
        "range_start": datetime(2026, 7, 1, 9, tzinfo=TZ),
        "range_end": datetime(2026, 7, 1, 10, tzinfo=TZ),
        "point_id": point_id,
        "created_at": datetime(2026, 7, 1, 11, tzinfo=TZ),
    }


def _render_settings(boundary_overrides):
    return _templates().env.get_template("settings.html").render(
        boundary_overrides=boundary_overrides,
        rates=[], vehicles=[], odometer=[], places=[], rules=[],
        geocode_enabled=False, user={"name": "Tester"}, csrf_token="test",
    )


def test_manual_trip_edits_summary_shows_the_count():
    body = _render_settings([_override(1, "suppress"), _override(2, "force", point_id=5)])

    assert "Manual trip edits (2)" in body


def test_manual_trip_edits_heading_is_an_h2_in_the_document_outline():
    # Regression: an earlier revision used a bare <summary> with no <h2>,
    # which dropped the section from the document outline entirely and made
    # it look like a control belonging to the preceding section.
    body = _render_settings([_override(1, "suppress")])

    assert "<summary><h2>Manual trip edits (1)</h2></summary>" in body


def test_manual_trip_edits_section_is_collapsed_by_default():
    body = _render_settings([_override(1, "suppress")])

    id_pos = body.index('id="manual-trip-edits"')
    tag_start = body.rfind("<details", 0, id_pos)
    tag_end = body.index(">", id_pos)
    opening_tag = body[tag_start:tag_end]
    assert "open" not in opening_tag


def test_manual_trip_edits_empty_state():
    body = _render_settings([])

    assert "Manual trip edits (0)" in body
    assert "No manual trip edits." in body


def test_manual_trip_edits_kind_column_uses_plain_language():
    body = _render_settings([
        _override(1, "suppress"),
        _override(2, "force", point_id=5),
        _override(3, "discard"),
    ])

    assert "Merged" in body
    assert "Split" in body
    assert "Deleted" in body
    assert "Suppress" not in body
    assert "Force" not in body
    assert "Discard" not in body


def test_manual_trip_edits_split_detail_reads_at_point():
    body = _render_settings([_override(1, "force", point_id=48211)])

    assert "at point #48211" in body


def test_settings_offers_a_three_way_theme_control():
    body = _templates().env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[],
        rules=[], geocode_enabled=False, user={"name": "Tester"}, csrf_token="test",
    )

    picker = body.split('id="theme-picker"')[1].split("</fieldset>")[0]
    assert '<input type="radio" name="theme-choice" value="system">' in picker
    assert '<input type="radio" name="theme-choice" value="light">' in picker
    assert '<input type="radio" name="theme-choice" value="dark">' in picker
    assert "localStorage.removeItem(\"theme\")" in body
    assert "localStorage.setItem(\"theme\", choice)" in body


def test_settings_tables_are_full_width_padded_and_scroll_inside_section_wrappers():
    body = _templates().env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[],
        rules=[], geocode_enabled=False, user={"name": "Tester"}, csrf_token="test",
    )
    odometer_source = _templates().env.loader.get_source(
        _templates().env, "_odometer_table.html"
    )[0]

    # boundary_overrides=[] here renders the "No manual trip edits." empty
    # state instead of a table, so it's one short of the other five sections.
    assert body.count('class="settings-table"') == 4
    assert odometer_source.count('class="settings-table"') == 2
    assert body.count('class="settings-table-scroll ') == 4
    assert odometer_source.count('class="settings-table-scroll ') == 2
    assert '<div id="rates-table">\n<div class="settings-table-scroll settings-table-scroll-rates">' in body
    assert '<div id="vehicles-table">\n<div class="settings-table-scroll settings-table-scroll-vehicles">' in body
    assert '<div class="settings-table-scroll settings-table-scroll-places">\n<table id="places-table"' in body
    assert 'hx-target="#rates-table" hx-swap="outerHTML"' in body
    assert 'hx-target="#vehicles-table" hx-swap="outerHTML"' in body
    assert 'hx-target="#odometer-table" hx-swap="outerHTML"' in body

    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert ".settings-table { width: 100%; }" in css
    assert ".settings-table-scroll { max-width: 100%; overflow-x: auto;" in css
    assert ".settings-table { display: block" not in css
    assert ".settings-table th:first-child" not in css
    assert ".settings-table th:last-child" not in css
    assert "th, td { text-align: left; padding: .35rem .5rem;" in css
    for section in ("rates", "vehicles", "odometer-readings", "odometer-intervals",
                    "places", "rules", "overrides", "workers"):
        assert f".settings-table-scroll-{section} .settings-table {{ min-width:" in css
    assert ".settings-table button, .vehicle-default-indicator { white-space: nowrap; }" in css


def test_diagnostics_workers_table_scrolls_inside_its_own_wrapper():
    body = _templates().env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[],
        rules=[], geocode_enabled=False, user={"name": "Tester", "is_admin": True}, csrf_token="test",
        diagnostics={"app_version": "test", "git_revision": "test",
                     "schema_version": "1", "detector_version": 1},
        diagnostics_report={
            "database": {"ok": True, "error_type": None, "stats": {}},
            "migrations": {"status": "up_to_date", "applied": [1], "expected": [1]},
            "workers": [{
                "name": "detector", "enabled": True, "state_available": True,
                "last_run_at": None, "last_success_at": None, "last_skip_at": None,
                "last_failure_at": None, "last_failure_type": None, "next_run_at": None,
            }],
            "config_presence": {},
        },
    )

    # Same wrapper convention as every other settings table (rates, vehicles,
    # places, ...), which the Workers table previously lacked.
    assert '<div class="settings-table-scroll settings-table-scroll-workers">\n  <table class="settings-table">' in body


def test_vehicle_default_uses_neutral_control_scale_indicator():
    body = _templates().env.get_template("_vehicles_table.html").render(
        vehicles=[{
            "id": 1, "name": "My Car", "make": None, "model": None,
            "plate": None, "active": True, "is_default": True,
        }]
    )
    css = (Path(__file__).parents[1] / "static/style.css").read_text()

    assert '<span class="vehicle-default-indicator">Default</span>' in body
    assert '<span class="manual-badge">default</span>' not in body
    assert ".vehicle-default-indicator {" in css
    assert "display: inline-flex; min-height: var(--control-height); align-items: center;" in css
    assert "background: var(--surface0); color: var(--fg);" in css


def test_rates_help_remains_outside_scroll_area_in_full_and_partial_renders():
    templates = _templates()
    context = dict(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[],
        rules=[], geocode_enabled=False, user={"name": "Tester"}, csrf_token="test",
    )
    partial = templates.env.get_template("_rates_table.html").render(**context)
    full = templates.env.get_template("settings.html").render(**context)
    assert partial in full
    assert '</table>\n</div>\n\n<p class="muted">Tick <em>split from</em>' in partial
    assert 'hx-target="#rates-table" hx-swap="outerHTML"' in partial
    css = (Path(__file__).parents[1] / "static/style.css").read_text()
    assert (
        ".settings-page .settings-section > .muted,\n"
        ".settings-page #rates-table > .muted { max-width: 72ch; }"
    ) in css
    assert ".settings-table-scroll { max-width: 100%; overflow-x: auto;" in css
