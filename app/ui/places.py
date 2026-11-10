from __future__ import annotations

from app.page import render_page, render_template

import logging
from datetime import date, datetime

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from psycopg import errors
from psycopg.rows import dict_row

from app.account_context import account_id
from app.auth import require_csrf, require_user
from app.capacity import owned_thread
from app.detector.lock import lock_detector
from app.detector.runner import reprocess_places_in
from app.places_desc import PLACE_KINDS
from app.rates import load_rates
from app.report import build_range_report
from app.trip_queries import DISPLAY_DISTANCE_SQL, TRIP_COLUMNS
from app.validation import parse_finite_number
from app.vehicles import list_vehicles

from app.ui._common import (
    RULE_CATEGORIES, _fetch_recent_purposes, _poke_snap_worker, _redirect_back, _url_with_filters,
)

log = logging.getLogger(__name__)


async def _fetch_places_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, name, kind::text AS kind, "
        " ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon, radius_m "
        "FROM places WHERE account_id = %s ORDER BY name", (account_id(conn),)
    )
    return await cur.fetchall()


def _side_desc(place_id: int | None, kind: str | None, place_names: dict[int, str]) -> str:
    if place_id is not None:
        return place_names.get(place_id, f"place #{place_id}")
    if kind is not None:
        return f"Any {kind}"
    return "Any place"


async def _fetch_rules_rows(conn, places: list[dict]) -> list[dict]:
    place_names = {p["id"]: p["name"] for p in places}
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules WHERE account_id = %s ORDER BY id",
        (account_id(conn),),
    )
    rows = await cur.fetchall()
    for row in rows:
        row["a_desc"] = _side_desc(row["a_place"], row["a_kind"], place_names)
        row["b_desc"] = _side_desc(row["b_place"], row["b_kind"], place_names)
    return rows


def _year_bounds(year: int, tz) -> tuple[datetime, datetime]:
    # Local midnights, the same bounds the archive's from/to and the report use.
    return datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz)


async def _fetch_place_index_rows(conn, year_start: datetime, year_end: datetime) -> list[dict]:
    """Every place with this year's trips and miles, and its last visit.

    The OR join meets a trip once per place, so a trip from and to one place
    counts once. Last visit is all time: the latest arrival or departure.
    """
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT places.id, places.name, places.kind::text AS kind, "
        " count(trips.id) FILTER (WHERE trips.started_at >= %s AND trips.started_at < %s) AS trip_count, "
        f" COALESCE(SUM({DISPLAY_DISTANCE_SQL}) FILTER (WHERE trips.started_at >= %s AND trips.started_at < %s), 0) AS total_m, "
        " max(CASE WHEN trips.end_place_id = places.id THEN trips.ended_at ELSE trips.started_at END) AS last_visit "
        "FROM places LEFT JOIN trips ON trips.account_id = places.account_id "
        " AND (trips.start_place_id = places.id OR trips.end_place_id = places.id) "
        " AND trips.exclusion IS DISTINCT FROM 'not_my_vehicle' "
        "WHERE places.account_id = %s GROUP BY places.id ORDER BY places.name",
        (year_start, year_end, year_start, year_end, account_id(conn)),
    )
    rows = await cur.fetchall()
    for row in rows:
        row["total_m"] = float(row["total_m"])
    return rows


async def _fetch_place(conn, place_id: int) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, name, kind::text AS kind, "
        " ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon, radius_m "
        "FROM places WHERE id = %s AND account_id = %s",
        (place_id, account_id(conn)),
    )
    return await cur.fetchone()


async def _fetch_place_trips(conn, place_id: int, year_start: datetime, year_end: datetime) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips WHERE account_id = %s "
        "AND (start_place_id = %s OR end_place_id = %s) "
        "AND started_at >= %s AND started_at < %s ORDER BY started_at DESC, id DESC",
        (account_id(conn), place_id, place_id, year_start, year_end),
    )
    return await cur.fetchall()


