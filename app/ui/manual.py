from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.auth import require_csrf, require_user
from app.formatting import format_miles
from app.page import render_page
from app.rates import METERS_PER_MILE
from app.snap import route_distance_m, route_line
from app.validation import parse_finite_number
from app.vehicles import list_vehicles

from app.ui._common import (
    CATEGORIES,
    EXCLUSIONS,
    ManualTripValidationError,
    _fetch_recent_purposes,
    _fetch_trip,
    _parse_vehicle_form,
    normalize_trip_label,
)
from app.ui.places import _fetch_places_rows

log = logging.getLogger(__name__)

# The only value /trips?notice=... is ever allowed to mean something:
# whitelisted (never reflected as-is) so an arbitrary query string can't
# echo attacker-controlled text onto the page.
MANUAL_ROUTE_UNAVAILABLE_NOTICE = "route_unavailable"


def _local_time_is_real(naive: datetime, tz: ZoneInfo) -> bool:
    """A spring-forward gap wall time (e.g. 02:30 when clocks jump 02:00 to
    03:00) has no corresponding instant, so attaching a timezone to it and
    normalizing through UTC changes the wall clock. A fall-back ambiguous
    time (occurs twice) round-trips unchanged and must stay accepted.
    """
    aware = naive.replace(tzinfo=tz)
    return aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive


def parse_manual_trip_input(
    date_value: str,
    start_time: str,
    end_time: str,
    distance: str,
    tz: ZoneInfo,
    *,
    distance_optional: bool = False,
) -> tuple[datetime, datetime, float | None]:
    """Interpret manual trip fields once for create and edit.

    The browser submits wall-clock values without an offset. Attaching the
    configured display timezone, rather than the server or browser timezone,
    keeps a later edit from moving a trip across a local reporting boundary.
    End times at or before the start represent an overnight trip, matching the
    original manual-entry contract. Distance is parsed from text so NaN and
    infinity cannot bypass a simple positive-number comparison.

    `distance_optional` defaults False so trip-edit's existing required-
    distance contract (and its tests) are untouched; a routed manual trip
    passes True to let a blank distance mean "use the routed distance"
    instead of a validation error, returning None for it rather than 0 or
    NaN so the caller can't mistake "not supplied" for a real value.
    """
    errors: dict[str, str] = {}
    try:
        naive_start = datetime.fromisoformat(f"{date_value}T{start_time}")
    except ValueError:
        naive_start = None
        if not date_value:
            errors["date"] = "Enter a date."
        elif not start_time:
            errors["start_time"] = "Enter a start time."
        else:
            errors["date"] = "Enter a valid date and start time."
    else:
        if not _local_time_is_real(naive_start, tz):
            errors["start_time"] = (
                "That time does not exist on this date (clocks skip forward for "
                "daylight saving). Enter a later time."
            )
    started_at = naive_start.replace(tzinfo=tz) if naive_start is not None else None
    try:
        naive_end = datetime.fromisoformat(f"{date_value}T{end_time}")
    except ValueError:
        naive_end = None
        errors["end_time"] = "Enter a valid end time."
    else:
        if not _local_time_is_real(naive_end, tz):
            errors["end_time"] = (
                "That time does not exist on this date (clocks skip forward for "
                "daylight saving). Enter a later time."
            )
    ended_at = naive_end.replace(tzinfo=tz) if naive_end is not None else None
    distance_miles: float | None
    # A direct call that bypasses form coercion (rather than going through
    # FastAPI) can still hand this a float instead of the submitted string;
    # only a string can be blank, so anything else falls through to the
    # numeric parse below as it always has.
    if distance_optional and isinstance(distance, str) and not distance.strip():
        distance_miles = None
    else:
        try:
            distance_miles = float(distance)
        except (TypeError, ValueError):
            distance_miles = math.nan
        if not math.isfinite(distance_miles) or distance_miles <= 0:
            errors["distance"] = "Distance must be a positive finite number."
    if errors:
        raise ManualTripValidationError(errors)
    assert started_at is not None and ended_at is not None
    if ended_at <= started_at:
        ended_at += timedelta(days=1)
    if distance_miles is None:
        distance_m = None
    else:
        distance_m = distance_miles * METERS_PER_MILE
        if not math.isfinite(distance_m):
            raise ManualTripValidationError(
                {"distance": "Distance must be a positive finite number."}
            )
    return started_at, ended_at, distance_m


