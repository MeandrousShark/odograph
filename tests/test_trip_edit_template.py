from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates
from tests.test_trip_row_template import _trip


TZ = ZoneInfo("America/Los_Angeles")


def _render(source="detected", errors=None, **overrides):
    trip = _trip(source=source, **overrides)
    values = {
        "category": "business",
        "purpose": "Client visit",
        "notes": "Submitted notes",
        "vehicle_id": "9",
        "date": "2026-07-14",
        "start_time": "23:30",
        "end_time": "01:00",
        "distance": "8.4",
    }
    return make_templates(SimpleNamespace(display_tz=TZ)).env.get_template(
        "_trip_edit_card.html"
    ).render(
        trip=trip,
        values=values,
        errors=errors or {},
        recent_purposes=["Client visit"],
        vehicles=[{"id": 1, "name": "Active car"}],
    )


def test_detected_edit_exposes_only_human_owned_fields_and_card_targets():
    body = _render()

    for name in ("category", "purpose", "notes", "vehicle_id"):
        assert f'name="{name}"' in body
    for name in ("date", "start_time", "end_time", "distance"):
        assert f'name="{name}"' not in body
    assert 'hx-post="/trips/42/edit"' in body
    assert 'hx-get="/trips/42/card"' in body
    assert 'hx-target="#trip-42"' in body
    assert 'hx-swap="outerHTML"' in body


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


def test_inactive_assigned_vehicle_remains_representable():
    body = _render(vehicle_id=9, vehicle_name="Retired car")

    assert '<option value="9" selected>Retired car (inactive)</option>' in body
