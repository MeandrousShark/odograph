"""Place pages: route order and lanes, the counting rule, and the templates.

Database reads, row security and the rendered archive rows are in
tests/test_place_pages_db.py.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from starlette.routing import Match

from app.main import make_templates
from app.rates import YearRate
from app.report import build_range_report
from app.ui import make_router
from app.ui.places import place_archive_url, place_year_totals

TZ = ZoneInfo("America/Los_Angeles")
MILE = 1609.344
USER = {"sub": "test"}
PLACE = {"id": 7, "name": "Office", "kind": "work", "lat": 37.5, "lon": -122.25, "radius_m": 150.0}


def _first_matching_route(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path}
    for route in make_router().routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_places_search_is_not_captured_by_the_place_page():
    assert _first_matching_route("GET", "/places/search").path == "/places/search"
    assert _first_matching_route("GET", "/places").path == "/places"
    assert _first_matching_route("GET", "/places/7").path == "/places/{place_id}"
    assert _first_matching_route("POST", "/places/7/update").path == "/places/{place_id}/update"
    assert _first_matching_route("POST", "/places").path == "/places"


def test_place_routes_have_their_admission_lanes():
    lanes = {
        (method, route.path): route.capacity_lane
        for route in make_router().routes for method in route.methods
    }
    assert lanes[("GET", "/places")] == "navigation"
    assert lanes[("GET", "/places/{place_id}")] == "foreground"
    assert lanes[("GET", "/places/search")] == "foreground"


def _trip(start, end, miles=1.0, category="business", exclusion=None):
    return {
        "start_place_id": start, "end_place_id": end, "display_distance_m": miles * MILE,
        "category": category, "exclusion": exclusion,
    }


def test_a_trip_counts_once_and_not_my_vehicle_is_left_out():
    totals = place_year_totals([
        _trip(7, 7, 2.0),                       # from and to the place
        _trip(7, 8, 3.0),                       # departure
        _trip(9, 7, 4.0, "unclassified"),       # arrival, unclassified
        _trip(8, 7, 5.0, "personal", "not_deductible"),
        _trip(7, 9, 100.0, exclusion="not_my_vehicle"),
        _trip(8, 9, 50.0),                      # does not touch the place
    ], 7)
    assert totals["trip_count"] == 4
    assert totals["arrivals"] == 3
    assert totals["departures"] == 2
    assert round(totals["total_m"] / MILE, 6) == 14.0
    assert round(totals["unclassified_m"] / MILE, 6) == 4.0


def test_archive_link_filters_by_place_and_the_selected_year():
    assert place_archive_url(7, 2025) == "/trips?from=2025-01-01&to=2025-12-31&place=7"


def _render(name: str, **context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    base = {"user": USER, "csrf": "token", "csp_nonce": "test-nonce", "static_version": "v1"}
    return templates.env.get_template(name).render(**{**base, **context})


def _place_context(trips, rates, year=2026, **overrides):
    context = {
        "place": PLACE, "year": year, "next_year_disabled": False,
        "totals": place_year_totals(trips, PLACE["id"]),
        "report": build_range_report(trips, rates, TZ, date(year, 1, 1), date(year, 12, 31)),
        "rules": [], "trips": [], "vehicles": [], "recent_purposes": [], "has_more": False,
        "archive_url": place_archive_url(PLACE["id"], year),
    }
    context.update(overrides)
    return context


def _report_trip(category, miles, **extra):
    return {
        "started_at": datetime(2026, 3, 15, 12, tzinfo=TZ), "display_distance_m": miles * MILE,
        "category": category, "purpose": "Client visit", "has_gap": False,
        "snap_status": "ok", "source": "detected", "vehicle_id": 1, "vehicle_name": "Truck",
        "start_place_id": 7, "end_place_id": 7, "exclusion": None, **extra,
    }


def test_place_page_renders_map_counts_report_and_links():
    trips = [_report_trip("business", 10), _report_trip("unclassified", 2)]
    body = _render("place.html", **_place_context(trips, {2026: YearRate(0.70)}))
    assert '<div id="place-map" class="place-map"' in body
    assert '<script src="/static/vendor/leaflet/leaflet.js"></script>' in body
    script = body.split('<script nonce="test-nonce">\n  (function () {', 1)[1].split("</script>", 1)[0]
    assert "const center = [37.5, -122.25];" in script
    assert "const radius = 150.0;" in script
    assert script.index("map.setView(") < script.index("L.circle(")
    assert "<span>Trips</span><strong>2</strong>" in body
    assert "<span>Arrivals</span><strong>2</strong>" in body
    assert "<span>Departures</span><strong>2</strong>" in body
    assert "<span>Miles</span><strong>12.0 mi</strong>" in body
    assert "<span>Unclassified miles</span><strong>2.0 mi</strong>" in body
    # The report summary keeps its own rule: unclassified miles stay out.
    assert "<span>Business miles</span><strong>10.0 mi</strong>" in body
    assert "1 trip(s) still unclassified" in body
    assert 'href="/trips?from=2026-01-01&amp;to=2026-12-31&amp;place=7"' in body
    assert 'href="/settings#places-and-rules"' in body
    assert 'href="/places/7?year=2025"' in body
    assert 'href="/places/7?year=2027"' in body


def test_place_page_carries_the_carried_rate_note():
    body = _render("place.html", **_place_context(
        [_report_trip("business", 10)], {2025: YearRate(0.70)},
    ))
    assert "No 2026 mileage rate is saved. 2026 figures use the 2025 rate" in body
    assert '<a href="/settings">Add the 2026 rate</a>.' in body


def test_place_page_disables_next_year_and_omits_an_empty_report():
    body = _render("place.html", **_place_context([], {}, next_year_disabled=True))
    assert '<span class="year-nav-next" aria-disabled="true">2027 →</span>' in body
    assert 'href="/places/7?year=2027"' not in body
    assert "place-report-heading" not in body
    assert "No trips start or end here in 2026." in body
    assert "No auto-tag rule names Office or the Work kind." in body


def test_place_page_has_no_previous_year_link_at_year_one():
    body = _render("place.html", **_place_context([], {}, year=1))
    assert 'href="/places/7?year=0"' not in body
    assert 'href="/places/7?year=2"' in body


def test_place_page_hides_row_selection_even_after_a_row_re_renders():
    # The fragment routes re-render rows without page context, so the hiding
    # must be a page-scoped style, not a template flag.
    from pathlib import Path
    css = (Path(__file__).parents[1] / "static" / "style.css").read_text()
    assert ".place-page .trip-archive-row-selector { display: none; }" in css
    assert '<div class="page place-page">' in _render("place.html", **_place_context([], {}))


def test_place_page_lists_rules():
    rules = [{"a_desc": "Office", "b_desc": "Any home", "category": "business"}]
    body = _render("place.html", **_place_context([], {}, rules=rules))
    assert "<tr><td>Office</td><td>Any home</td><td>Business</td></tr>" in body


def test_places_index_lists_each_place_with_its_page():
    places = [
        {"id": 3, "name": "Home", "kind": "home", "trip_count": 4, "total_m": 8 * MILE,
         "last_visit": datetime(2026, 2, 1, 18, tzinfo=TZ)},
        {"id": 7, "name": "Office", "kind": "work", "trip_count": 0, "total_m": 0.0, "last_visit": None},
    ]
    body = _render("places.html", places=places, year=2026)
    assert '<td><a href="/places/3">Home</a></td>' in body
    assert "<td>Home</td>" in body
    assert '<td class="num">4</td>' in body
    assert '<td class="num">8.0</td>' in body
    assert ">Feb 1, 2026</time>" in body
    assert '<span class="muted">Never</span>' in body
    assert "Trips in 2026" in body


def test_places_index_without_places_points_to_settings():
    body = _render("places.html", places=[], year=2026)
    assert "No saved places yet." in body
    assert 'href="/settings#places-and-rules"' in body


def test_stats_most_used_places_says_it_counts_trip_ends():
    from app.stats import Dashboard

    stats = Dashboard(
        year=2026, trip_count=1, business_m=1.0, personal_m=0.0, unclassified_m=0.0,
        unclassified_trips=0, weekly_chart="", monthly_chart="", routes=[],
        places=[{"name": "Home", "visit_count": 2}], unnamed_trip_count=0,
    )
    body = _render(
        "stats.html", stats=stats, filter_year=2026, filter_from="", filter_to="",
        filter_vehicle="", vehicles=[], next_year_disabled=False,
        period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        multiyear=None, trend=None, vehicle_breakdown=None,
    )
    section = body.split("<h2>Most-used places</h2>", 1)[1].split("</section>", 1)[0]
    assert '<td class="num">2</td>' in section
    assert "Counts trip ends, so a trip from and to one place counts twice." in section