@dataclass(frozen=True)
class _ManualRouteEndpoints:
    """Validated routing endpoints for a manual trip, resolved from
    whichever `route_mode` the form submitted. `start_place_id`/
    `end_place_id` are only set for `route_mode == "places"` -- a
    map-picked endpoint has no place to attribute the trip to.
    """
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    start_place_id: int | None
    end_place_id: int | None


def _parse_manual_route_coord(value: str, *, minimum: float, maximum: float) -> float:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid route coordinate")
    parsed = parse_finite_number(raw, minimum=minimum, maximum=maximum)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid route coordinate")
    return parsed


async def _resolve_manual_route_endpoints(
    conn, route_mode: str, start_place: str, end_place: str,
    start_lat: str, start_lon: str, end_lat: str, end_lon: str,
) -> _ManualRouteEndpoints | None:
    """Turn a manual-trip route selection into validated endpoint
    coordinates. Returns None for `route_mode == "none"` (no routing at
    all, the caller's cue to skip OSRM entirely). Raises HTTPException(400)
    for anything unparseable, unselected, or out of range -- unlike a
    detected trip's coordinates, these come straight from user-controlled
    form fields with no GPS-pipeline validation behind them, and the
    message never echoes the submitted value back (an unknown place id or
    a bad coordinate string is not safe to reflect into a response body).

    Never touches OSRM itself: the caller must release this connection
    before routing, so a slow or hung outbound OSRM call never holds a
    pooled database connection.
    """
    if route_mode == "none":
        return None
    if route_mode == "places":
        try:
            start_id = int(start_place)
            end_id = int(end_place)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Select a start and end place")
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon "
            "FROM places WHERE id = ANY(%s)",
            ([start_id, end_id],),
        )
        rows = {row["id"]: row for row in await cur.fetchall()}
        if start_id not in rows or end_id not in rows:
            raise HTTPException(status_code=400, detail="Unknown place selected")
        return _ManualRouteEndpoints(
            from_lat=rows[start_id]["lat"], from_lon=rows[start_id]["lon"],
            to_lat=rows[end_id]["lat"], to_lon=rows[end_id]["lon"],
            start_place_id=start_id, end_place_id=end_id,
        )
    if route_mode == "map":
        return _ManualRouteEndpoints(
            from_lat=_parse_manual_route_coord(start_lat, minimum=-90, maximum=90),
            from_lon=_parse_manual_route_coord(start_lon, minimum=-180, maximum=180),
            to_lat=_parse_manual_route_coord(end_lat, minimum=-90, maximum=90),
            to_lon=_parse_manual_route_coord(end_lon, minimum=-180, maximum=180),
            start_place_id=None, end_place_id=None,
        )
    raise HTTPException(status_code=400, detail="Unknown route mode")


