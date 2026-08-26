"""Template tests for the weekly dashboard. `dashboard.html` is rendered
directly against a
hand-built `WeekDashboard` (same "build the pure model, render it, assert on
the HTML" pattern as `tests/test_trips_template.py`) so these stay fast and
DB-free; `tests/test_dashboard_db.py` covers the route/query wiring.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.dashboard import (
    AttentionStrip,
    DayGroup,
    DeductionEstimate,
    DistanceBreakdown,
    WeekDashboard,
    WeekNav,
)
from app.main import make_templates

TZ = ZoneInfo("America/Los_Angeles")
MI = 1609.344  # app.rates.METERS_PER_MILE, kept local so this file has no DB-touching import


def _trip(trip_id: int, started_at: datetime, ended_at: datetime, category="business",
          distance_m: float = 10 * MI, source: str = "detected", **overrides) -> dict:
    trip = {
        "id": trip_id, "device": "phone", "source": source,
        "started_at": started_at, "ended_at": ended_at,
        "distance_m": distance_m, "display_distance_m": distance_m,
        "snap_status": "snapped" if source == "detected" else None,
        "point_count": 20, "has_gap": False,
        "category": category, "purpose": "", "notes": "",
        "start_lat": 47.0, "start_lon": -122.0, "end_lat": 47.1, "end_lon": -122.1,
        "start_place_name": "Home", "end_place_name": "Office",
        "vehicle_id": None, "vehicle_name": None,
        "has_route_geometry": source == "detected",
        "start_address": None, "end_address": None,
        "prev_end_gap_m": None, "prev_trip_ended_at": None,
        "prev_trip_end_lat": None, "prev_trip_end_lon": None,
        "prev_trip_end_place_name": None, "missing_trip_covered": False,
    }
    trip.update(overrides)
    return trip


def _nav(week_start=date(2026, 7, 13), week_end=date(2026, 7, 19), is_current_week=True,
          prev=date(2026, 7, 6), nxt=date(2026, 7, 20)) -> WeekNav:
    return WeekNav(
        week_start=week_start, week_end=week_end,
        prev_week_start=prev, next_week_start=nxt, is_current_week=is_current_week,
    )


def _dashboard(**overrides) -> WeekDashboard:
    defaults = dict(
        trip_count=0,
        distance=DistanceBreakdown(0.0, 0.0, 0.0, 0.0),
        expense_total=Decimal("0.00"),
        deduction=DeductionEstimate(amount=0.0, available=True),
        day_groups=[],
        attention=None,
        nav=_nav(),
    )
    defaults.update(overrides)
    return WeekDashboard(**defaults)


def _render(dashboard: WeekDashboard) -> str:
    templates = make_templates(SimpleNamespace(
        display_tz=TZ, missing_trip_gap_m=1000.0, app_version="test",
    ))
    return templates.env.get_template("dashboard.html").render(
        dashboard=dashboard, user={"sub": "test"}, csrf="token",
    )


def test_metrics_render_trip_count_distance_and_expenses():
    dashboard = _dashboard(
        trip_count=3,
        distance=DistanceBreakdown(total_m=10 * MI, business_m=6 * MI, personal_m=3 * MI, unclassified_m=1 * MI),
        expense_total=Decimal("42.50"),
        deduction=DeductionEstimate(amount=4.02, available=True),
    )
    body = _render(dashboard)
    assert ">3<" in body
    assert "10.0 mi" in body
    assert "6.0 mi business" in body
    assert "3.0 mi personal" in body
    assert "1.0 mi unclassified" in body
    assert "$42.50" in body
    assert "$4.02" in body


def test_exact_date_range_heading():
    body = _render(_dashboard())
    assert (
        '<h2 class="dashboard-heading">Jul 13, 2026 - Jul 19, 2026</h2>'
        in body
    )


def test_next_week_disabled_on_current_week():
    body = _render(_dashboard(nav=_nav(is_current_week=True)))
    assert 'aria-disabled="true"' in body
    assert 'href="/?week=2026-07-20"' not in body


def test_next_week_enabled_on_a_past_week():
    body = _render(_dashboard(nav=_nav(
        week_start=date(2026, 6, 29), week_end=date(2026, 7, 5),
        is_current_week=False, prev=date(2026, 6, 22), nxt=date(2026, 7, 6),
    )))
    assert 'href="/?week=2026-07-06"' in body
    assert 'aria-disabled="true"' not in body


def test_previous_week_and_current_week_links_always_present():
    body = _render(_dashboard())
    assert 'href="/?week=2026-07-06"' in body
    assert '>Current week</a>' in body
    assert 'href="/">Current week</a>' in body


def test_empty_week_state_keeps_metric_cards_and_nav():
    body = _render(_dashboard())
    assert "No trips this week." in body
    assert ">0<" in body  # trip count metric still rendered
    assert 'href="/?week=2026-07-06"' in body  # nav still present


def test_attention_strip_absent_when_none():
    body = _render(_dashboard(attention=None))
    assert "attention-strip" not in body


def test_attention_strip_present_with_review_and_missing_trip_links():
    dashboard = _dashboard(attention=AttentionStrip(
        unclassified_count=2, review_url="/review?from=2026-07-13&to=2026-07-19",
        missing_trip_count=1, missing_trip_url="/trips?manual_date=2026-07-14#manual-trip",
    ))
    body = _render(dashboard)
    assert "attention-strip" in body
    assert 'href="/review?from=2026-07-13&amp;to=2026-07-19"' in body
    assert "2 unclassified" in body
    assert 'href="/trips?manual_date=2026-07-14#manual-trip"' in body
    assert "1 possible missing trip" in body


def test_view_all_trips_and_add_manual_trip_links():
    body = _render(_dashboard())
    assert 'class="control control-secondary" href="/trips">View all trips</a>' in body
    assert 'class="control control-secondary" href="/trips?manual_open=true#manual-trip">Add manual trip</a>' in body


def test_deduction_unavailable_shows_settings_link_not_a_number():
    body = _render(_dashboard(deduction=DeductionEstimate(amount=None, available=False)))
    assert "Unavailable" in body
    assert 'href="/settings"' in body
    assert "add one" in body


def test_detected_and_manual_cards_render_through_shared_partial():
    detected = _trip(1, datetime(2026, 7, 13, 15, tzinfo=TZ),
                      datetime(2026, 7, 13, 16, tzinfo=TZ), source="detected")
    manual = _trip(2, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ),
                    source="manual", category="personal")
    dashboard = _dashboard(
        trip_count=2,
        day_groups=[DayGroup(day=date(2026, 7, 13), is_today=False, is_yesterday=False,
                              trips=[manual, detected])],
    )
    body = _render(dashboard)
    assert 'id="trip-1"' in body
    assert 'id="trip-2"' in body
    assert 'class="status-badge manual-badge">Manual</span>' in body
    assert body.count('class="card trip-card"') == 2


def test_day_group_heading_includes_today_and_yesterday_prefixes():
    dashboard = _dashboard(day_groups=[
        DayGroup(day=date(2026, 7, 13), is_today=True, is_yesterday=False,
                  trips=[_trip(1, datetime(2026, 7, 13, 9, tzinfo=TZ), datetime(2026, 7, 13, 10, tzinfo=TZ))]),
        DayGroup(day=date(2026, 7, 12), is_today=False, is_yesterday=True,
                  trips=[_trip(2, datetime(2026, 7, 12, 9, tzinfo=TZ), datetime(2026, 7, 12, 10, tzinfo=TZ))]),
    ])
    body = _render(dashboard)
    assert "Today" in body
    assert "Yesterday" in body
    assert "Mon Jul 13" in body
    assert "Sun Jul 12" in body
