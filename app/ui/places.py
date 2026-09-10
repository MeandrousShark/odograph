from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from psycopg import errors
from psycopg.rows import dict_row

from app.auth import require_csrf, require_user
from app.detector.runner import reprocess_places_in
from app.places_desc import PLACE_KINDS
from app.validation import parse_finite_number

from app.ui._common import RULE_CATEGORIES, _poke_snap_worker, _redirect_back

log = logging.getLogger(__name__)


async def _fetch_places_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, name, kind::text AS kind, "
        " ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon, radius_m "
        "FROM places ORDER BY name"
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
        " category::text AS category FROM tag_rules ORDER BY id"
    )
    rows = await cur.fetchall()
    for row in rows:
        row["a_desc"] = _side_desc(row["a_place"], row["a_kind"], place_names)
        row["b_desc"] = _side_desc(row["b_place"], row["b_kind"], place_names)
    return rows


async def _fetch_boundary_overrides_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, device, kind::text AS kind, range_start, range_end, point_id, created_at "
        "FROM trip_boundary_overrides ORDER BY created_at DESC"
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
            runner = request.app.state.detector_runner
            reprocessed = False
            async with request.app.state.pool.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM trip_boundary_overrides WHERE id = %s RETURNING device",
                    (override_id,),
                )
                row = await cur.fetchone()
                if row:
                    await runner.reprocess_device_in(conn, row[0])
                    reprocessed = True
            if reprocessed:
                _poke_snap_worker(request)
            return _redirect_back(request)


def register(router: APIRouter) -> None:
        @router.get("/places/search")
        async def search_places(
            request: Request, q: str = Query(""), user: dict = Depends(require_user)
        ):
            cfg = request.app.state.config
            results = []
            q = q.strip()
            provider = cfg.geocode_provider
            if provider is not None and q:
                try:
                    results = await provider.autocomplete(request.app.state.geocode_http_client, q)
                except (httpx.HTTPError, ValueError) as e:
                    # Not str(e): the provider's request carries both its API
                    # key and the user's typed search text as query parameters,
                    # and a raise_for_status() HTTPStatusError's message embeds
                    # the full URL it failed against.
                    log.warning("address search failed: %s", type(e).__name__)
            return request.app.state.templates.TemplateResponse(
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
            async with request.app.state.pool.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO places (name, kind, geom, radius_m) "
                        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
                        (name, kind, lon, lat, radius_m),
                    )
                except errors.UniqueViolation:
                    raise HTTPException(status_code=400, detail="A place with that name already exists")
                await reprocess_places_in(conn)
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
            async with request.app.state.pool.connection() as conn:
                try:
                    await conn.execute(
                        "UPDATE places SET name = %s, kind = %s, "
                        " geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, radius_m = %s "
                        "WHERE id = %s",
                        (name, kind, lon, lat, radius_m, place_id),
                    )
                except errors.UniqueViolation:
                    raise HTTPException(status_code=400, detail="A place with that name already exists")
                await reprocess_places_in(conn)
            return _redirect_back(request)

        @router.post("/places/{place_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_place(request: Request, place_id: int, user: dict = Depends(require_user)):
            async with request.app.state.pool.connection() as conn:
                await conn.execute("DELETE FROM places WHERE id = %s", (place_id,))
                await reprocess_places_in(conn)
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

            async with request.app.state.pool.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO tag_rules (a_place, a_kind, b_place, b_kind, category) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (a_place_id, a_kind_val, b_place_id, b_kind_val, category),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such place")
                await reprocess_places_in(conn)
            return _redirect_back(request)

        @router.post("/rules/{rule_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_rule(request: Request, rule_id: int, user: dict = Depends(require_user)):
            async with request.app.state.pool.connection() as conn:
                await conn.execute("DELETE FROM tag_rules WHERE id = %s", (rule_id,))
                await reprocess_places_in(conn)
            return _redirect_back(request)
