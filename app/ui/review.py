from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import Response

from app.account_context import account_id
from app.auth import require_csrf, require_user
from app.page import render_page
from app.vehicles import list_vehicles

from app.ui._common import (
    EXCLUSIONS,
    TRIP_COLUMNS,
    _fetch_recent_purposes,
    _parse_vehicle_form,
    _parse_vehicle_id,
    _trip_filter_sql,
    _url_with_filters,
    parse_date_range,
)
from app.ui.trips import _apply_human_tag, _delete_trip_in, _trip_position


def _review_url(from_str: str, to_str: str, vehicle_str: str, q_str: str = "") -> str:
    """`/review` link carrying the current filters. No category param:
    `/review` always pins category to unclassified.
    """
    return _url_with_filters("/review", from_str, to_str, vehicle_str, q_str)


async def _fetch_review_card(
    conn, where: str, params: list, cursor: tuple[datetime, int] | None,
    *, inclusive: bool = False,
) -> dict:
    """One review card (or the lack of one): the oldest unclassified trip
    matching the filters, strictly after `cursor` (None = fresh `/review`
    load). `remaining` counts matches at or after the returned trip, so
    "N remaining" includes the trip on screen. `state` distinguishes an
    empty filtered set ("empty": nothing ever matched) from an exhausted
    pass ("done": the cursor ran out but a fresh load would find trips),
    review.html renders the two differently.

    `inclusive=True` switches the cursor comparison to "at or after", so the
    trip named by `cursor` itself can be the one returned. Every advancing
    caller (skip, tag-and-advance, delete) leaves this False, matching the
    "strictly after" contract above; only undo passes `inclusive=True`, with
    the undone trip's own position as cursor, to bring that exact trip back
    to the card instead of the trip after it.
    """
    where += " AND" if where else "WHERE"
    where += " trips.account_id = %s"
    params = [*params, account_id(conn)]
    extra_where, extra_params = "", []
    if cursor is not None:
        op = ">=" if inclusive else ">"
        extra_where = f" AND (started_at, id) {op} (%s, %s)"
        extra_params = list(cursor)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {TRIP_COLUMNS} FROM trips {where}{extra_where} "
        "ORDER BY started_at, id LIMIT 1",
        [*params, *extra_params],
    )
    trip = await cur.fetchone()
    if trip is None:
        return {
            "trip": None, "remaining": 0,
            "state": "empty" if cursor is None else "done",
            "path_geojson": None, "path_snapped_geojson": None,
        }
    remaining_cur = await conn.execute(
        f"SELECT count(*) FROM trips {where} AND (started_at, id) >= (%s, %s)",
        [*params, trip["started_at"], trip["id"]],
    )
    remaining = (await remaining_cur.fetchone())[0]
    path_geojson = path_snapped_geojson = None
    if trip["source"] == "detected":
        path_cur = await conn.execute(
            "SELECT ST_AsGeoJSON(path), ST_AsGeoJSON(path_snapped) FROM trips WHERE id = %s AND account_id = %s",
            (trip["id"], account_id(conn)),
        )
        row = await path_cur.fetchone()
        if row:
            path_geojson, path_snapped_geojson = row
    return {
        "trip": trip, "remaining": remaining, "state": "card",
        "path_geojson": path_geojson, "path_snapped_geojson": path_snapped_geojson,
    }


def _review_filter_sql(
    request: Request, from_: str, to: str, vehicle: str, q: str = ""
) -> tuple[str, list]:
    """The unclassified-pinned filter every `/review` route shares."""
    tz = request.state.config.display_tz
    from_dt, to_dt = parse_date_range(from_, to, tz)
    return _trip_filter_sql(
        "unclassified", from_dt, to_dt, _parse_vehicle_id(vehicle), q=q,
        owner_id=request.state.principal.account_id,
    )


async def _render_review_card(
    request: Request, conn, template: str, card: dict,
    from_: str, to: str, vehicle: str, q: str = "", **extra,
):
    """Render a review card with the shared context (vehicles, purpose
    suggestions, current filters) every `/review` route passes identically.
    """
    vehicles = await list_vehicles(conn)
    recent_purposes = await _fetch_recent_purposes(conn)
    if template == "review.html":
        return await render_page(
            request, template,
            {
                **card, "vehicles": vehicles, "recent_purposes": recent_purposes,
                "filter_from": from_, "filter_to": to, "filter_vehicle": vehicle,
                "filter_q": q,
                "review_url": _review_url(from_, to, vehicle, q),
                "undo_notice": "",
                **extra,
            },
        )
    return request.app.state.templates.TemplateResponse(
        request, template,
        {
            **card, "vehicles": vehicles, "recent_purposes": recent_purposes,
            "filter_from": from_, "filter_to": to, "filter_vehicle": vehicle,
            "filter_q": q,
            "review_url": _review_url(from_, to, vehicle, q),
            "undo_notice": "",
            **extra,
        },
    )


