from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates
from tests.test_trip_row_template import _trip


TZ = ZoneInfo("America/Los_Angeles")


def _render(
    source="detected", errors=None, dashboard_week="",
    start_label_value="", end_label_value="", **overrides,
):
    trip = _trip(source=source, **overrides)
    values = {
        "category": "business",
        "exclusion": "not_my_vehicle",
        "purpose": "Client visit",
        "notes": "Submitted notes",
        "vehicle_id": "9",
        "date": "2026-07-14",
        "start_time": "23:30",
        "end_time": "01:00",
        "distance": "8.4",
        "start_label": start_label_value,
        "end_label": end_label_value,
    }
    return make_templates(SimpleNamespace(display_tz=TZ, app_version="test")).env.get_template(
        "_trip_edit_card.html"
    ).render(
        trip=trip,
        values=values,
        errors=errors or {},
        recent_purposes=["Client visit"],
        vehicles=[{"id": 1, "name": "Active car"}],
        dashboard_week=dashboard_week,
    )


def test_detected_edit_exposes_only_human_owned_fields_and_card_targets():
    body = _render()

    for name in ("category", "exclusion", "purpose", "notes", "vehicle_id"):
        assert f'name="{name}"' in body
    for name in ("date", "start_time", "end_time", "distance"):
        assert f'name="{name}"' not in body
    assert 'hx-post="/trips/42/edit"' in body
    assert 'hx-get="/trips/42/card"' in body
    assert 'hx-target="#trip-42"' in body
    assert 'hx-swap="outerHTML"' in body
    assert '<option value="not_my_vehicle" selected>Not one of my vehicles</option>' in body


def test_edit_category_uses_shared_labeled_radio_group_with_exact_checked_value():
    body = _render()
    group = body.split('<fieldset class="category-segmented">', 1)[1].split(
        "</fieldset>", 1
    )[0]

    assert "<legend>Category</legend>" in group
    assert group.count('type="radio"') == 3
    assert group.index('value="personal"') < group.index('value="unclassified"')
    assert group.index('value="unclassified"') < group.index('value="business"')
    assert 'value="business"\n           checked' in group
    assert "hx-post" not in group


def test_manual_edit_exposes_time_and_distance_fields_with_submitted_values():
    body = _render(source="manual")

    for name, value in (("date", "2026-07-14"), ("start_time", "23:30"),
                        ("end_time", "01:00"), ("distance", "8.4")):
        assert f'name="{name}" value="{value}"' in body


def test_edit_errors_are_accessible_and_do_not_discard_values():
    body = _render(source="manual", errors={"distance": "Distance is invalid."})

    assert 'role="alert"' in body
    assert 'name="distance" value="8.4"' in body
    assert 'aria-invalid="true"' in body
    assert 'aria-describedby="edit-distance-42-error"' in body
    assert 'id="edit-distance-42-error">Distance is invalid.</span>' in body


def test_edit_card_keeps_the_selection_marker_class():
    # trips.html's selection script (updateSelectionShell/reconcileSelection)
    # queries .trip-archive-item, not .trip-archive-row (that class also
    # switches on the row's own CSS grid layout, which the edit card must
    # not pick up). Without this marker, a row being edited in place would
    # silently drop out of Select all and is-selected reconciliation.
    body = _render()

    assert 'class="card trip-card trip-edit-card trip-archive-item"' in body


def test_inactive_assigned_vehicle_remains_representable():
    body = _render(vehicle_id=9, vehicle_name="Retired car")

    assert '<option value="9" selected>Retired car (inactive)</option>' in body


def test_dashboard_edit_preserves_week_context_for_form_and_cancel():
    body = _render(dashboard_week="2026-07-13")

    assert 'name="dashboard_week" value="2026-07-13"' in body
    assert 'hx-get="/trips/42/card?dashboard_week=2026-07-13"' in body


# --- Start/end location name eligibility and prefill -----------------------
#
# Eligibility mirrors migrations/025_manual_trip_labels.sql's own constraint
# exactly: manual source, no saved place id, no geometry for that endpoint
# (start_lat/end_lat come from ST_Y(start_geom/end_geom), so a null value
# means null geometry). It is checked per endpoint, not once per trip.

def _eligible(**overrides):
    values = {
        "start_place_id": None, "end_place_id": None,
        "start_lat": None, "end_lat": None,
    }
    values.update(overrides)
    return _render(source="manual", **values)