async def _fetch_place_rules(conn, place: dict, places: list[dict]) -> list[dict]:
    """Rules that name the place or its kind on either side. A wildcard
    side is NULL in both columns, so it never matches."""
    place_names = {p["id"]: p["name"] for p in places}
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules WHERE account_id = %s "
        "AND (a_place = %s OR b_place = %s OR a_kind::text = %s OR b_kind::text = %s) ORDER BY id",
        (account_id(conn), place["id"], place["id"], place["kind"], place["kind"]),
    )
    rows = await cur.fetchall()
    for row in rows:
        row["a_desc"] = _side_desc(row["a_place"], row["a_kind"], place_names)
        row["b_desc"] = _side_desc(row["b_place"], row["b_kind"], place_names)
    return rows


def place_year_totals(trips: list[dict], place_id: int) -> dict:
    """A place page's counts: each trip touching the place once, arrivals
    ending there and departures starting there, so a trip from and to the
    place is one trip, one arrival and one departure. `not_my_vehicle` trips
    are left out; every category is in the miles."""
    totals = {"trip_count": 0, "arrivals": 0, "departures": 0, "total_m": 0.0, "unclassified_m": 0.0}
    for trip in trips:
        if trip.get("exclusion") == "not_my_vehicle":
            continue
        starts = trip.get("start_place_id") == place_id
        ends = trip.get("end_place_id") == place_id
        if not (starts or ends):
            continue
        distance = float(trip["display_distance_m"] or 0.0)
        totals["trip_count"] += 1
        totals["arrivals"] += ends
        totals["departures"] += starts
        totals["total_m"] += distance
        if trip.get("category") == "unclassified":
            totals["unclassified_m"] += distance
    return totals


def place_archive_url(place_id: int, year: int) -> str:
    return _url_with_filters(
        "/trips", date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat(), "", "", str(place_id)
    )


async def _fetch_boundary_overrides_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, device, kind::text AS kind, range_start, range_end, point_id, created_at "
        "FROM trip_boundary_overrides WHERE account_id = %s ORDER BY created_at DESC",
        (account_id(conn),),
    )
    return await cur.fetchall()


def register_boundary_override(router: APIRouter) -> None:
        @router.post("/settings/boundary_overrides/{override_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_boundary_override(
            request: Request, override_id: int, user: dict = Depends(require_user)
        ):
            """Same single-transaction treatment as `split_trip`: the override
            delete and the reprocess it triggers share one connection, so a
            failed reprocess doesn't leave the override gone with the device's
            trips still reflecting it.
            """
            runner = request.state.detector_runner
            reprocessed = False
            async with request.state.account_pool.connection() as conn:
                # Detector lock precedes writes and the reprocess they trigger.
                await lock_detector(conn)
                cur = await conn.execute(
                    "DELETE FROM trip_boundary_overrides WHERE id = %s AND account_id = %s RETURNING tracking_device_id",
                    (override_id, account_id(conn)),
                )
                row = await cur.fetchone()
                if row:
                    await runner.reprocess_device_in(conn, row[0])
                    reprocessed = True
            request.state._capacity_mutation_committed = True
            if reprocessed:
                _poke_snap_worker(request)
            return _redirect_back(request)