def register(router: APIRouter) -> None:
        @router.get("/review")
        async def review_page(
            request: Request,
            user: dict = Depends(require_user),
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            vehicle: str = Query(""),
            q: str = Query(""),
        ):
            where, params = _review_filter_sql(request, from_, to, vehicle, q)
            async with request.state.account_pool.connection() as conn:
                card = await _fetch_review_card(conn, where, params, cursor=None)
                return await _render_review_card(
                    request, conn, "review.html", card, from_, to, vehicle, q,
                    user=user, csrf=request.session.get("csrf", ""),
                )

        @router.get("/review/card")
        async def review_card(
            request: Request,
            user: dict = Depends(require_user),
            after: int = Query(...),
            from_: str = Query("", alias="from"),
            to: str = Query(""),
            vehicle: str = Query(""),
            q: str = Query(""),
        ):
            """Next-card partial, used by Skip. `after` is the currently displayed
            trip's id. Its own `(started_at, id)` becomes the cursor, so a
            skipped trip can't reappear within this pass.
            """
            where, params = _review_filter_sql(request, from_, to, vehicle, q)
            async with request.state.account_pool.connection() as conn:
                cursor = await _trip_position(conn, after)
                card = await _fetch_review_card(conn, where, params, cursor)
                return await _render_review_card(
                    request, conn, "_review_card.html", card, from_, to, vehicle, q,
                )

        @router.post("/review/{trip_id}/skip", dependencies=[Depends(require_csrf)])
        async def review_skip_trip(
            request: Request,
            trip_id: int,
            purpose: str = Form(""),
            from_: str = Form("", alias="from"),
            to: str = Form(""),
            vehicle: str = Form(""),
            user: dict = Depends(require_user),
            notes: str = Form(""),
            vehicle_id: str = Form(""),
            q: str = Form(""),
            exclusion: str = Form(""),
        ):
            """Save visible fields and advance without classifying the trip.

            Carrying every editable value makes Skip authoritative if an
            independent field save is still in flight.
            """
            exclusion = exclusion if isinstance(exclusion, str) else ""
            if exclusion and exclusion not in EXCLUSIONS:
                raise HTTPException(status_code=400, detail="Unknown exclusion")
            where, params = _review_filter_sql(request, from_, to, vehicle, q)
            notes_value = notes if isinstance(notes, str) else ""
            vehicle_value = vehicle_id if isinstance(vehicle_id, str) else ""
            async with request.state.account_pool.connection() as conn:
                position = await _trip_position(conn, trip_id)
                if position is None:
                    raise HTTPException(status_code=404, detail="No such trip")
                try:
                    cur = await conn.execute(
                        "UPDATE trips SET purpose = %s, notes = %s, vehicle_id = %s, "
                        "exclusion = %s, "
                        "updated_at = now() WHERE id = %s AND account_id = %s",
                        (purpose.strip() or None, notes_value.strip() or None,
                         _parse_vehicle_form(vehicle_value), exclusion or None, trip_id, account_id(conn)),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such trip")
                card = await _fetch_review_card(conn, where, params, cursor=position)
                return await _render_review_card(
                    request, conn, "_review_card.html", card, from_, to, vehicle, q,
                )

        @router.post("/review/{trip_id}/tag", dependencies=[Depends(require_csrf)])
        async def review_tag_trip(
            request: Request,
            trip_id: int,
            category: str = Form(...),
            purpose: str = Form(""),
            from_: str = Form("", alias="from"),
            to: str = Form(""),
            vehicle: str = Form(""),
            user: dict = Depends(require_user),
            notes: str = Form(""),
            vehicle_id: str = Form(""),
            q: str = Form(""),
            exclusion: str = Form(""),
        ):
            """Tag-and-advance in one round trip. Unlike
            the list-view `tag_trip`, a review card is by definition unclassified,
            there's nothing to clear, so category is restricted to the two real
            tags. The just-tagged trip's own position becomes the next cursor: it
            has left the unclassified set, so cursor-forward and "next remaining"
            coincide.
            """
            if category not in ("business", "personal"):
                raise HTTPException(status_code=400, detail="Unknown category")
            exclusion = exclusion if isinstance(exclusion, str) else ""
            if exclusion and exclusion not in EXCLUSIONS:
                raise HTTPException(status_code=400, detail="Unknown exclusion")
            where, params = _review_filter_sql(request, from_, to, vehicle, q)
            notes_value = notes if isinstance(notes, str) else ""
            vehicle_value = vehicle_id if isinstance(vehicle_id, str) else ""
            async with request.state.account_pool.connection() as conn:
                position = await _trip_position(conn, trip_id)
                if position is None:
                    raise HTTPException(status_code=404, detail="No such trip")
                try:
                    cur = await conn.execute(
                        "UPDATE trips SET category = %s, purpose = %s, notes = %s, "
                        "vehicle_id = %s, exclusion = %s, tag_source = 'human', updated_at = now() "
                        "WHERE id = %s AND account_id = %s",
                        (category, purpose.strip() or None, notes_value.strip() or None,
                         _parse_vehicle_form(vehicle_value), exclusion or None, trip_id, account_id(conn)),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such trip")
                card = await _fetch_review_card(conn, where, params, cursor=position)
                return await _render_review_card(
                    request, conn, "_review_card.html", card, from_, to, vehicle, q,
                )

        @router.post("/review/{trip_id}/exclusion", dependencies=[Depends(require_csrf)])
        async def review_exclude_trip(
            request: Request,
            trip_id: int,
            exclusion: str = Form(""),
            purpose: str = Form(""),
            from_: str = Form("", alias="from"),
            to: str = Form(""),
            vehicle: str = Form(""),
            user: dict = Depends(require_user),
            notes: str = Form(""),
            vehicle_id: str = Form(""),
            q: str = Form(""),
        ):
            exclusion = exclusion if isinstance(exclusion, str) else ""
            if exclusion and exclusion not in EXCLUSIONS:
                raise HTTPException(status_code=400, detail="Unknown exclusion")
            notes_value = notes if isinstance(notes, str) else ""
            vehicle_value = vehicle_id if isinstance(vehicle_id, str) else ""
            async with request.state.account_pool.connection() as conn:
                try:
                    cur = await conn.execute(
                        "UPDATE trips SET exclusion = %s, purpose = %s, notes = %s, "
                        "vehicle_id = %s, updated_at = now() WHERE id = %s AND account_id = %s",
                        (
                            exclusion or None, purpose.strip() or None,
                            notes_value.strip() or None,
                            _parse_vehicle_form(vehicle_value), trip_id, account_id(conn),
                        ),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such trip")
            return Response(status_code=204)

        @router.post("/review/{trip_id}/undo", dependencies=[Depends(require_csrf)])
        async def review_undo_trip(
            request: Request,
            trip_id: int,
            kind: str = Form(...),
            from_: str = Form("", alias="from"),
            to: str = Form(""),
            vehicle: str = Form(""),
            user: dict = Depends(require_user),
            q: str = Form(""),
        ):
            """Reverse the one action review.html's page script remembers, and
            re-present that trip. One step, client-remembered, lost on reload --
            not the override/audit machinery that backs merge/split/delete undo
            in Settings. That machinery is for structural changes to trips; this
            is taking back the last advance.

            `kind` distinguishes what to reverse, because tag and skip are
            not symmetric. A tag (`review_tag_trip`) wrote `category` and left the
            unclassified set, so undoing it clears `category` back to
            'unclassified' through `_apply_human_tag`, the same clearing path
            the list-view `tag_trip` already uses -- `tag_source` stays 'human'
            on the clear, deliberately (see that function's docstring for why
            that's load-bearing). A skip (`review_skip_trip`) leaves the trip
            in the unclassified set, so undoing it reverses no write and
            leaves every saved visible field alone.

            Either way the trip is re-fetched with `inclusive=True` so it, not
            whatever comes after it, lands back on the card.
            """
            if kind not in ("tag", "skip"):
                raise HTTPException(status_code=400, detail="Unknown undo kind")
            where, params = _review_filter_sql(request, from_, to, vehicle, q)
            async with request.state.account_pool.connection() as conn:
                position = await _trip_position(conn, trip_id)
                if position is None:
                    raise HTTPException(status_code=404, detail="No such trip")
                if kind == "tag":
                    await _apply_human_tag(conn, trip_id, "unclassified")
                card = await _fetch_review_card(
                    conn, where, params, cursor=position, inclusive=True,
                )
                return await _render_review_card(
                    request, conn, "_review_card.html", card, from_, to, vehicle, q,
                    undo_notice="Previous action undone. Trip restored.",
                )

        @router.post("/review/{trip_id}/delete", dependencies=[Depends(require_csrf)])
        async def review_delete_trip(
            request: Request,
            trip_id: int,
            from_: str = Form("", alias="from"),
            to: str = Form(""),
            vehicle: str = Form(""),
            user: dict = Depends(require_user),
            q: str = Form(""),
        ):
            """Delete the displayed trip and advance beyond its prior position.

            The cursor is captured by `_delete_trip_in` before either deletion
            removes the row, so the review pass continues exactly as tag and
            Skip do, including stable id tie-breaking.
            """
            where, params = _review_filter_sql(request, from_, to, vehicle, q)
            async with request.state.account_pool.connection() as conn:
                position = await _delete_trip_in(conn, trip_id)
                card = await _fetch_review_card(conn, where, params, cursor=position)
                return await _render_review_card(
                    request, conn, "_review_card.html", card, from_, to, vehicle, q,
                )