def test_eligible_endpoints_always_render_their_inputs_even_when_label_is_null():
    body = _eligible()

    assert 'name="start_label" value="" maxlength="100"' in body
    assert 'name="end_label" value="" maxlength="100"' in body
    assert "Start location name" in body
    assert "End location name" in body


def test_eligible_label_inputs_prefill_with_stored_values():
    body = _eligible(start_label_value="Grandma's house", end_label_value="Work site")

    assert 'name="start_label" value="Grandma&#39;s house" maxlength="100"' in body
    assert 'name="end_label" value="Work site" maxlength="100"' in body


def test_detected_trip_renders_neither_label_input():
    body = _render(source="detected")

    assert 'name="start_label"' not in body
    assert 'name="end_label"' not in body


def test_routed_manual_trip_renders_neither_label_input():
    # _trip()'s defaults (unless overridden) already carry real start/end
    # coordinates, i.e. a routed manual trip, so this only needs source.
    body = _render(source="manual")

    assert 'name="start_label"' not in body
    assert 'name="end_label"' not in body


def test_endpoint_with_a_saved_place_hides_only_that_endpoints_label_input():
    body = _eligible(start_place_id=5)

    assert 'name="start_label"' not in body
    assert 'name="end_label" value="" maxlength="100"' in body


def test_endpoint_with_geometry_hides_only_that_endpoints_label_input():
    body = _eligible(end_lat=47.7)

    assert 'name="start_label" value="" maxlength="100"' in body
    assert 'name="end_label"' not in body


def test_label_value_survives_a_validation_error_on_a_different_field():
    body = _eligible(
        start_label_value="Grandma's house",
        errors={"distance": "Distance is invalid."},
    )

    assert 'name="start_label" value="Grandma&#39;s house" maxlength="100"' in body


def test_label_field_error_renders_next_to_its_own_input_only():
    body = _eligible(
        start_label_value="x" * 101,
        errors={"start_label": "Keep it to 100 characters or fewer."},
    )

    start_block = body.split("Start location name", 1)[1].split("</label>", 1)[0]
    assert 'aria-invalid="true"' in start_block
    assert 'aria-describedby="edit-start-label-42-error"' in start_block
    assert 'id="edit-start-label-42-error">Keep it to 100 characters or fewer.</span>' in start_block

    end_block = body.split("End location name", 1)[1].split("</label>", 1)[0]
    assert "aria-invalid" not in end_block
    assert "field-error" not in end_block


def _identity(body: str) -> str:
    return body.split('<div class="trip-edit-identity">', 1)[1].split(
        '<div class="trip-edit-heading">', 1
    )[0]


def test_detected_edit_card_repeats_the_rows_identifying_summary_above_the_form():
    """B25: the card replaces the row in place, so without this summary a
    detected trip's editor showed nothing identifying and was mistaken for
    the row above it."""
    body = _render()
    identity = _identity(body)

    assert body.index('<div class="trip-edit-identity">') < body.index("<h3>Edit detected trip</h3>")
    assert "<strong>Wed Jul 1</strong>" in identity
    assert '<time datetime="2026-07-01T09:00:00+00:00">02:00</time> to' in identity
    assert '<time datetime="2026-07-01T09:20:00+00:00">02:20</time>' in identity
    assert '<span class="trip-edit-distance">1.0 mi</span>' in identity
    assert 'aria-label="Route: 123 Main St, Seattle, WA 98101 to Home"' in identity
    assert "<span>123 Main St</span>" in identity
    assert "<span>Home</span>" in identity


def test_edit_card_summary_shows_stored_values_not_submitted_ones():
    """A validation redisplay keeps the typed values in the form, but the
    summary still names the trip as saved."""
    body = _render(
        source="manual", errors={"date": "Enter a valid date."},
        start_place_id=None, end_place_id=None,
    )
    identity = _identity(body)

    assert "<strong>Wed Jul 1</strong>" in identity
    assert "2026-07-14" not in identity
    assert "23:30" not in identity
    assert "8.4" not in identity
    assert 'value="2026-07-14"' in body
    assert body.index('<div class="trip-edit-identity">') < body.index("<h3>Edit manual trip</h3>")


def test_dashboard_and_archive_edit_cards_share_the_summary():
    dashboard = _render(dashboard_week="2026-06-29")
    archive = _render()

    assert _identity(dashboard) == _identity(archive)
    assert 'name="dashboard_week" value="2026-06-29"' in dashboard


def test_edit_card_summary_escapes_place_names():
    identity = _identity(_render(end_place_name="Bob's <Garage>"))

    assert "Bob&#39;s &lt;Garage&gt;" in identity
    assert "<Garage>" not in identity
