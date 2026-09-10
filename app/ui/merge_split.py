from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from psycopg import errors
from starlette.responses import JSONResponse, Response

from app.auth import require_csrf, require_user
from app.db import DETECTOR_ADVISORY_LOCK_KEY
from app.detector.runner import load_trip_points
from app.merge import TripSpan, plan_merge_selected

from app.ui._common import CATEGORIES, _fetch_trip, _parse_vehicle_form, _path_distance_m, _poke_snap_worker

async def _merge_trips_core(
    request: Request, trip_ids: list[int], category: str = "keep", purpose: str = "",
    notes: str = "", vehicle: str = "keep",
) -> int:
    """Shared by merge_next/merge_prev and merge_selected: validates the
    selection is a contiguous run of detected trips for one device,
    suppresses the real stay between each consecutive pair, reprocesses
    the device once, then overwrites the resulting trip's
    tag/purpose/notes with what the user submitted. Reconcile keeps the
    *longest* original trip's fields, an implementation detail the user
    shouldn't have to think about. Returns the merged trip's id.

    `category` and `vehicle` are both tri-state: "keep" (default)
    preserves whatever reconcile's longest-trip inheritance left,
    including the existing `tag_source`, without human-locking it; a
    real category, or "" / a digit string for vehicle, assigns and (for
    category) also claims human ownership. A concrete category default
    would silently human-lock every merge from callers that don't
    supply one (e.g. a cached old form post, or an untouched select
    whose first option the browser auto-submits); "keep" makes silence
    safe and mirrors `vehicle`.

    Runs on one connection/transaction: the
    override writes, reprocess, and final UPDATE used to span three
    commits, and a failure between them could leave committed
    suppress-overrides with no corresponding merged trip. The advisory
    lock is taken up front so the *entire* merge is serialized against a
    concurrent detector run; `reprocess_device_in` re-taking it later is
    a no-op (advisory locks are reentrant within one session).
    """
    keep_category = category == "keep"
    if not keep_category and category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category")
    keep_vehicle = vehicle == "keep"
    parsed_vehicle_id = None if keep_vehicle else _parse_vehicle_form(vehicle)
    trip_ids = sorted(set(trip_ids))

    runner = request.app.state.detector_runner
    async with request.app.state.pool.connection() as conn:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
        )

        # Checked before the trip-selection query below excludes imported
        # trips (AND NOT imported): that exclusion alone would just make
        # an imported id look "no longer exist[ent]" below, an accurate
        # but unhelpful reason for a trip that in fact exists and simply
        # can't be merged. An imported trip has no backing points in this
        # instance (see migrations/019_trip_imported.sql), so it was
        # never a legitimate merge candidate -- suppressing the real stay
        # between it and a neighbor is meaningless with no points to
        # reprocess, and the old behavior here was an opaque 500 once the
        # final lookup failed to find a trip the suppress override could
        # never have produced.
        cur = await conn.execute(
            "SELECT 1 FROM trips WHERE id = ANY(%s) AND imported LIMIT 1",
            (trip_ids,),
        )
        if await cur.fetchone():
            raise HTTPException(
                status_code=400,
                detail="One or more selected trips came from a data import and have no "
                "location points in this instance, so they cannot be merged.",
            )

        cur = await conn.execute(
            "SELECT id, device, source::text AS source, started_at, ended_at "
            "FROM trips WHERE id = ANY(%s) AND NOT imported",
            (trip_ids,),
        )
        rows = await cur.fetchall()
        if len(rows) != len(trip_ids):
            raise HTTPException(status_code=400, detail="One or more selected trips no longer exist")
        devices = {r[1] for r in rows}
        if len(devices) > 1:
            raise HTTPException(status_code=400, detail="Selected trips must all be from the same device")
        if any(r[2] != "detected" for r in rows):
            raise HTTPException(status_code=400, detail="Only detected trips can be merged")
        device = next(iter(devices))
        selected = [TripSpan(id=r[0], started_at=r[3], ended_at=r[4]) for r in rows]

        first_start = min(t.started_at for t in selected)
        last_start = max(t.started_at for t in selected)
        cur = await conn.execute(
            "SELECT id, started_at, ended_at FROM trips WHERE device = %s "
            "AND source = 'detected' AND NOT imported AND started_at BETWEEN %s AND %s "
            "ORDER BY started_at",
            (device, first_start, last_start),
        )
        in_range = [TripSpan(id=r[0], started_at=r[1], ended_at=r[2]) for r in await cur.fetchall()]

        try:
            ranges = plan_merge_selected(selected, in_range)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        for range_start, range_end in ranges:
            await conn.execute(
                "DELETE FROM trip_boundary_overrides o USING points p "
                "WHERE o.point_id = p.id AND o.kind = 'force' AND o.device = %s "
                "AND p.recorded_at BETWEEN %s AND %s",
                (device, range_start, range_end),
            )
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, range_start, range_end) "
                "VALUES (%s, 'suppress', %s, %s) ON CONFLICT DO NOTHING",
                (device, range_start, range_end),
            )

        ordered = sorted(selected, key=lambda t: t.started_at)
        merged_start, merged_end = ordered[0].started_at, ordered[-1].ended_at

        await runner.reprocess_device_in(conn, device)

        cur = await conn.execute(
            "SELECT id FROM trips WHERE device = %s AND source = 'detected' "
            "AND started_at = %s AND ended_at = %s",
            (device, merged_start, merged_end),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="Merge did not produce the expected trip")
        merged_id = row[0]
        set_clause = "purpose = %s, notes = %s"
        update_params = [purpose.strip() or None, notes.strip() or None]
        if not keep_category:
            set_clause += ", category = %s, tag_source = 'human'"
            update_params.append(category)
        if not keep_vehicle:
            set_clause += ", vehicle_id = %s"
            update_params.append(parsed_vehicle_id)
        update_params.append(merged_id)
        try:
            await conn.execute(
                f"UPDATE trips SET {set_clause}, updated_at = now() WHERE id = %s",
                update_params,
            )
        except errors.ForeignKeyViolation:
            raise HTTPException(status_code=400, detail="No such vehicle")
    _poke_snap_worker(request)
    return merged_id


