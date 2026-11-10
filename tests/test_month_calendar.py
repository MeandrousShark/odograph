"""Month calendar: the grid model, month parsing and navigation, the shared
day function, DST, the route's lane and parameter handling, and the
templates. Database reads and row security are in
tests/test_month_calendar_db.py.
"""
from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from starlette.routing import Match

from app.dashboard import build_week_dashboard, daily_distances, week_bounds
from app.main import make_templates
from app.month_calendar import build_month_calendar, month_grid, parse_month
from app.trip_archive import month_bounds
from app.ui import make_router

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
MILE = 1609.344
USER = {"sub": "test"}


def _url(day: date) -> str:
    return f"/trips?from={day.isoformat()}&to={day.isoformat()}"


def _trip(started_at: datetime, meters: float, category="business", exclusion=None) -> dict:
    return {
        "started_at": started_at, "display_distance_m": meters,
        "category": category, "exclusion": exclusion,
    }


def _dashboard_trip(trip_id: int, started_at: datetime, meters, category, exclusion) -> dict:
    return {
        "id": trip_id, "started_at": started_at, "display_distance_m": meters,
        "category": category, "exclusion": exclusion,
        "prev_end_gap_m": None, "prev_trip_ended_at": None, "prev_trip_end_lat": None,
        "prev_trip_end_lon": None, "prev_trip_end_place_name": None,
        "missing_trip_covered": False, "start_place_name": None, "start_lat": None,
        "start_lon": None, "start_address": None,
    }


def _days(model) -> dict[date, object]:
    return {cell.day: cell for week in model.weeks for cell in week}


# --- month parsing ------------------------------------------------------

def test_parse_month_defaults_to_todays_month():
    assert parse_month("", date(2026, 11, 10)) == date(2026, 11, 1)


@pytest.mark.parametrize("value, expected", [
    ("2026-02", date(2026, 2, 1)),
    ("0001-01", date(1, 1, 1)),
    ("9998-12", date(9998, 12, 1)),
])
def test_parse_month_accepts_yyyy_mm_within_the_year_bounds(value, expected):
    assert parse_month(value, date(2026, 11, 10)) == expected


@pytest.mark.parametrize("value", [
    "2026-1", "2026-13", "2026-00", "0000-01", "9999-01", "26-02", "2026-02-01",
    " 2026-02", "2026-02 ", "2026/02", "abcd-ef", "２０２６-01",
])
def test_parse_month_refuses_anything_else(value):
    with pytest.raises(ValueError):
        parse_month(value, date(2026, 11, 10))


# --- grid ---------------------------------------------------------------

def test_grid_fills_leading_and_trailing_days_from_the_neighboring_months():
    # February 2024 starts on a Thursday and, as a leap year, ends on the 29th.
    weeks = month_grid(date(2024, 2, 1))
    assert len(weeks) == 5
    assert weeks[0] == [
        date(2024, 1, 29), date(2024, 1, 30), date(2024, 1, 31),
        date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 3), date(2024, 2, 4),
    ]
    assert weeks[-1] == [
        date(2024, 2, 26), date(2024, 2, 27), date(2024, 2, 28), date(2024, 2, 29),
        date(2024, 3, 1), date(2024, 3, 2), date(2024, 3, 3),
    ]


def test_grid_for_a_month_starting_on_monday_has_no_leading_days():
    weeks = month_grid(date(2021, 2, 1))
    assert len(weeks) == 4
    assert weeks[0][0] == date(2021, 2, 1)
    assert weeks[-1][-1] == date(2021, 2, 28)
    weeks = month_grid(date(2026, 6, 1))
    assert weeks[0][0] == date(2026, 6, 1)
    assert weeks[-1] == [
        date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2),
        date(2026, 7, 3), date(2026, 7, 4), date(2026, 7, 5),
    ]


def test_grid_for_a_31_day_month_starting_on_sunday_has_six_rows():
    weeks = month_grid(date(2026, 3, 1))
    assert len(weeks) == 6
    assert weeks[0][0] == date(2026, 2, 23)
    assert weeks[0][-1] == date(2026, 3, 1)
    assert weeks[-1] == [
        date(2026, 3, 30), date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 2),
        date(2026, 4, 3), date(2026, 4, 4), date(2026, 4, 5),
    ]


