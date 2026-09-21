"""Dirty-window detection runs against the database.

Concurrency model:
- In-process, a single scheduler task owns execution; ingest only poke()s it.
- Cross-process, each run try-locks a Postgres advisory lock and skips if it
  loses. last_run_at only advances when a run commits, so skipped runs leave
  the dirty window intact for the next debounce/sweep firing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import sys
import time
from datetime import datetime, timedelta, timezone

from app.account_context import AccountPool, account_id

from app.autotag import AutotagTrip, Rule, plan_autotags
from app.db import DETECTOR_ADVISORY_LOCK_KEY
from app.detector.core import Override, Params, Point, Trip, detect
from app.detector.reconcile import ExistingTrip, plan_reconcile

log = logging.getLogger(__name__)

DETECTOR_VERSION = 2  # v2: on-foot (low-speed) stay detection
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# last_run_at is set to now() minus this margin, so a point whose inserting
# transaction started just before our snapshot but committed after it is
# still picked up by the next run. Overlap only causes harmless re-detection.
RUN_OVERLAP_MARGIN = timedelta(seconds=60)


def current_rss_bytes() -> int | None:
    """Read current resident memory on Linux without adding a dependency."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            resident_pages = int(statm.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def high_water_rss_bytes() -> int | None:
    """Return process peak RSS with the platform's `ru_maxrss` units normalized."""
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    return int(peak if sys.platform == "darwin" else peak * 1024)