async def _merge_with_neighbor(
    request: Request, trip_id: int, direction: str, category: str, purpose: str, notes: str,
    vehicle: str = "keep",
) -> Response:
    pool = request.app.state.pool
    trip = await _fetch_trip(pool, trip_id)
    # Keep this early check even though _merge_trips_core repeats it: it
    # reports the real invalid-source error before a neighbor lookup can
    # misleadingly report that no adjacent detected trip exists.
    if trip["source"] != "detected":
        raise HTTPException(status_code=400, detail="Only detected trips can be merged")
    # Same reasoning, for the other reason a trip can't be merged: an
    # imported trip has no backing points in this instance, so it's
    # never a legitimate merge candidate regardless of direction.
    if trip["imported"]:
        raise HTTPException(
            status_code=400,
            detail="This trip came from a data import and has no location points in "
            "this instance, so it cannot be merged.",
        )

    # direction is validated upstream to "next"/"prev" before this point,
    # so interpolating op/order (rather than %s-parameterizing them) is
    # safe here; device and started_at stay as %s params.
    op, order = (">", "ASC") if direction == "next" else ("<", "DESC")
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM trips WHERE device = %s "
            f"AND source = 'detected' AND NOT imported AND started_at {op} %s "
            f"ORDER BY started_at {order} LIMIT 1",
            (trip["device"], trip["started_at"]),
        )
        neighbor = await cur.fetchone()
        if not neighbor:
            raise HTTPException(status_code=400, detail="No adjacent trip to merge with")

    merged_id = await _merge_trips_core(
        request, [trip_id, neighbor[0]], category, purpose, notes, vehicle,
    )
    return Response(status_code=204, headers={"HX-Redirect": f"/trips/{merged_id}"})