def test_grid_builds_at_the_year_bounds():
    assert month_grid(date(1, 1, 1))[0][0] == date(1, 1, 1)
    assert month_grid(date(9998, 12, 1))[-1][-1] == date(9999, 1, 3)


def test_neighbor_days_carry_no_distance_or_link():
    model = build_month_calendar(
        [_trip(datetime(2024, 1, 31, 17, tzinfo=UTC), 5000.0)],
        date(2024, 2, 1), UTC, date(2024, 2, 10), _url,
    )
    days = _days(model)
    for day in (date(2024, 1, 29), date(2024, 1, 31), date(2024, 3, 3)):
        assert not days[day].in_month
        assert days[day].distance is None and days[day].archive_url is None
    assert days[date(2024, 2, 29)].in_month
    assert days[date(2024, 2, 29)].archive_url == "/trips?from=2024-02-29&to=2024-02-29"
    assert model.max_day_m == 0.0
    assert days[date(2024, 2, 10)].is_today
    assert sum(cell.is_today for cell in days.values()) == 1


# --- navigation ---------------------------------------------------------

def _nav(month: date, today: date):
    return build_month_calendar([], month, UTC, today, _url).nav


def test_current_month_has_no_next_month():
    nav = _nav(date(2026, 11, 1), date(2026, 11, 10))
    assert nav.is_current_month
    assert nav.prev_month == date(2026, 10, 1)
    assert nav.next_month is None


def test_past_month_links_forward_across_a_year():
    nav = _nav(date(2025, 12, 1), date(2026, 11, 10))
    assert not nav.is_current_month
    assert nav.next_month == date(2026, 1, 1)
    assert _nav(date(2026, 1, 1), date(2026, 11, 10)).prev_month == date(2025, 12, 1)


def test_later_month_reached_by_url_renders_without_next():
    nav = _nav(date(2027, 3, 1), date(2026, 11, 10))
    assert not nav.is_current_month
    assert nav.prev_month == date(2027, 2, 1)
    assert nav.next_month is None


def test_year_bounds_stop_previous_and_next():
    assert _nav(date(1, 1, 1), date(2026, 11, 10)).prev_month is None
    assert _nav(date(1, 2, 1), date(2026, 11, 10)).prev_month == date(1, 1, 1)
    assert _nav(date(9998, 12, 1), date(2026, 11, 10)).next_month is None


# --- the shared day function -------------------------------------------

def test_daily_distances_follow_the_dashboard_rules():
    day = datetime(2026, 7, 13, 12, tzinfo=UTC)
    series = daily_distances([
        _trip(day, 1000.0, "business"),
        _trip(day, 2000.0, "personal"),
        _trip(day, 3000.0, "unclassified"),
        _trip(day, 4000.0, "business", "not_deductible"),
        _trip(day, 5000.0, "business", "not_my_vehicle"),
        _trip(day, None, "personal"),
        _trip(day + timedelta(days=1), 600.0, None),
        _trip(day + timedelta(days=5), 700.0),  # outside the two days
    ], UTC, date(2026, 7, 13), 2)
    assert [entry.day for entry in series] == [date(2026, 7, 13), date(2026, 7, 14)]
    first, second = series
    assert (first.business_m, first.personal_m, first.unclassified_m, first.nondeductible_m) == (
        1000.0, 2000.0, 3000.0, 4000.0,
    )
    assert first.total_m == 10000.0
    assert second.unclassified_m == 600.0 and second.total_m == 600.0