class DetectorRunner:
    def __init__(
        self,
        pool: AccountPool,
        params: Params,
        full_reprocess_warn_points: int = 500_000,
    ):
        self.pool = pool
        self.params = params
        self.full_reprocess_warn_points = full_reprocess_warn_points

    async def run_once(self) -> bool:
        """Process each owned stream in its own transaction.

        A failed stream leaves its checkpoint untouched; other streams still
        commit. The same global transaction lock continues to exclude imports
        and structural mutations across each stream's protected reads and writes.
        """
        async with self.pool.connection() as conn:
            owner = account_id(conn)
            # Registering an unused device must not manufacture checkpoint
            # history. Existing output/overrides still require reconciliation.
            cur = await conn.execute(
                "SELECT d.id FROM tracking_devices d JOIN detector_state s "
                "ON s.account_id = d.account_id AND s.tracking_device_id = d.id "
                "WHERE d.account_id = %s AND d.enabled AND d.revoked_at IS NULL "
                "AND (s.detector_version <> %s OR EXISTS (SELECT 1 FROM points p "
                "WHERE p.account_id = d.account_id AND p.tracking_device_id = d.id "
                "AND p.received_at > COALESCE(s.last_run_at, '-infinity'::timestamptz))) "
                "AND (s.last_run_at IS NOT NULL OR s.detector_version <> 0 "
                "OR EXISTS (SELECT 1 FROM points p WHERE p.account_id=d.account_id "
                "AND p.tracking_device_id=d.id) "
                "OR EXISTS (SELECT 1 FROM trips t WHERE t.account_id=d.account_id "
                "AND t.tracking_device_id=d.id AND t.source='detected' AND NOT t.imported) "
                "OR EXISTS (SELECT 1 FROM trip_boundary_overrides o WHERE o.account_id=d.account_id "
                "AND o.tracking_device_id=d.id)) "
                "ORDER BY s.last_run_at NULLS FIRST, d.id",
                (owner, DETECTOR_VERSION),
            )
            devices = [row[0] for row in await cur.fetchall()]
        ran = not devices
        failure = None
        for device in devices:
            try:
                async with self.pool.connection() as conn:
                    cur = await conn.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
                    )
                    if not (await cur.fetchone())[0]:
                        log.info("detector: advisory lock busy, skipping stream")
                        continue
                    if await self._admit_device(conn, device) is None:
                        continue
                    await self._run(conn, device)
                ran = True
            except Exception as exc:
                log.warning("detector: stream failed: %s", type(exc).__name__)
                failure = failure or exc
        if failure is not None:
            raise failure
        return ran

    async def _admit_device(self, conn, device: int) -> str | None:
        cur = await conn.execute(
            "SELECT label FROM tracking_devices WHERE account_id = %s AND id = %s "
            "AND enabled AND revoked_at IS NULL FOR SHARE", (account_id(conn), device),
        )
        row = await cur.fetchone()
        return row[0] if row is not None else None

    async def reprocess_device_in(self, conn, device: int) -> None:
        """`conn`-accepting single-device reprocess (app/ui/merge_split.py's
        merge/split UI endpoints). A deliberate user action should show its
        result immediately rather than wait on the debounce/sweep.

        Takes the caller's connection instead of opening its own, so a
        caller with other writes to make around the reprocess (merge:
        override writes before, a tag/purpose/notes UPDATE after) can run all
        of it in one transaction. A failure anywhere rolls the whole thing
        back instead of leaving, say, committed suppress-overrides with no
        corresponding merged trip. `reprocess_device_now` below is the thin
        pool-owning wrapper for callers that don't need that.

        Always rewinds to EPOCH rather than computing a minimal dirty
        window: this is a full re-detect for exactly one device (cheap at
        this app's per-device point volume), which sidesteps having to work
        out a rewind point that's guaranteed to precede the boundary being
        edited, using the same "full reprocess" path a DETECTOR_VERSION bump
        takes, just scoped to one device instead of all of them.

        Blocking `pg_advisory_xact_lock`, not `run_once`'s try-lock: a user
        click can afford to wait briefly for an in-flight background run to
        finish, whereas a skipped background run just retries later.
        Transaction-scoped, releasing automatically on commit or rollback,
        no matching unlock call needed. Taking it here is a no-op if the
        caller already holds it (Postgres advisory locks are reentrant
        within one session/transaction), so a caller that must serialize
        earlier writes too can safely take it again before this call.
        """
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
        )
        if await self._admit_device(conn, device) is None:
            raise ValueError("No such tracking device")
        await self._process_device(conn, device, EPOCH, full=True)

    async def reprocess_device_now(self, device: int) -> None:
        """Thin pool-owning wrapper around `reprocess_device_in` for callers
        that have no other writes to share a transaction with.
        """
        async with self.pool.connection() as conn:
            await self.reprocess_device_in(conn, device)

    async def _run(self, conn, device: int) -> None:
        owner = account_id(conn)
        cur = await conn.execute(
            "SELECT last_run_at, detector_version FROM detector_state "
            "WHERE account_id = %s AND tracking_device_id = %s FOR UPDATE", (owner, device),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError("Tracking device has no detector checkpoint")
        last_run_at, stored_version = row
        full = stored_version != DETECTOR_VERSION
        cur = await conn.execute("SELECT now()")
        run_started = (await cur.fetchone())[0]
        if full:
            dirty_from = EPOCH
        else:
            cur = await conn.execute(
                "SELECT min(recorded_at) FROM points WHERE account_id = %s "
                "AND tracking_device_id = %s "
                "AND received_at > COALESCE(%s, '-infinity'::timestamptz)",
                (owner, device, last_run_at),
            )
            dirty_from = (await cur.fetchone())[0]
        if dirty_from is not None:
            await self._process_device(conn, device, dirty_from, full)
        await conn.execute(
            "UPDATE detector_state SET last_run_at = %s, detector_version = %s "
            "WHERE account_id = %s AND tracking_device_id = %s",
            (run_started - RUN_OVERLAP_MARGIN, DETECTOR_VERSION, owner, device),
        )

    async def _process_device(self, conn, device: int, dirty_from: datetime, full: bool) -> None:
        owner = account_id(conn)
        label = await self._admit_device(conn, device)
        if label is None:
            raise ValueError("No such tracking device")
        full_started = None
        full_point_count = None
        if full:
            count_cur = await conn.execute(
                "SELECT count(*) FROM points WHERE account_id = %s AND tracking_device_id = %s", (owner, device)
            )
            full_point_count = (await count_cur.fetchone())[0]
            full_started = time.perf_counter()
            rss = current_rss_bytes()
            peak = high_water_rss_bytes()
            level = (
                logging.WARNING
                if full_point_count >= self.full_reprocess_warn_points
                else logging.INFO
            )
            log.log(
                level,
                "detector: full reprocess starting device=%s points=%d rss_bytes=%s "
                "high_water_rss_bytes=%s warning_threshold_points=%d",
                device, full_point_count, rss, peak, self.full_reprocess_warn_points,
            )
        # Rewind to the start of the newest stay settled before the dirty
        # point, so the window opens mid-stay and every trip in it is
        # bracketed.
        t0 = EPOCH
        if not full:
            cur = await conn.execute(
                "SELECT started_at FROM stays "
                "WHERE account_id = %s AND tracking_device_id = %s AND ended_at < %s ORDER BY ended_at DESC LIMIT 1",
                (owner, device, dirty_from),
            )
            row = await cur.fetchone()
            if row:
                t0 = row[0]

        cur = await conn.execute(
            "SELECT id, recorded_at, ST_Y(geom::geometry), ST_X(geom::geometry), "
            "       accuracy_m, velocity_kmh "
            "FROM points WHERE account_id = %s AND tracking_device_id = %s AND recorded_at >= %s ORDER BY recorded_at",
            (owner, device, t0),
        )
        points = [
            Point(t=r[1], lat=r[2], lon=r[3], accuracy_m=r[4], velocity_kmh=r[5], id=r[0])
            for r in await cur.fetchall()
        ]

        # All of a device's durable detector overrides, not just those inside
        # this window. This is trivial volume at this app's scale and sidesteps
        # any off-by-one risk in trying to cleverly scope the fetch to the
        # window. Overrides whose anchors fall outside `points` are harmless
        # no-ops in detect(); discard ranges are likewise matched against only
        # the assembled trips present in this run.
        cur = await conn.execute(
            "SELECT kind::text, range_start, range_end, point_id "
            "FROM trip_boundary_overrides WHERE account_id = %s AND tracking_device_id = %s",
            (owner, device),
        )
        overrides = [
            Override(kind=r[0], range_start=r[1], range_end=r[2], point_id=r[3])
            for r in await cur.fetchall()
        ]

        # detect() is CPU-bound and pure over its (immutable) inputs, so it
        # runs off the event loop; the held connection stays idle during the
        # thread and is used again for the writes that follow.
        stays, trips = await asyncio.to_thread(detect, points, self.params, overrides)

        await conn.execute(
            "DELETE FROM stays WHERE account_id = %s AND tracking_device_id = %s AND started_at >= %s", (owner, device, t0)
        )
        for s in stays:
            await conn.execute(
                "INSERT INTO stays (account_id, tracking_device_id, device, started_at, ended_at, centroid, point_count) "
                "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
                (owner, device, label, s.started_at, s.ended_at, s.lon, s.lat, s.point_count),
            )

        cur = await conn.execute(
            "SELECT id, started_at, ended_at, category::text, tag_source::text FROM trips "
            "WHERE account_id = %s AND tracking_device_id = %s AND source = 'detected' AND NOT imported AND started_at >= %s",
            (owner, device, t0),
        )
        existing = [
            ExistingTrip(id=r[0], started_at=r[1], ended_at=r[2], category=r[3], tag_source=r[4])
            for r in await cur.fetchall()
        ]
        plan = plan_reconcile(existing, [(t.started_at, t.ended_at) for t in trips])

        previous_snap_inputs = await self._load_previous_snap_inputs(conn, existing)

        # NOTE: strictly `>` t0, not `>=`. t0 is the opening stay's started_at,
        # which is the *first point of that stay*, i.e. the arrival boundary of
        # the trip that ended at this stay. That trip started before t0, so it
        # is not in this window and is never re-emitted below; nulling its
        # arrival point here would orphan it permanently (its trip_id would stay
        # NULL, silently one point short of the trip's own point_count/path).
        # Leaving the point at exactly t0 untouched keeps it pointing at that
        # correct, unchanged preceding trip.
        await conn.execute(
            "UPDATE points SET trip_id = NULL WHERE account_id = %s AND tracking_device_id = %s AND recorded_at > %s",
            (owner, device, t0),
        )

        for old in plan.deletes:
            if old.tag_source == "human":
                log.warning(
                    "detector: deleting trip %s tagged %r (no longer detected after reprocess)",
                    old.id, old.category,
                )
            linked_cur = await conn.execute(
                "SELECT count(*) FROM expenses WHERE account_id = %s AND trip_id = %s", (owner, old.id)
            )
            linked_expenses = (await linked_cur.fetchone())[0]
            if linked_expenses:
                log.warning(
                    "detector: deleting trip %s with %d linked expenses; expenses detached",
                    old.id, linked_expenses,
                )
            await conn.execute("DELETE FROM trips WHERE account_id = %s AND id = %s", (owner, old.id))

        touched_ids: list[int] = []
        for trip_id, ni in plan.matches:
            touched_ids.append(await self._write_trip(
                conn, device, label, trips[ni], trip_id=trip_id,
                previous_snap_inputs=previous_snap_inputs[trip_id],
            ))
        for ni in plan.inserts:
            touched_ids.append(await self._write_trip(conn, device, label, trips[ni], trip_id=None))

        await resolve_and_autotag(conn, touched_ids)

        log.info(
            "detector: %s from %s: %d pts -> %d stays, %d trips "
            "(%d updated, %d new, %d deleted)",
            device, t0.isoformat(), len(points), len(stays), len(trips),
            len(plan.matches), len(plan.inserts), len(plan.deletes),
        )
        if full_started is not None:
            log.info(
                "detector: full reprocess completed device=%s points=%d elapsed_s=%.3f "
                "rss_bytes=%s high_water_rss_bytes=%s",
                device, full_point_count, time.perf_counter() - full_started,
                current_rss_bytes(), high_water_rss_bytes(),
            )

    async def _load_previous_snap_inputs(
        self, conn, existing: list[ExistingTrip]
    ) -> dict[int, list[tuple]]:
        """Capture signatures before reprocessing clears point assignments.

        A full-device pass may match many trips whose OSRM inputs did not
        change, and forcing those terminal snap results back through the queue
        is both wasteful and externally visible. Point IDs alone cannot prove
        equivalence because corrections can update a point in place, so the
        signature covers timestamp, coordinates, and accuracy too. The
        time-range join intentionally matches ``load_trip_points``: adjacent
        trips share boundary fixes even though ``trip_id`` is single-valued.
        """
        account_id(conn)
        previous: dict[int, list[tuple]] = {trip.id: [] for trip in existing}
        if not previous:
            return previous
        cur = await conn.execute(
            "SELECT t.id, p.id, p.recorded_at, "
            "ST_Y(p.geom::geometry), ST_X(p.geom::geometry), p.accuracy_m "
            "FROM trips t JOIN points p ON p.account_id = t.account_id "
            "AND p.tracking_device_id = t.tracking_device_id "
            "AND p.recorded_at >= t.started_at AND p.recorded_at <= t.ended_at "
            "WHERE t.account_id = %s AND t.id = ANY(%s) AND p.trip_id IS NOT NULL "
            "ORDER BY t.id, p.recorded_at, p.id",
            (account_id(conn), list(previous)),
        )
        for row in await cur.fetchall():
            previous[row[0]].append(tuple(row[1:]))
        return previous

    async def _write_trip(
        self, conn, device: int, label: str, trip: Trip, trip_id: int | None,
        previous_snap_inputs: list[tuple] | None = None,
    ) -> int:
        """Persist one detected trip without invalidating valid snap output.

        Rewriting detector-owned fields used to reset every matched trip to
        pending even when a full pass reproduced it exactly. Snap output now
        survives only when its persisted detector metadata and complete OSRM
        point sequence are unchanged; merge, split, late data, and corrected
        fixes still invalidate it. Raw paths use exact point IDs because shared
        boundary fixes make ``points.trip_id`` depend on write order. The raw
        path is always rewritten, but legacy path differences alone do not
        invalidate a snap when the authoritative inputs are unchanged.
        """
        owner = account_id(conn)
        args = (
            trip.started_at, trip.ended_at,
            trip.start_lon, trip.start_lat,
            trip.end_lon, trip.end_lat,
            trip.distance_m, len(trip.points), trip.has_gap, DETECTOR_VERSION,
        )
        if trip_id is None:
            cur = await conn.execute(
                "INSERT INTO trips (account_id, tracking_device_id, device, started_at, ended_at, start_geom, end_geom, "
                " distance_m, point_count, has_gap, detector_version, "
                " snap_status, path_snapped, distance_snapped_m, snapped_at, vehicle_id) "
                "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
                " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s, %s, "
                " 'pending', NULL, NULL, NULL, "
                # NULL when the setting is off, no vehicle is flagged default, or the
                # default has been retired (deactivate_vehicle clears is_default too).
                # A full reprocess deletes and re-inserts every unmatched trip as a
                # fresh row, so a trip the operator had deliberately left unassigned
                # comes back carrying the default here -- consistent with treating an
                # insert as a new trip, but surprising enough to call out at the site
                # of the behavior.
                " (SELECT v.id FROM vehicles v JOIN account_settings s ON s.account_id = v.account_id"
                "   WHERE s.account_id = %s AND s.auto_assign_default_vehicle"
                "     AND v.is_default AND v.active)) "
                "RETURNING id",
                (owner, device, label) + args + (owner,),
            )
            trip_id = (await cur.fetchone())[0]
        else:
            new_snap_inputs = [
                (p.id, p.t, p.lat, p.lon, p.accuracy_m) for p in trip.points
            ]
            same_snap_inputs = previous_snap_inputs == new_snap_inputs

            point_ids = [p.id for p in trip.points if p.id is not None]
            await conn.execute(
                "WITH new_raw AS ("
                " SELECT ST_Simplify("
                "   ST_MakeLine(geom::geometry ORDER BY recorded_at, id), 0.0001"
                " ) AS path FROM points WHERE account_id = %s AND tracking_device_id = %s AND id = ANY(%s)"
                "), prior AS ("
                " SELECT t.id, new_raw.path, ("
                "   %s AND t.snap_status IS NOT NULL "
                "   AND t.started_at = %s AND t.ended_at = %s "
                "   AND t.distance_m = %s::real AND t.point_count = %s "
                "   AND ST_X(t.start_geom::geometry) IS NOT DISTINCT FROM %s "
                "   AND ST_Y(t.start_geom::geometry) IS NOT DISTINCT FROM %s "
                "   AND ST_X(t.end_geom::geometry) IS NOT DISTINCT FROM %s "
                "   AND ST_Y(t.end_geom::geometry) IS NOT DISTINCT FROM %s "
                " ) AS preserve_snap FROM trips t CROSS JOIN new_raw "
                "WHERE t.account_id = %s AND t.tracking_device_id = %s AND t.id = %s"
                ") UPDATE trips t SET started_at = %s, ended_at = %s, "
                " start_geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
                " end_geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
                " distance_m = %s, point_count = %s, has_gap = %s, "
                " detector_version = %s, updated_at = now(), path = prior.path, "
                " snap_status = CASE WHEN prior.preserve_snap "
                "   THEN t.snap_status ELSE 'pending'::snap_state END, "
                " path_snapped = CASE WHEN prior.preserve_snap THEN t.path_snapped ELSE NULL END, "
                " distance_snapped_m = CASE WHEN prior.preserve_snap "
                "   THEN t.distance_snapped_m ELSE NULL END, "
                " snapped_at = CASE WHEN prior.preserve_snap THEN t.snapped_at ELSE NULL END "
                "FROM prior WHERE t.id = prior.id",
                (
                    owner, device, point_ids, same_snap_inputs,
                    trip.started_at, trip.ended_at, trip.distance_m, len(trip.points),
                    trip.start_lon, trip.start_lat, trip.end_lon, trip.end_lat, owner, device, trip_id,
                ) + args,
            )

        point_ids = [p.id for p in trip.points if p.id is not None]
        await conn.execute(
            "UPDATE points SET trip_id = %s WHERE account_id = %s AND tracking_device_id = %s AND id = ANY(%s)", (trip_id, owner, device, point_ids)
        )
        if previous_snap_inputs is None:
            await conn.execute(
                "UPDATE trips SET path = ("
                " SELECT ST_Simplify("
                "   ST_MakeLine(geom::geometry ORDER BY recorded_at, id), 0.0001"
                " ) FROM points WHERE account_id = %s AND tracking_device_id = %s AND id = ANY(%s)"
                ") WHERE account_id = %s AND tracking_device_id = %s AND id = %s",
                (owner, device, point_ids, owner, device, trip_id),
            )
        return trip_id


async def load_trip_points(conn, trip_id: int) -> list[tuple]:
    """A trip's full point sequence, sourced by the trip's own
    [started_at, ended_at] time range rather than `points.trip_id`.

    When two adjacent trips share a boundary fix (a single-point stay,
    common in OwnTracks' battery-saving mode, and every user-driven split
    deliberately recreates this pattern), `points.trip_id` is single-valued,
    so the trip written second silently steals the shared point from the
    one written first. Querying by time range instead recovers both
    boundary points for both trips. `trip_id IS NOT NULL` still excludes
    the detector's filter-rejected fixes (accuracy/teleport gates).

    Returns raw rows `(id, recorded_at, lat, lon, accuracy_m)`, shared by
    `SnapWorker` (which adapts them into its own `MatchPoint`) and the
    split-point-picker endpoint (`app/ui/merge_split.py`), so neither has to
    depend on the other's types.
    """
    cur = await conn.execute(
        "SELECT p.id, p.recorded_at, ST_Y(p.geom::geometry), ST_X(p.geom::geometry), p.accuracy_m "
        "FROM points p JOIN trips t ON t.id = %s "
        "WHERE t.account_id = %s AND p.account_id = t.account_id "
        "AND p.tracking_device_id = t.tracking_device_id AND p.recorded_at >= t.started_at "
        "  AND p.recorded_at <= t.ended_at AND p.trip_id IS NOT NULL "
        "ORDER BY p.recorded_at",
        (trip_id, account_id(conn)),
    )
    return await cur.fetchall()


async def resolve_and_autotag(conn, trip_ids: list[int]) -> None:
    """Resolve start/end places for `trip_ids` (nearest place whose radius
    contains the endpoint) and apply auto-tag rules to whichever of those
    trips isn't human-tagged. Shared by the detector's post-write hook and
    the UI's places/rules CRUD endpoints (via `reprocess_places`) so both
    paths run the identical logic. `source = 'detected'` restricts this to
    trips with geometry. Manual trips have none, so they'd never resolve a
    place anyway, but the filter keeps intent explicit. `NOT imported`
    excludes portable-imported trips: they carry no start_geom/end_geom (the
    bundle format has no geometry), so re-resolving would null out the
    place ids the import set directly and cascade into un-autotagging them.
    """
    account_id(conn)
    if not trip_ids:
        return
    await conn.execute(
        "UPDATE trips t SET "
        " start_place_id = ("
        "   SELECT p.id FROM places p"
        "   WHERE p.account_id = t.account_id AND t.start_geom IS NOT NULL AND ST_DWithin(t.start_geom, p.geom, p.radius_m)"
        "   ORDER BY ST_Distance(t.start_geom, p.geom) LIMIT 1"
        " ),"
        " end_place_id = ("
        "   SELECT p.id FROM places p"
        "   WHERE p.account_id = t.account_id AND t.end_geom IS NOT NULL AND ST_DWithin(t.end_geom, p.geom, p.radius_m)"
        "   ORDER BY ST_Distance(t.end_geom, p.geom) LIMIT 1"
        " ) "
        "WHERE t.account_id = %s AND t.id = ANY(%s) AND t.source = 'detected' AND NOT t.imported",
        (account_id(conn), trip_ids),
    )

    cur = await conn.execute(
        "SELECT t.id, t.category::text, t.tag_source::text, "
        " t.start_place_id, sp.kind::text, t.end_place_id, ep.kind::text "
        "FROM trips t "
        "LEFT JOIN places sp ON sp.account_id = t.account_id AND sp.id = t.start_place_id "
        "LEFT JOIN places ep ON ep.account_id = t.account_id AND ep.id = t.end_place_id "
        "WHERE t.account_id = %s AND t.id = ANY(%s) AND t.source = 'detected' AND NOT t.imported",
        (account_id(conn), trip_ids),
    )
    autotag_trips = [
        AutotagTrip(
            id=r[0], category=r[1], tag_source=r[2],
            start_place=(r[3], r[4]) if r[3] is not None else None,
            end_place=(r[5], r[6]) if r[5] is not None else None,
        )
        for r in await cur.fetchall()
    ]

    cur = await conn.execute(
        "SELECT id, a_place, a_kind::text, b_place, b_kind::text, category::text "
        "FROM tag_rules WHERE account_id = %s ORDER BY id", (account_id(conn),)
    )
    rules = [Rule(*r) for r in await cur.fetchall()]

    for result in plan_autotags(autotag_trips, rules):
        # tag_source IS DISTINCT FROM 'human' (not <>: tag_source is often
        # NULL, and NULL <> 'human' is NULL/false, which would silently skip
        # every untagged trip) makes the human-tag-supremacy invariant robust
        # here too, not just at plan_autotags' filter: the rows read above
        # aren't locked, so a concurrent human-tag write landing after that
        # read and before this UPDATE would otherwise be silently clobbered
        # back to a rule category.
        await conn.execute(
            "UPDATE trips SET category = %s, tag_source = %s, updated_at = now() "
            "WHERE account_id = %s AND id = %s AND tag_source IS DISTINCT FROM 'human'",
            (result.category, result.tag_source, account_id(conn), result.trip_id),
        )


async def reprocess_places_in(conn) -> None:
    """`conn`-accepting variant of `reprocess_places`, for callers (the UI's
    places/rules CRUD handlers) that have a config-row mutation to share a
    transaction with: a failure here must roll back that mutation too,
    rather than leave it committed with trip tags now inconsistent with it.
    Same reasoning as `reprocess_device_in` vs. `reprocess_device_now`.

    Serialized against the detector on the same advisory lock: both this and a
    detector run UPDATE trips.category, so they must not interleave on the same
    rows. `pg_advisory_xact_lock` blocks until any in-flight detector run's
    transaction (holding `pg_try_advisory_xact_lock`) commits or rolls back and
    releases it, then holds the lock for this transaction (auto-released on
    commit); a detector run starting meanwhile finds the lock busy and skips,
    re-running on its next debounce/sweep, the same safe skip path the
    detector already relies on. At single-instance scale the detector's runs
    are short, so the block here is brief. Taking it here is a no-op if the
    caller already holds it (advisory locks are reentrant within one
    session/transaction).
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
    )
    cur = await conn.execute(
        "SELECT id FROM trips WHERE account_id = %s AND source = 'detected' AND NOT imported",
        (account_id(conn),)
    )
    trip_ids = [r[0] for r in await cur.fetchall()]
    await resolve_and_autotag(conn, trip_ids)


async def reprocess_places(pool: AccountPool) -> None:
    """Re-resolve places and re-apply auto-tag rules for every detected trip.
    Called after any places/rules CRUD from the UI, so existing trips reflect
    the new configuration immediately rather than waiting for their next
    detector reprocess.

    Thin pool-owning wrapper around `reprocess_places_in` for callers that
    have no other writes to share a transaction with.
    """
    async with pool.connection() as conn:
        await reprocess_places_in(conn)