async def _validate_split_distance(
    conn, trip_id: int, point_id: int, min_trip_distance_m: float
) -> tuple[float, float]:
    """Reject split points that the detector would discard as a short half.

    The check must happen before writing an override: otherwise reprocessing
    silently drops the sub-minimum trip and leaves its points unowned.
    """
    trip_points = await load_trip_points(conn, trip_id)
    idx = next((i for i, row in enumerate(trip_points) if row[0] == point_id), None)
    if idx is None:
        raise HTTPException(
            status_code=400,
            detail="Point is not part of this trip's surviving (filtered) point set",
        )
    first_half_m = _path_distance_m(trip_points[: idx + 1])
    second_half_m = _path_distance_m(trip_points[idx:])
    if first_half_m < min_trip_distance_m or second_half_m < min_trip_distance_m:
        raise HTTPException(
            status_code=400,
            detail=(
                "Split point is too close to the start or end of the trip: both "
                f"resulting halves must be at least {min_trip_distance_m:.0f}m "
                f"(this split would produce {first_half_m:.0f}m and "
                f"{second_half_m:.0f}m)"
            ),
        )
    return first_half_m, second_half_m


def register(router: APIRouter) -> None:
        @router.post("/trips/{trip_id}/merge_next", dependencies=[Depends(require_csrf)])
        async def merge_trip_next(
            request: Request,
            trip_id: int,
            category: str = Form("keep"),
            purpose: str = Form(""),
            notes: str = Form(""),
            vehicle_id: str = Form("keep"),
            user: dict = Depends(require_user),
        ):
            return await _merge_with_neighbor(
                request, trip_id, "next", category, purpose, notes, vehicle_id,
            )

        @router.post("/trips/{trip_id}/merge_prev", dependencies=[Depends(require_csrf)])
        async def merge_trip_prev(
            request: Request,
            trip_id: int,
            category: str = Form("keep"),
            purpose: str = Form(""),
            notes: str = Form(""),
            vehicle_id: str = Form("keep"),
            user: dict = Depends(require_user),
        ):
            return await _merge_with_neighbor(
                request, trip_id, "prev", category, purpose, notes, vehicle_id,
            )

        @router.post("/trips/merge_selected", dependencies=[Depends(require_csrf)])
        async def merge_selected_trips(
            request: Request,
            trip_ids: list[int] = Form(...),
            category: str = Form("keep"),
            purpose: str = Form(""),
            notes: str = Form(""),
            vehicle_id: str = Form("keep"),
            user: dict = Depends(require_user),
        ):
            merged_id = await _merge_trips_core(request, trip_ids, category, purpose, notes, vehicle_id)
            return JSONResponse({"trip_id": merged_id})


def register_split(router: APIRouter) -> None:
        @router.post("/trips/{trip_id}/split", dependencies=[Depends(require_csrf)])
        async def split_trip(
            request: Request,
            trip_id: int,
            point_id: int = Form(...),
            user: dict = Depends(require_user),
        ):
            """The override insert and the reprocess share one
            connection/transaction, same reasoning as `_merge_trips_core`. The
            advisory lock is taken first, before the trip is read, and the trip
            is re-read fresh under the lock rather than trusting a pre-lock
            fetch, so a concurrent detector run can't rewrite the trip's
            boundaries out from under this split between the read and the
            override write -- same reasoning as `_delete_trip_in`.
            """
            runner = request.app.state.detector_runner
            async with request.app.state.pool.connection() as conn:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
                )
                cur = await conn.execute(
                    "SELECT device, source::text, started_at, ended_at "
                    "FROM trips WHERE id = %s FOR UPDATE",
                    (trip_id,),
                )
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="No such trip")
                device, source, started_at, ended_at = row
                if source != "detected":
                    raise HTTPException(status_code=400, detail="Only detected trips can be split")
                cur = await conn.execute(
                    "SELECT 1 FROM points WHERE id = %s AND device = %s "
                    "AND recorded_at > %s AND recorded_at < %s",
                    (point_id, device, started_at, ended_at),
                )
                if not await cur.fetchone():
                    raise HTTPException(
                        status_code=400,
                        detail="Point must belong to this trip's interior, strictly between its "
                               "start and end (not already a boundary point)",
                    )
                await _validate_split_distance(
                    conn,
                    trip_id,
                    point_id,
                    request.app.state.config.detector_params.min_trip_distance_m,
                )
                await conn.execute(
                    "INSERT INTO trip_boundary_overrides (device, kind, point_id) "
                    "VALUES (%s, 'force', %s) ON CONFLICT DO NOTHING",
                    (device, point_id),
                )
                await runner.reprocess_device_in(conn, device)
            _poke_snap_worker(request)
            return Response(status_code=204, headers={"HX-Redirect": "/trips"})