def test_a_weeks_days_are_identical_through_the_dashboard_and_the_calendar():
    rng = random.Random(92)
    monday = date(2026, 3, 2)  # this week holds the Mar 8 spring-forward
    for _ in range(50):
        start = week_bounds(monday, NY).start.astimezone(UTC)
        trips = [
            _dashboard_trip(
                index, start + timedelta(seconds=rng.uniform(-86400, 15 * 86400)),
                rng.choice([None, 0, rng.uniform(1, 90000)]),
                rng.choice(["business", "personal", "unclassified", None]),
                rng.choice([None, None, "not_deductible", "not_my_vehicle"]),
            )
            for index in range(rng.randrange(0, 40))
        ]
        trips.sort(key=lambda trip: trip["started_at"], reverse=True)
        calendar_days = _days(build_month_calendar(trips, date(2026, 3, 1), NY, date(2026, 11, 10), _url))
        for week_start in (monday, monday + timedelta(days=7)):
            bounds = week_bounds(week_start, NY)
            week_trips = [t for t in trips if bounds.start <= t["started_at"] < bounds.end]
            dashboard = build_week_dashboard(
                week_trips, 0, {}, week_start, NY, datetime(2026, 11, 10, tzinfo=NY), 1000.0,
            )
            for entry in dashboard.daily_series:
                assert calendar_days[entry.day].distance == entry


# --- DST ----------------------------------------------------------------

def test_spring_forward_month_bounds_are_local_midnights():
    start, end = month_bounds(2026, 3, NY)
    assert start.astimezone(UTC) == datetime(2026, 3, 1, 5, tzinfo=UTC)   # EST
    assert end.astimezone(UTC) == datetime(2026, 4, 1, 4, tzinfo=UTC)     # EDT


def test_fall_back_month_bounds_are_local_midnights():
    start, end = month_bounds(2026, 11, NY)
    assert start.astimezone(UTC) == datetime(2026, 11, 1, 4, tzinfo=UTC)  # EDT
    assert end.astimezone(UTC) == datetime(2026, 12, 1, 5, tzinfo=UTC)    # EST


def test_spring_forward_day_buckets_trips_near_local_midnight():
    # 2026-03-08 02:00 EST jumps to 03:00 EDT.
    trips = [
        _trip(datetime(2026, 3, 8, 4, 30, tzinfo=UTC), 1.0),    # Mar 7 23:30 EST
        _trip(datetime(2026, 3, 8, 5, 10, tzinfo=UTC), 10.0),   # Mar 8 00:10 EST
        _trip(datetime(2026, 3, 9, 3, 30, tzinfo=UTC), 100.0),  # Mar 8 23:30 EDT
        _trip(datetime(2026, 3, 9, 4, 10, tzinfo=UTC), 1000.0), # Mar 9 00:10 EDT
    ]
    days = _days(build_month_calendar(trips, date(2026, 3, 1), NY, date(2026, 11, 10), _url))
    assert days[date(2026, 3, 7)].distance.total_m == 1.0
    assert days[date(2026, 3, 8)].distance.total_m == 110.0
    assert days[date(2026, 3, 9)].distance.total_m == 1000.0


def test_fall_back_day_buckets_trips_near_local_midnight():
    # 2026-11-01 02:00 EDT falls back to 01:00 EST, on the month's first day.
    trips = [
        _trip(datetime(2026, 11, 1, 3, 30, tzinfo=UTC), 1.0),    # Oct 31 23:30 EDT
        _trip(datetime(2026, 11, 1, 4, 10, tzinfo=UTC), 10.0),   # Nov 1 00:10 EDT
        _trip(datetime(2026, 11, 2, 4, 30, tzinfo=UTC), 100.0),  # Nov 1 23:30 EST
        _trip(datetime(2026, 11, 2, 5, 10, tzinfo=UTC), 1000.0), # Nov 2 00:10 EST
    ]
    days = _days(build_month_calendar(trips, date(2026, 11, 1), NY, date(2026, 11, 10), _url))
    assert not days[date(2026, 10, 31)].in_month
    assert days[date(2026, 11, 1)].distance.total_m == 110.0
    assert days[date(2026, 11, 2)].distance.total_m == 1000.0


# --- route --------------------------------------------------------------

def _routes():
    return make_router().routes