async def _resolve_missing_trip_osrm_hint(request: Request, bridge_trip: str) -> str | None:
    """The missing-trip badge's road-distance suggestion, resolved only when
    a badge's prefill link is followed, never per trip-list row. `bridge_trip`
    carries the later trip's id so this can re-fetch the coordinates itself
    rather than trusting lat/lon round-tripped through the URL. Degrades to
    `None` (no hint, not an error) on a malformed/stale id, missing OSRM
    config, missing coordinates, or any transport failure.
    """
    cfg = request.app.state.config
    http_client = request.app.state.osrm_http_client
    if not cfg.osrm_url or http_client is None:
        return None
    try:
        trip_id = int(bridge_trip)
    except ValueError:
        return None
    try:
        trip = await _fetch_trip(request.app.state.pool, trip_id)
    except HTTPException:
        return None
    from_lat, from_lon = trip.get("prev_trip_end_lat"), trip.get("prev_trip_end_lon")
    to_lat, to_lon = trip.get("start_lat"), trip.get("start_lon")
    if None in (from_lat, from_lon, to_lat, to_lon):
        return None
    try:
        distance_m = await route_distance_m(
            http_client, cfg.osrm_url, from_lat, from_lon, to_lat, to_lon,
        )
    except (httpx.HTTPError, ValueError) as e:
        # Not str(e): route_distance_m builds the request URL from the two
        # trip endpoints' exact coordinates, and a raise_for_status()
        # HTTPStatusError's message embeds the full URL it failed against.
        # The exception type is enough to know the suggestion failed.
        log.warning("missing-trip OSRM route suggestion failed: %s", type(e).__name__)
        return None
    if distance_m is None:
        return None
    return f"~{format_miles(distance_m)} mi by road"


