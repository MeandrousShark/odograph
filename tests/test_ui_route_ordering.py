"""Route-ordering regression test: `/report/range` and
`/report/range/export` must stay registered before `/report/{year}` and
`/report/{year}/export` in `app.ui.make_router()`'s route list.

There's no `:int` Starlette path convertor on `{year}` (FastAPI's own int
validation happens later, via dependency injection) — so Starlette's
router matches a bare `{year}` segment against *any* single path component,
`"range"` included, purely on regex shape, before FastAPI ever tries to
parse it as an int. A `/report/{year}` route registered ahead of
`/report/range` therefore swallows the request and 422s on `int("range")`
instead of ever reaching the range handler — confirmed by QA reproducing it
with the order flipped.

`tests/test_report_range_db.py`'s `_endpoint()` helper looks routes up by
exact `route.path` string equality and calls the endpoint function
directly, which bypasses Starlette's dispatch order entirely and so can't
catch a regression here (e.g. someone alphabetizing the routes). This test
instead drives real `Route.matches()` resolution — the same mechanism the
live ASGI app uses per request — with no DB or running server needed, so a
reordering fails loudly here.
"""
from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from starlette.routing import Match

from app.ui import make_router


def _first_matching_route(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path}
    for route in make_router().routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_report_range_route_is_not_swallowed_by_report_year():
    route = _first_matching_route("GET", "/report/range")
    assert route is not None
    assert route.path == "/report/range"


def test_report_range_export_route_is_not_swallowed_by_report_year_export():
    route = _first_matching_route("GET", "/report/range/export")
    assert route is not None
    assert route.path == "/report/range/export"


def test_report_year_route_still_matches_numeric_years():
    # The ordering fix must not come at the cost of the annual route itself
    # — a real year should still resolve to /report/{year}, not get
    # accidentally shadowed by the more specific range routes.
    route = _first_matching_route("GET", "/report/2026")
    assert route is not None
    assert route.path == "/report/{year}"


def test_trip_card_and_edit_routes_resolve_to_literal_actions():
    assert _first_matching_route("GET", "/trips/42/card").path == "/trips/{trip_id}/card"
    assert _first_matching_route("GET", "/trips/42/edit").path == "/trips/{trip_id}/edit"
    assert _first_matching_route("POST", "/trips/42/edit").path == "/trips/{trip_id}/edit"


def test_existing_literal_trip_routes_are_not_swallowed_by_trip_detail():
    assert _first_matching_route("GET", "/trips/month/2026/7").path == "/trips/month/{year}/{month}"
    assert _first_matching_route("POST", "/trips/merge_selected").path == "/trips/merge_selected"
    assert _first_matching_route("POST", "/trips/batch_update").path == "/trips/batch_update"


def test_trips_archive_route_is_not_swallowed_by_trip_detail():
    # `/trips` (the archive, moved off `/`) and
    # `/trips/month/{year}/{month}` must resolve to their own handlers, not
    # `/trips/{trip_id}` -- same swallowing hazard as the other literal
    # `/trips/...` segments above, just for the newly-added archive route.
    assert _first_matching_route("GET", "/trips").path == "/trips"
    assert _first_matching_route("GET", "/trips/month/2026/7").path == "/trips/month/{year}/{month}"


def test_trip_card_routes_keep_ui_auth_and_edit_csrf_dependencies():
    routes = [route for route in make_router().routes if route.path in {
        "/trips/{trip_id}/card", "/trips/{trip_id}/edit",
    }]
    dependencies = {
        (route.path, next(iter(route.methods))): {
            dependency.call.__name__ for dependency in route.dependant.dependencies
        }
        for route in routes
    }

    assert dependencies[("/trips/{trip_id}/card", "GET")] == {"require_user"}
    assert dependencies[("/trips/{trip_id}/edit", "GET")] == {"require_user"}
    assert dependencies[("/trips/{trip_id}/edit", "POST")] == {"require_user", "require_csrf"}


def test_settings_vehicles_auto_assign_route_is_not_swallowed_by_vehicle_id_routes():
    # /settings/vehicles/{vehicle_id}/default (and its /update, /deactivate
    # siblings) are all 4-segment paths, one segment longer than
    # /settings/vehicles/auto_assign, so there's no shared-shape collision
    # like /report/{year} vs /report/range -- this just pins that down.
    route = _first_matching_route("POST", "/settings/vehicles/auto_assign")
    assert route is not None
    assert route.path == "/settings/vehicles/auto_assign"
    dependencies = {
        dependency.call.__name__ for dependency in route.dependant.dependencies
    }
    assert dependencies == {"require_user", "require_csrf"}


def test_trip_thumbnail_route_is_gone():
    # The tileless-thumbnail endpoint was
    # removed as dead code once every list-page caller was gone. No route
    # matches the old path at all, so Starlette's default 404 fires before
    # any dependency (auth included) ever runs -- no DB or session needed to
    # prove this, unlike the thumbnail route's own now-deleted tests.
    assert _first_matching_route("GET", "/trips/42/thumb.svg") is None

    app = FastAPI()
    app.include_router(make_router())
    transport = httpx.ASGITransport(app=app)

    async def _get() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/trips/42/thumb.svg")

    response = asyncio.run(_get())
    assert response.status_code == 404