def test_calendar_route_matches_and_is_in_the_navigation_lane():
    scope = {"type": "http", "method": "GET", "path": "/calendar"}
    matched = [route for route in _routes() if route.matches(scope)[0] is Match.FULL]
    assert matched[0].path == "/calendar"
    assert matched[0].capacity_lane == "navigation"
    assert {
        dependency.call.__name__ for dependency in matched[0].dependant.dependencies
    } == {"require_user"}


@pytest.mark.parametrize("month", ["2026-13", "0000-01", "9999-01", "26-1", "nope"])
def test_malformed_month_is_a_422_before_any_read(month):
    endpoint = next(route.endpoint for route in _routes() if route.path == "/calendar")
    request = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(display_tz=NY)))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(endpoint(request, USER, month))
    assert refused.value.status_code == 422


# --- templates ----------------------------------------------------------

def _render(name: str, **context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=NY, app_version="test"))
    base = {"user": USER, "csrf": "token", "csp_nonce": "test-nonce", "static_version": "v1"}
    return templates.env.get_template(name).render(**{**base, **context})


def _calendar_body(trips, month=date(2026, 3, 1), today=date(2026, 11, 10)) -> str:
    return _render("calendar.html", calendar=build_month_calendar(trips, month, NY, today, _url))


def _cell(body: str, day: date) -> str:
    marker = f'href="/trips?from={day.isoformat()}&amp;to={day.isoformat()}"'
    return body.split(marker, 1)[1].split("</td>", 1)[0]


def test_calendar_template_links_each_day_and_labels_it():
    noon = datetime(2026, 3, 9, 17, tzinfo=UTC)
    body = _calendar_body([
        _trip(noon, 10 * MILE, "business"),
        _trip(noon, 2.5 * MILE, "personal"),
        _trip(noon, 4 * MILE, "business", "not_my_vehicle"),
    ])
    assert '<div class="page calendar-page">' in body
    assert "March 2026" in body
    assert body.count('class="calendar-day-link"') == 31
    cell = _cell(body, date(2026, 3, 9))
    assert 'aria-label="Monday, March 9: 12.5 miles total. Business 10.0 miles; Personal 2.5 miles."' in cell
    assert ">12.5<" in cell
    assert 'class="dashboard-bar-segment dashboard-bar-business" style="flex-basis: 80.0%;"' in cell
    assert 'class="calendar-bar-fill" style="width: 100.0%;"' in cell
    empty = _cell(body, date(2026, 3, 10))
    assert 'aria-label="Tuesday, March 10: 0.0 miles total."' in empty
    assert "calendar-bar" not in empty


def test_calendar_template_mutes_neighbor_days_without_links():
    body = _calendar_body([])
    outside = body.split('class="calendar-day calendar-day-outside"')[1:]
    # February 23 to 28 lead and April 1 to 5 trail March 2026.
    assert len(outside) == 11
    assert all("<a " not in cell.split("</td>", 1)[0] for cell in outside)
    assert 'aria-hidden="true">23</span>' in outside[0]
    assert "from=2026-02-28" not in body and "from=2026-04-01" not in body


def test_calendar_template_navigation():
    past = _calendar_body([])
    assert 'href="/calendar?month=2026-02"' in past
    assert 'href="/calendar?month=2026-04"' in past
    assert '<a class="week-nav-link control control-secondary" href="/calendar">This month</a>' in past

    current = _calendar_body([], month=date(2026, 11, 1))
    assert 'href="/calendar?month=2026-10"' in current
    assert 'aria-label="Next month unavailable"' in current
    assert "This month</a>" not in current
    assert 'aria-current="date"' in _cell(current, date(2026, 11, 10))

    first = _calendar_body([], month=date(1, 1, 1))
    assert "Previous month" not in first
    assert 'href="/calendar?month=0001-02"' in first


def test_dashboard_week_header_links_to_the_weeks_month():
    dashboard = build_week_dashboard(
        [], 0, {}, date(2026, 6, 29), NY, datetime(2026, 7, 2, 12, tzinfo=NY), 1000.0,
    )
    body = _render(
        "dashboard.html", dashboard=dashboard, vehicles=[], recent_purposes=[],
    )
    header = body.split('aria-label="Week navigation"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/calendar?month=2026-06">Calendar</a>' in header