def register(router: APIRouter) -> None:
        @router.get("/trips/manual")
        async def manual_trip_page(
            request: Request,
            user: dict = Depends(require_user),
            manual_date: str = Query(""),
            manual_start: str = Query(""),
            manual_notes: str = Query(""),
            bridge_trip: str = Query(""),
        ):
            manual_prefill = None
            if manual_date or manual_start or manual_notes:
                manual_prefill = {
                    "date": manual_date,
                    "start_time": manual_start,
                    "notes": manual_notes,
                    "osrm_hint": await _resolve_missing_trip_osrm_hint(
                        request, bridge_trip
                    ) if bridge_trip else None,
                }
            async with request.app.state.pool.connection() as conn:
                vehicles = await list_vehicles(conn)
                recent_purposes = await _fetch_recent_purposes(conn)
                places = await _fetch_places_rows(conn)
            return await render_page(
                request,
                "manual_trip.html",
                {
                    "user": user,
                    "csrf": request.session.get("csrf", ""),
                    "vehicles": vehicles,
                    "recent_purposes": recent_purposes,
                    "places": places,
                    "manual_prefill": manual_prefill,
                },
            )

        @router.post("/trips/manual/route-preview", dependencies=[Depends(require_csrf)])
        async def manual_route_preview(
            request: Request,
            route_mode: str = Form("none"),
            start_place: str = Form(""),
            end_place: str = Form(""),
            start_lat: str = Form(""),
            start_lon: str = Form(""),
            end_lat: str = Form(""),
            end_lon: str = Form(""),
            user: dict = Depends(require_user),
        ):
            """Preview-only: resolves and routes, but never writes anything.
            add_manual_trip below re-resolves and re-routes from scratch on
            submission rather than trusting anything from this response, so a
            stale or tampered preview can, at worst, mislead the form's display,
            never the saved trip.

            Never leaks the OSRM base URL, an exception message, or the
            submitted coordinates back to the browser on failure -- `ok: false`
            with a plain "unavailable" reason is all a caller ever needs to
            degrade the form to a manual distance entry.
            """
            async with request.app.state.pool.connection() as conn:
                endpoints = await _resolve_manual_route_endpoints(
                    conn, route_mode, start_place, end_place,
                    start_lat, start_lon, end_lat, end_lon,
                )
            if endpoints is None:
                raise HTTPException(status_code=400, detail="Select a route to preview")

            cfg = request.app.state.config
            http_client = request.app.state.osrm_http_client
            if not cfg.osrm_url or http_client is None:
                return JSONResponse({"ok": False, "reason": "unavailable"})
            try:
                routed = await route_line(
                    http_client, cfg.osrm_url,
                    endpoints.from_lat, endpoints.from_lon,
                    endpoints.to_lat, endpoints.to_lon,
                )
            except (httpx.HTTPError, ValueError) as e:
                # Not str(e): route_line builds its request URL from these exact
                # coordinates, and a raise_for_status() HTTPStatusError's message
                # embeds the full URL it failed against -- same reasoning as
                # _resolve_missing_trip_osrm_hint.
                log.warning("manual route preview OSRM call failed: %s", type(e).__name__)
                return JSONResponse({"ok": False, "reason": "unavailable"})
            if routed is None:
                return JSONResponse({"ok": False, "reason": "unavailable"})

            return JSONResponse({
                "ok": True,
                "distance_m": routed.distance_m,
                "distance_miles": format_miles(routed.distance_m),
                "geometry": routed.geojson,
                "start": [endpoints.from_lat, endpoints.from_lon],
                "end": [endpoints.to_lat, endpoints.to_lon],
            })

        @router.post("/trips/manual", dependencies=[Depends(require_csrf)])
        async def add_manual_trip(
            request: Request,
            date: str = Form(...),
            start_time: str = Form(...),
            end_time: str = Form(...),
            distance: str = Form(...),
            category: str = Form("unclassified"),
            purpose: str = Form(""),
            notes: str = Form(""),
            vehicle_id: str = Form(""),
            route_mode: str = Form("none"),
            start_place: str = Form(""),
            end_place: str = Form(""),
            start_lat: str = Form(""),
            start_lon: str = Form(""),
            end_lat: str = Form(""),
            end_lon: str = Form(""),
            routed_distance: str = Form(""),
            user: dict = Depends(require_user),
            exclusion: str = Form(""),
            start_label: str | None = Form(None),
            end_label: str | None = Form(None),
        ):
            exclusion = exclusion if isinstance(exclusion, str) else ""
            # Same defensive coercion as exclusion above: a caller that
            # invokes this endpoint function directly (bypassing FastAPI's
            # request parsing, as several existing DB tests do) without
            # knowing about these two newer fields gets `Form()`'s own
            # sentinel object here, not a string. Coerced to None (absent),
            # matching what a real request omitting the field would produce
            # -- on create, absent and blank both mean "no label" anyway
            # (see the `raw_value or ""` below), so this is only about the
            # two handlers treating a missing field the same way.
            start_label = start_label if isinstance(start_label, str) else None
            end_label = end_label if isinstance(end_label, str) else None
            tz = request.app.state.config.display_tz
            routing_active = route_mode != "none"
            try:
                started_at, ended_at, distance_m = parse_manual_trip_input(
                    date, start_time, end_time, distance, tz,
                    distance_optional=routing_active,
                )
            except ManualTripValidationError as exc:
                if "distance" in exc.errors and len(exc.errors) == 1:
                    raise HTTPException(status_code=400, detail="Invalid distance")
                raise HTTPException(status_code=400, detail="Invalid date/time")
            if category not in CATEGORIES:
                raise HTTPException(status_code=400, detail="Unknown category")
            if exclusion and exclusion not in EXCLUSIONS:
                raise HTTPException(status_code=400, detail="Unknown exclusion")
            parsed_vehicle_id = _parse_vehicle_form(vehicle_id)

            # Labels only describe an endpoint that has no other name for
            # itself (see migrations/025_manual_trip_labels.sql): a named
            # place already has a stable name and a map-picked point already
            # has coordinates to reverse-geocode, so any nonblank label
            # submitted alongside a route selection is rejected outright
            # rather than silently dropped. This checks the submitted
            # `route_mode`, not whether routing ends up actually storing
            # geometry -- a route that fails and falls back to a legacy
            # distance-only save (below) was still a route selection.
            normalized_labels: dict[str, str | None] = {}
            for field, raw_value in (("start_label", start_label), ("end_label", end_label)):
                try:
                    # `raw_value or ""`: on create, a field the client never
                    # submitted (None) means the same thing as one it
                    # submitted blank, so both take the same normalize path.
                    normalized_labels[field] = normalize_trip_label(raw_value or "", field)
                except ManualTripValidationError as exc:
                    raise HTTPException(status_code=400, detail=next(iter(exc.errors.values())))
                if normalized_labels[field] is not None and routing_active:
                    raise HTTPException(
                        status_code=400,
                        detail="Location names are only available when there is no route.",
                    )

            path_geojson = None
            start_point = None
            end_point = None
            start_place_id = None
            end_place_id = None
            notice = None

            if routing_active:
                # Resolve first, release the connection, THEN call OSRM: a
                # slow/hung outbound route request must never hold a pooled
                # database connection while it waits.
                async with request.app.state.pool.connection() as conn:
                    endpoints = await _resolve_manual_route_endpoints(
                        conn, route_mode, start_place, end_place,
                        start_lat, start_lon, end_lat, end_lon,
                    )
                if endpoints is None:
                    raise HTTPException(status_code=400, detail="Invalid route selection")

                cfg = request.app.state.config
                http_client = request.app.state.osrm_http_client
                routed = None
                if cfg.osrm_url and http_client is not None:
                    try:
                        routed = await route_line(
                            http_client, cfg.osrm_url,
                            endpoints.from_lat, endpoints.from_lon,
                            endpoints.to_lat, endpoints.to_lon,
                        )
                    except (httpx.HTTPError, ValueError) as e:
                        # Not str(e): see manual_route_preview above for why.
                        log.warning("manual trip routing failed: %s", type(e).__name__)
                        routed = None

                if routed is None:
                    if distance_m is None:
                        raise HTTPException(
                            status_code=400,
                            detail="Automatic routing was unavailable. Enter the distance.",
                        )
                    # Legacy manual trip: no geometry, submitted distance as-is.
                    notice = MANUAL_ROUTE_UNAVAILABLE_NOTICE
                else:
                    # `routed_distance` is the exact value the preview put into
                    # the distance field. Equal to the submitted distance means
                    # the user never touched it (an echo, not an override), so
                    # the freshly re-routed distance wins rather than trusting a
                    # client-submitted number; different (or blank) means the
                    # user deliberately typed their own distance, which is kept
                    # as entered even though the route geometry is still stored.
                    is_echo = False
                    if distance_m is not None:
                        routed_hint = routed_distance.strip()
                        if routed_hint:
                            try:
                                is_echo = float(distance) == float(routed_hint)
                            except ValueError:
                                is_echo = False
                    if distance_m is None or is_echo:
                        distance_m = routed.distance_m
                    path_geojson = routed.geojson
                    start_point = (endpoints.from_lon, endpoints.from_lat)
                    end_point = (endpoints.to_lon, endpoints.to_lat)
                    start_place_id = endpoints.start_place_id
                    end_place_id = endpoints.end_place_id

            async with request.app.state.pool.connection() as conn:
                try:
                    if path_geojson is not None:
                        await conn.execute(
                            "INSERT INTO trips (device, source, started_at, ended_at, distance_m,"
                            " category, exclusion, purpose, notes, vehicle_id, path, start_geom, end_geom,"
                            " start_place_id, end_place_id, snap_status, start_label, end_label)"
                            " VALUES ('manual', 'manual', %s, %s, %s, %s, %s, %s, %s, %s,"
                            " ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),"
                            " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                            " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                            " %s, %s, NULL, %s, %s)",
                            (started_at, ended_at, distance_m, category, exclusion or None,
                             purpose.strip() or None,
                             notes.strip() or None, parsed_vehicle_id,
                             json.dumps(path_geojson),
                             start_point[0], start_point[1],
                             end_point[0], end_point[1],
                             start_place_id, end_place_id,
                             normalized_labels["start_label"], normalized_labels["end_label"]),
                        )
                    else:
                        await conn.execute(
                            "INSERT INTO trips (device, source, started_at, ended_at, distance_m,"
                            " category, exclusion, purpose, notes, vehicle_id, start_label, end_label)"
                            " VALUES ('manual', 'manual', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (started_at, ended_at, distance_m, category, exclusion or None,
                             purpose.strip() or None,
                             notes.strip() or None,
                             parsed_vehicle_id,
                             normalized_labels["start_label"], normalized_labels["end_label"]),
                        )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
            redirect = "/trips" if notice is None else f"/trips?notice={notice}"
            return Response(status_code=204, headers={"HX-Redirect": redirect})