def register(router: APIRouter) -> None:
        @router.get("/places/search")
        async def search_places(
            request: Request, q: str = Query(""), user: dict = Depends(require_user)
        ):
            cfg = request.state.config
            results = []
            q = q.strip()
            provider = cfg.geocode_provider
            if provider is not None and q:
                try:
                    async with request.app.state.geocode_pacer.request():
                        results = await provider.autocomplete(request.app.state.geocode_http_client, q)
                except (httpx.HTTPError, ValueError) as e:
                    # Not str(e): the provider's request carries both its API
                    # key and the user's typed search text as query parameters,
                    # and a raise_for_status() HTTPStatusError's message embeds
                    # the full URL it failed against.
                    log.warning("address search failed: %s", type(e).__name__)
            return await render_template(
                request, "_address_results.html", {"results": results}
            )

        @router.post("/places", dependencies=[Depends(require_csrf)])
        async def create_place(
            request: Request,
            name: str = Form(...),
            kind: str = Form(...),
            lat: float = Form(...),
            lon: float = Form(...),
            radius_m: float = Form(150.0),
            user: dict = Depends(require_user),
        ):
            name = name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name required")
            if kind not in PLACE_KINDS:
                raise HTTPException(status_code=400, detail="Unknown kind")
            parsed_radius = parse_finite_number(radius_m)
            if parsed_radius is None or parsed_radius <= 0:
                raise HTTPException(status_code=400, detail="Radius must be positive")
            radius_m = parsed_radius
            # The geography cast below silently coerces an out-of-range
            # coordinate rather than rejecting it, so the range must be enforced
            # here or a bad lat/lon reaches the database wrong instead of refused.
            parsed_lat = parse_finite_number(lat, minimum=-90, maximum=90)
            parsed_lon = parse_finite_number(lon, minimum=-180, maximum=180)
            if parsed_lat is None or parsed_lon is None:
                raise HTTPException(status_code=400, detail="Invalid coordinates")
            lat, lon = parsed_lat, parsed_lon
            async with request.state.account_pool.connection() as conn:
                # Detector lock precedes writes and the reprocess they trigger.
                await lock_detector(conn)
                try:
                    await conn.execute(
                        "INSERT INTO places (account_id, name, kind, geom, radius_m) "
                        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
                        (account_id(conn), name, kind, lon, lat, radius_m),
                    )
                except errors.UniqueViolation:
                    raise HTTPException(status_code=400, detail="A place with that name already exists")
                await reprocess_places_in(conn)
            request.state._capacity_mutation_committed = True
            return _redirect_back(request)

        @router.post("/places/{place_id}/update", dependencies=[Depends(require_csrf)])
        async def update_place(
            request: Request,
            place_id: int,
            name: str = Form(...),
            kind: str = Form(...),
            lat: float = Form(...),
            lon: float = Form(...),
            radius_m: float = Form(...),
            user: dict = Depends(require_user),
        ):
            name = name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name required")
            if kind not in PLACE_KINDS:
                raise HTTPException(status_code=400, detail="Unknown kind")
            parsed_radius = parse_finite_number(radius_m)
            if parsed_radius is None or parsed_radius <= 0:
                raise HTTPException(status_code=400, detail="Radius must be positive")
            radius_m = parsed_radius
            # The geography cast below silently coerces an out-of-range
            # coordinate rather than rejecting it, so the range must be enforced
            # here or a bad lat/lon reaches the database wrong instead of refused.
            parsed_lat = parse_finite_number(lat, minimum=-90, maximum=90)
            parsed_lon = parse_finite_number(lon, minimum=-180, maximum=180)
            if parsed_lat is None or parsed_lon is None:
                raise HTTPException(status_code=400, detail="Invalid coordinates")
            lat, lon = parsed_lat, parsed_lon
            async with request.state.account_pool.connection() as conn:
                # Detector lock precedes writes and the reprocess they trigger.
                await lock_detector(conn)
                try:
                    await conn.execute(
                        "UPDATE places SET name = %s, kind = %s, "
                        " geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, radius_m = %s "
                        "WHERE id = %s AND account_id = %s",
                        (name, kind, lon, lat, radius_m, place_id, account_id(conn)),
                    )
                except errors.UniqueViolation:
                    raise HTTPException(status_code=400, detail="A place with that name already exists")
                await reprocess_places_in(conn)
            request.state._capacity_mutation_committed = True
            return _redirect_back(request)

        @router.post("/places/{place_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_place(request: Request, place_id: int, user: dict = Depends(require_user)):
            async with request.state.account_pool.connection() as conn:
                # Detector lock precedes writes and the reprocess they trigger.
                await lock_detector(conn)
                await conn.execute("DELETE FROM places WHERE id = %s AND account_id = %s", (place_id, account_id(conn)))
                await reprocess_places_in(conn)
            request.state._capacity_mutation_committed = True
            return _redirect_back(request)

        @router.post("/rules", dependencies=[Depends(require_csrf)])
        async def create_rule(
            request: Request,
            a_mode: str = Form(...),
            a_kind: str = Form(""),
            a_place: str = Form(""),
            b_mode: str = Form(...),
            b_kind: str = Form(""),
            b_place: str = Form(""),
            category: str = Form(...),
            user: dict = Depends(require_user),
        ):
            if category not in RULE_CATEGORIES:
                raise HTTPException(status_code=400, detail="Unknown category")

            def resolve(mode: str, kind: str, place: str) -> tuple[int | None, str | None]:
                if mode == "place":
                    if not place:
                        raise HTTPException(status_code=400, detail="Select a place")
                    try:
                        return int(place), None
                    except ValueError:
                        raise HTTPException(status_code=400, detail="Invalid place")
                if mode == "kind":
                    if kind not in PLACE_KINDS:
                        raise HTTPException(status_code=400, detail="Unknown kind")
                    return None, kind
                return None, None

            a_place_id, a_kind_val = resolve(a_mode, a_kind, a_place)
            b_place_id, b_kind_val = resolve(b_mode, b_kind, b_place)
            if a_place_id is None and a_kind_val is None and b_place_id is None and b_kind_val is None:
                raise HTTPException(
                    status_code=400, detail="At least one side must be a specific place or a kind"
                )

            async with request.state.account_pool.connection() as conn:
                # Detector lock precedes writes and the reprocess they trigger.
                await lock_detector(conn)
                try:
                    await conn.execute(
                        "INSERT INTO tag_rules (account_id, a_place, a_kind, b_place, b_kind, category) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (account_id(conn), a_place_id, a_kind_val, b_place_id, b_kind_val, category),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such place")
                await reprocess_places_in(conn)
            request.state._capacity_mutation_committed = True
            return _redirect_back(request)

        @router.post("/rules/{rule_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_rule(request: Request, rule_id: int, user: dict = Depends(require_user)):
            async with request.state.account_pool.connection() as conn:
                # Detector lock precedes writes and the reprocess they trigger.
                await lock_detector(conn)
                await conn.execute("DELETE FROM tag_rules WHERE id = %s AND account_id = %s", (rule_id, account_id(conn)))
                await reprocess_places_in(conn)
            request.state._capacity_mutation_committed = True
            return _redirect_back(request)

        # After /places/search, which /places/{place_id} would capture, and
        # after the writes: route tests find POST /places by path alone.
        @router.get("/places")
        async def places_index(request: Request, user: dict = Depends(require_user)):
            tz = request.state.config.display_tz
            year = datetime.now(tz).year
            async with request.state.account_pool.connection() as conn:
                places = await _fetch_place_index_rows(conn, *_year_bounds(year, tz))
            return await render_page(
                request, "places.html", {"user": user, "places": places, "year": year},
            )

        @router.get("/places/{place_id}")
        async def place_page(
            request: Request,
            place_id: int,
            year: int | None = Query(None, ge=1, le=9998),
            user: dict = Depends(require_user),
        ):
            tz = request.state.config.display_tz
            now = datetime.now(tz)
            selected_year = year or now.year
            async with request.state.account_pool.connection() as conn:
                place = await _fetch_place(conn, place_id)
                if place is None:
                    raise HTTPException(status_code=404, detail="Place not found")
                trips = await _fetch_place_trips(conn, place_id, *_year_bounds(selected_year, tz))
                rates = await load_rates(conn)
                rules = await _fetch_place_rules(conn, place, await _fetch_places_rows(conn))
                vehicles = await list_vehicles(conn)
                recent_purposes = await _fetch_recent_purposes(conn)
            # The report keeps its own rule (unclassified miles outside its
            # total); the page's totals beside it include them.
            report = await owned_thread(
                build_range_report, trips, rates, tz,
                date(selected_year, 1, 1), date(selected_year, 12, 31),
            )
            return await render_page(
                request, "place.html", {
                    "user": user,
                    "place": place,
                    "year": selected_year,
                    "next_year_disabled": selected_year >= now.year,
                    "totals": place_year_totals(trips, place_id),
                    "report": report,
                    "rules": rules,
                    "trips": trips,
                    "vehicles": vehicles,
                    "recent_purposes": recent_purposes,
                    "has_more": False,
                    "archive_url": place_archive_url(place_id, selected_year),
                },
            )
