"""Unit tests for trip list warning acknowledgements: the pure fingerprint
matching in app/trip_warnings.py, the Archive/Dashboard/Trip Detail
templates that read it, the Dashboard attention count, and the routes'
wiring. tests/test_trip_warnings_db.py covers storage, isolation and
lifecycle against a real database."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.auth import require_csrf
from app.dashboard import DayGroup, build_week_dashboard
from app.main import make_templates
from app.missing_trip import missing_trip_badge
from app.trip_warnings import (
    MISSING_TRIP, RECORDING_GAP, WarningFingerprint, current_fingerprints, trip_warnings,
)
from app.ui import make_router

UTC = timezone.utc
START = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)
END = datetime(2026, 7, 13, 15, 30, tzinfo=UTC)
PREV_END = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)


def _trip(**overrides) -> dict:
    trip = {
        "id": 42, "device": "phone", "source": "detected",
        "started_at": START, "ended_at": END,
        "display_distance_m": 1609.344, "distance_m": 1609.344, "distance_snapped_m": None,
        "snap_status": "pending", "point_count": 20, "has_gap": False,
        "category": "business", "exclusion": None, "purpose": None, "notes": None,
        "vehicle_id": None, "vehicle_name": None, "has_route_geometry": True,
        "start_place_name": None, "start_lat": 47.6, "start_lon": -122.3, "start_address": None,
        "end_place_name": "Home", "end_lat": 47.7, "end_lon": -122.4, "end_address": None,
        "expense_count": 0,
        "prev_end_gap_m": None, "prev_trip_ended_at": None, "prev_trip_id": None,
        "prev_trip_end_lat": None, "prev_trip_end_lon": None,
        "prev_trip_end_place_name": None, "missing_trip_covered": False,
    }
    trip.update(overrides)
    return trip


def _missing(**overrides) -> dict:
    """A trip whose predecessor (ID 41) ended 5 km from this start."""
    fields = dict(prev_end_gap_m=5000.0, prev_trip_ended_at=PREV_END, prev_trip_id=41,
                  prev_trip_end_lat=47.0, prev_trip_end_lon=-122.0)
    fields.update(overrides)
    return _trip(**fields)


GAP_ACK = {"gap_ack_trip_started_at": START, "gap_ack_trip_ended_at": END}
MISSING_ACK = {"missing_ack_trip_started_at": START, "missing_ack_prev_trip_id": 41,
               "missing_ack_prev_ended_at": PREV_END}


def _warnings(trip: dict):
    return trip_warnings(trip, missing_trip_badge(trip, 1000.0, UTC))


# --- pure matching ---------------------------------------------------------

def test_fingerprints_cover_only_the_warnings_shown_now():
    assert current_fingerprints(_trip(), None) == {}
    trip = _missing(has_gap=True)
    assert current_fingerprints(trip, missing_trip_badge(trip, 1000.0, UTC)) == {
        RECORDING_GAP: WarningFingerprint(START, END),
        MISSING_TRIP: WarningFingerprint(START, END, 41, PREV_END),
    }


def test_missing_trip_without_predecessor_id_has_no_fingerprint():
    trip = _missing(prev_trip_id=None)
    badge = missing_trip_badge(trip, 1000.0, UTC)
    assert badge is not None
    assert current_fingerprints(trip, badge) == {}
    assert _warnings({**trip, **MISSING_ACK}).listed_missing is not None


def test_matching_fingerprints_acknowledge_each_warning():
    warnings = _warnings(_missing(has_gap=True, **GAP_ACK, **MISSING_ACK))
    assert warnings.gap and warnings.gap_acknowledged and not warnings.listed_gap
    assert warnings.missing is not None and warnings.missing_acknowledged
    assert warnings.listed_missing is None
    assert not warnings.any_listed


def test_unacknowledged_trip_lists_both_warnings():
    warnings = _warnings(_missing(has_gap=True))
    assert warnings.listed_gap and warnings.listed_missing is not None and warnings.any_listed
    assert not warnings.gap_acknowledged and not warnings.missing_acknowledged


def test_gap_acknowledgement_is_stale_after_any_boundary_change():
    for changed in ({"started_at": START - timedelta(minutes=1)},
                    {"ended_at": END + timedelta(seconds=1)}):
        warnings = _warnings(_trip(has_gap=True, **GAP_ACK, **changed))
        assert warnings.listed_gap and not warnings.gap_acknowledged


def test_missing_acknowledgement_is_stale_after_start_or_predecessor_change():
    for changed in ({"started_at": START + timedelta(minutes=1)},
                    {"prev_trip_id": 40},
                    {"prev_trip_ended_at": PREV_END - timedelta(minutes=5)}):
        warnings = _warnings({**_missing(**MISSING_ACK), **changed})
        assert warnings.listed_missing is not None and not warnings.missing_acknowledged


def test_missing_fingerprint_ignores_the_trips_own_end():
    warnings = _warnings(_missing(ended_at=END + timedelta(minutes=10), **MISSING_ACK))
    assert warnings.missing_acknowledged


def test_acknowledgement_without_a_current_warning_shows_nothing():
    warnings = _warnings(_trip(**GAP_ACK, **MISSING_ACK))
    assert not warnings.gap and warnings.missing is None and not warnings.any_listed
    assert not warnings.gap_acknowledged and not warnings.missing_acknowledged


def test_each_kind_is_acknowledged_independently():
    warnings = _warnings(_missing(has_gap=True, **GAP_ACK))
    assert not warnings.listed_gap and warnings.listed_missing is not None
    warnings = _warnings(_missing(has_gap=True, **MISSING_ACK))
    assert warnings.listed_gap and warnings.listed_missing is None


# --- templates -------------------------------------------------------------

def _templates():
    return make_templates(SimpleNamespace(display_tz=UTC, missing_trip_gap_m=1000.0, app_version="test"))


def _archive_row(trip: dict) -> str:
    return _templates().env.get_template("_trip_archive_row.html").render(
        trip=trip, vehicles=[], recent_purposes=[])


def _dashboard_row(trip: dict) -> str:
    return _templates().env.get_template("_dashboard_trip_row.html").render(
        trip=trip, vehicles=[], recent_purposes=[], dashboard_week="2026-07-13")


def _detail(trip: dict) -> str:
    return _templates().env.get_template("trip.html").render(
        trip=trip, recent_purposes=[], vehicles=[], categories=[],
        has_prev_trip=False, has_next_trip=False, path_geojson=None, path_snapped_geojson=None,
        stay_centroids="[]", min_trip_distance_m=100, user={"name": "Tester"}, csrf_token="test",
    )


def _panel(body: str, cls: str) -> str:
    return body.split(f'class="{cls}"', 1)[1].split("</details>", 1)[0]


def test_archive_row_offers_acknowledge_for_shown_warnings():
    body = _archive_row(_missing(has_gap=True))
    assert "Recording gap" in body and "Possible missing trip" in body
    panel = _panel(body, "trip-archive-row-more-panel")
    action = panel.split("Acknowledge warnings", 1)[0].rsplit("<button", 1)[1]
    assert 'hx-post="/trips/42/warnings/acknowledge"' in action
    assert 'hx-target="#trip-42"' in action and 'hx-swap="outerHTML"' in action
    assert "hx-vals" not in action
    assert "Add missing trip" in panel


def test_archive_row_hides_acknowledged_warnings_and_empty_status_area():
    body = _archive_row(_missing(has_gap=True, **GAP_ACK, **MISSING_ACK))
    assert "Recording gap" not in body and "missing-trip-badge" not in body
    assert "trip-archive-row-statuses" not in body
    panel = _panel(body, "trip-archive-row-more-panel")
    assert "Acknowledge warnings" not in panel
    # The bridge prefill stays reachable after acknowledgement.
    assert "Add missing trip" in panel


def test_archive_row_keeps_other_statuses_and_unacknowledged_warning():
    body = _archive_row(_missing(has_gap=True, source="manual", **GAP_ACK))
    statuses = body.split('class="trip-archive-row-statuses"', 1)[1].split("</div>", 1)[0]
    assert "Manual" in statuses and "Possible missing trip" in statuses
    assert "Recording gap" not in statuses
    assert "Acknowledge warnings" in _panel(body, "trip-archive-row-more-panel")


def test_archive_row_without_warnings_has_no_acknowledge_action():
    assert "Acknowledge warnings" not in _archive_row(_trip())


def test_dashboard_row_acknowledge_carries_the_week_and_hides_acknowledged():
    body = _dashboard_row(_trip(has_gap=True))
    panel = _panel(body, "dashboard-row-more-panel")
    action = panel.split("Acknowledge warnings", 1)[0].rsplit("<button", 1)[1]
    assert 'hx-post="/trips/42/warnings/acknowledge"' in action
    assert """hx-vals='{"dashboard_week": "2026-07-13"}'""" in action
    assert 'hx-target="#trip-42"' in action
    summary = body.split('<summary class="dashboard-row-more-trigger"', 1)[1].split(">", 1)[0]
    assert "autofocus" not in summary

    body = _dashboard_row(_trip(has_gap=True, **GAP_ACK))
    assert "Recording gap" not in body and "dashboard-row-statuses" not in body
    assert "Acknowledge warnings" not in body


def test_dashboard_acknowledge_response_focuses_the_rows_actions():
    templates = _templates()
    body = templates.env.get_template("_dashboard_trip_row.html").render(
        trip=_trip(has_gap=True, **GAP_ACK), vehicles=[], recent_purposes=[],
        dashboard_week="2026-07-13", focus_row_actions=True)
    summary = body.split('<summary class="dashboard-row-more-trigger"', 1)[1].split(">", 1)[0]
    assert "autofocus" in summary
    # htmx focuses the first [autofocus] in swapped content; the summary must
    # precede the delete dialog's own autofocus Cancel button.
    assert body.index("autofocus") == body.index(summary) + summary.index("autofocus")


def test_trip_detail_shows_both_warnings_without_markers_until_acknowledged():
    body = _detail(_missing(has_gap=True))
    assert 'role="group" aria-label="Trip warnings"' in body
    warnings = body.split('id="trip-warnings-42"', 1)[1].split("</div>", 1)[0]
    assert "Recording gap" in warnings and "Possible missing trip" in warnings
    assert "Acknowledged" not in warnings and "Show in lists again" not in warnings


def test_trip_detail_marks_each_acknowledged_warning_with_undo():
    body = _detail(_missing(has_gap=True, **GAP_ACK, **MISSING_ACK))
    warnings = body.split('id="trip-warnings-42"', 1)[1].split("</div>", 1)[0]
    assert "Recording gap" in warnings and "Possible missing trip" in warnings
    assert warnings.count("Acknowledged") == 2
    assert warnings.count("Show in lists again") == 2
    assert """hx-vals='{"kind": "recording_gap", "view": "detail"}'""" in warnings
    assert """hx-vals='{"kind": "missing_trip", "view": "detail"}'""" in warnings
    assert warnings.count('hx-post="/trips/42/warnings/unacknowledge"') == 2
    assert warnings.count('hx-target="#trip-warnings-42"') == 2
    # The accessible name starts with the visible label (WCAG 2.5.3).
    assert 'Show in lists again<span class="visually-hidden">: recording gap</span>' in warnings
    assert "aria-label=\"Show" not in warnings


def test_trip_detail_without_warnings_renders_no_warning_section():
    assert "trip-warnings-42" not in _detail(_trip())


# --- dashboard attention strip ---------------------------------------------

def _week(trips: list[dict]):
    return build_week_dashboard(
        trips, Decimal("0"), {}, date(2026, 7, 13), UTC, datetime(2026, 7, 20, tzinfo=UTC), 1000.0)


def test_attention_count_excludes_acknowledged_missing_trips():
    acked = _missing(id=1, **MISSING_ACK)
    shown = _missing(id=2, started_at=START + timedelta(hours=2), ended_at=END + timedelta(hours=2))
    dashboard = _week([shown, acked])
    assert dashboard.attention.missing_trip_count == 1
    assert "bridge_trip=2" in dashboard.attention.missing_trip_url
    assert _week([acked]).attention is None


def test_attention_count_returns_when_the_predecessor_changes():
    stale = {**_missing(**MISSING_ACK), "prev_trip_id": 39}
    assert _week([stale]).attention.missing_trip_count == 1


def test_attention_strip_template_disappears_at_zero():
    body = _templates().env.get_template("_dashboard_hero.html").render(
        dashboard=_week([_missing(**MISSING_ACK)]), dashboard_oob=True, user={"sub": "t"}, csrf="x")
    assert "possible missing trip" not in body


# --- routes ----------------------------------------------------------------

def _route(path: str):
    return next(route for route in make_router().routes
                if getattr(route, "path", None) == path and "POST" in route.methods)


def test_warning_routes_require_a_user_and_csrf():
    for path in ("/trips/{trip_id}/warnings/acknowledge", "/trips/{trip_id}/warnings/unacknowledge"):
        route = _route(path)
        assert any(dependency.dependency is require_csrf for dependency in route.dependencies)
        assert {dependency.call.__name__ for dependency in route.dependant.dependencies} == {
            "require_user", "require_csrf"}


def test_acknowledge_route_takes_no_client_fingerprint():
    import inspect

    params = set(inspect.signature(_route("/trips/{trip_id}/warnings/acknowledge").endpoint).parameters)
    assert params == {"request", "trip_id", "user", "dashboard_week", "view"}
