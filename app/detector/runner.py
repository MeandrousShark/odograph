"""Dirty-window detection runs against the database.

Concurrency model:
- In-process, a single scheduler task owns execution; ingest only poke()s it.
- Cross-process, each run try-locks a Postgres advisory lock and skips if it
  loses. last_run_at only advances when a run commits, so skipped runs leave
  the dirty window intact for the next debounce/sweep firing.
"""
from __future__ import annotations

import logging
import os
import resource
import sys
import time
from datetime import datetime, timedelta, timezone

from psycopg_pool import AsyncConnectionPool

from app.autotag import AutotagTrip, Rule, plan_autotags
from app.detector.core import Override, Params, Point, Trip, detect
from app.detector.reconcile import ExistingTrip, plan_reconcile
from app.worker import PokeSweepWorker

log = logging.getLogger(__name__)

DETECTOR_VERSION = 2  # v2: on-foot (low-speed) stay detection
ADVISORY_LOCK_KEY = 0x6D696C6531  # 'mile1'
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
        pool: AsyncConnectionPool,
        params: Params,
        full_reprocess_warn_points: int = 500_000,
    ):
        self.pool = pool
        self.params = params
        self.full_reprocess_warn_points = full_reprocess_warn_points

    async def run_once(self) -> bool:
        """One detection pass. Returns False if the advisory lock was busy.

        Transaction-scoped `pg_try_advisory_xact_lock`, not the session-
        scoped `pg_try_advisory_lock` this used to take: a *session* lock
        needs an explicit unlock, and if `_run()` fails with a SQL error the
        transaction is left aborted, so that unlock itself raises
        `InFailedSqlTransaction` — masking the real error in the log — while
        the lock survives the pool's rollback of the connection and never
        gets released. Every later background run then skips forever
        ("advisory lock busy"), and every blocking `pg_advisory_xact_lock`
        caller (merge/split/places CRUD, all sharing `ADVISORY_LOCK_KEY`)
        hangs indefinitely. The transaction-scoped lock sidesteps all of
        that: it releases automatically on commit *or* rollback, so no
        matching unlock call is needed here at all — same reasoning as
        `reprocess_device_now`'s blocking variant below.
        """
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,)
            )
            locked = (await cur.fetchone())[0]
            if not locked:
                log.info("detector: advisory lock busy, skipping run")
                return False
            await self._run(conn)
            await conn.commit()
        return True

    async def reprocess_device_in(self, conn, device: str) -> None:
        """`conn`-accepting single-device reprocess (app/ui.py's merge/split
        UI endpoints) — a deliberate user action should show its result
        immediately rather than wait on the debounce/sweep.

        Takes the caller's connection instead of opening its own, so a
        caller with other writes to make around the reprocess (merge:
        override writes before, a tag/purpose/notes UPDATE after) can run all
        of it in one transaction — a failure anywhere rolls the whole thing
        back instead of leaving, say, committed suppress-overrides with no
        corresponding merged trip. `reprocess_device_now` below is the thin
        pool-owning wrapper for callers that don't need that.

        Always rewinds to EPOCH rather than computing a minimal dirty
        window: this is a full re-detect for exactly one device (cheap at
        this app's per-device point volume), which sidesteps having to work
        out a rewind point that's guaranteed to precede the boundary being
        edited — the same "full reprocess" path a DETECTOR_VERSION bump
        takes, just scoped to one device instead of all of them.

        Blocking `pg_advisory_xact_lock`, not `run_once`'s try-lock: a user
        click can afford to wait briefly for an in-flight background run to
        finish, whereas a skipped background run just retries later.
        Transaction-scoped, releasing automatically on commit or rollback —
        no matching unlock call needed. Taking it here is a no-op if the
        caller already holds it (Postgres advisory locks are reentrant
        within one session/transaction), so a caller that must serialize
        earlier writes too can safely take it again before this call.
        """
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        await self._process_device(conn, device, EPOCH, full=True)

    async def reprocess_device_now(self, device: str) -> None:
        """Thin pool-owning wrapper around `reprocess_device_in` for callers
        that have no other writes to share a transaction with.
        """
        async with self.pool.connection() as conn:
            await self.reprocess_device_in(conn, device)

    async def _run(self, conn) -> None:
        cur = await conn.execute(
            "SELECT last_run_at, detector_version FROM detector_state WHERE id = 1"
        )
        last_run_at, stored_version = await cur.fetchone()
        full = stored_version != DETECTOR_VERSION

        cur = await conn.execute("SELECT now()")
        run_started = (await cur.fetchone())[0]

        if full:
            log.info(
                "detector: version %s -> %s, full reprocess", stored_version, DETECTOR_VERSION
            )
            cur = await conn.execute("SELECT DISTINCT device FROM points")
            dirty = [(row[0], EPOCH) for row in await cur.fetchall()]
        else:
            cur = await conn.execute(
                "SELECT device, min(recorded_at) FROM points "
                "WHERE received_at > coalesce(%s, '-infinity'::timestamptz) "
                "GROUP BY device",
                (last_run_at,),
            )
            dirty = list(await cur.fetchall())

        for device, dirty_from in dirty:
            await self._process_device(conn, device, dirty_from, full)

        await conn.execute(
            "UPDATE detector_state SET last_run_at = %s, detector_version = %s WHERE id = 1",
            (run_started - RUN_OVERLAP_MARGIN, DETECTOR_VERSION),
        )

    async def _process_device(self, conn, device: str, dirty_from: datetime, full: bool) -> None:
        full_started = None
        full_point_count = None
        if full:
            count_cur = await conn.execute(
                "SELECT count(*) FROM points WHERE device = %s", (device,)
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
                "WHERE device = %s AND ended_at < %s ORDER BY ended_at DESC LIMIT 1",
                (device, dirty_from),
            )
            row = await cur.fetchone()
            if row:
                t0 = row[0]

        cur = await conn.execute(
            "SELECT id, recorded_at, ST_Y(geom::geometry), ST_X(geom::geometry), "
            "       accuracy_m, velocity_kmh "
            "FROM points WHERE device = %s AND recorded_at >= %s ORDER BY recorded_at",
            (device, t0),
        )
        points = [
            Point(t=r[1], lat=r[2], lon=r[3], accuracy_m=r[4], velocity_kmh=r[5], id=r[0])
            for r in await cur.fetchall()
        ]

        # All of a device's durable detector overrides, not just those inside
        # this window — trivial volume at this app's scale, and it sidesteps
        # any off-by-one risk in trying to cleverly scope the fetch to the
        # window. Overrides whose anchors fall outside `points` are harmless
        # no-ops in detect(); discard ranges are likewise matched against only
        # the assembled trips present in this run.
        cur = await conn.execute(
            "SELECT kind::text, range_start, range_end, point_id "
            "FROM trip_boundary_overrides WHERE device = %s",
            (device,),
        )
        overrides = [
            Override(kind=r[0], range_start=r[1], range_end=r[2], point_id=r[3])
            for r in await cur.fetchall()
        ]

        stays, trips = detect(points, self.params, overrides)

        await conn.execute(
            "DELETE FROM stays WHERE device = %s AND started_at >= %s", (device, t0)
        )
        for s in stays:
            await conn.execute(
                "INSERT INTO stays (device, started_at, ended_at, centroid, point_count) "
                "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
                (device, s.started_at, s.ended_at, s.lon, s.lat, s.point_count),
            )

        cur = await conn.execute(
            "SELECT id, started_at, ended_at, category::text, tag_source::text FROM trips "
            "WHERE device = %s AND source = 'detected' AND started_at >= %s",
            (device, t0),
        )
        existing = [
            ExistingTrip(id=r[0], started_at=r[1], ended_at=r[2], category=r[3], tag_source=r[4])
            for r in await cur.fetchall()
        ]
        plan = plan_reconcile(existing, [(t.started_at, t.ended_at) for t in trips])

        previous_snap_inputs = await self._load_previous_snap_inputs(conn, existing)

        # NOTE: strictly `>` t0, not `>=`. t0 is the opening stay's started_at,
        # which is the *first point of that stay* — i.e. the arrival boundary of
        # the trip that ended at this stay. That trip started before t0, so it
        # is not in this window and is never re-emitted below; nulling its
        # arrival point here would orphan it permanently (its trip_id would stay
        # NULL, silently one point short of the trip's own point_count/path).
        # Leaving the point at exactly t0 untouched keeps it pointing at that
        # correct, unchanged preceding trip.
        await conn.execute(
            "UPDATE points SET trip_id = NULL WHERE device = %s AND recorded_at > %s",
            (device, t0),
        )

        for old in plan.deletes:
            if old.tag_source == "human":
                log.warning(
                    "detector: deleting trip %s tagged %r (no longer detected after reprocess)",
                    old.id, old.category,
                )
            await conn.execute("DELETE FROM trips WHERE id = %s", (old.id,))

        touched_ids: list[int] = []
        for trip_id, ni in plan.matches:
            touched_ids.append(await self._write_trip(
                conn, device, trips[ni], trip_id=trip_id,
                previous_snap_inputs=previous_snap_inputs[trip_id],
            ))
        for ni in plan.inserts:
            touched_ids.append(await self._write_trip(conn, device, trips[ni], trip_id=None))

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
        previous: dict[int, list[tuple]] = {trip.id: [] for trip in existing}
        if not previous:
            return previous
        cur = await conn.execute(
            "SELECT t.id, p.id, p.recorded_at, "
            "ST_Y(p.geom::geometry), ST_X(p.geom::geometry), p.accuracy_m "
            "FROM trips t JOIN points p ON p.device = t.device "
            "AND p.recorded_at >= t.started_at AND p.recorded_at <= t.ended_at "
            "WHERE t.id = ANY(%s) AND p.trip_id IS NOT NULL "
            "ORDER BY t.id, p.recorded_at, p.id",
            (list(previous),),
        )
        for row in await cur.fetchall():
            previous[row[0]].append(tuple(row[1:]))
        return previous

    async def _write_trip(
        self, conn, device: str, trip: Trip, trip_id: int | None,
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
        args = (
            trip.started_at, trip.ended_at,
            trip.start_lon, trip.start_lat,
            trip.end_lon, trip.end_lat,
            trip.distance_m, len(trip.points), trip.has_gap, DETECTOR_VERSION,
        )
        if trip_id is None:
            cur = await conn.execute(
                "INSERT INTO trips (device, started_at, ended_at, start_geom, end_geom, "
                " distance_m, point_count, has_gap, detector_version, "
                " snap_status, path_snapped, distance_snapped_m, snapped_at, vehicle_id) "
                "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
                " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s, %s, "
                " 'pending', NULL, NULL, NULL, "
                # NULL when the setting is off, no vehicle is flagged default, or the
                # default has been retired (deactivate_vehicle clears is_default too).
                # A full reprocess deletes and re-inserts every unmatched trip as a
                # fresh row, so a trip the operator had deliberately left unassigned
                # comes back carrying the default here -- consistent with treating an
                # insert as a new trip, but surprising enough to call out at the site
                # of the behavior.
                " (SELECT v.id FROM vehicles v, app_settings s"
                "   WHERE s.id = 1 AND s.auto_assign_default_vehicle"
                "     AND v.is_default AND v.active)) "
                "RETURNING id",
                (device,) + args,
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
                " ) AS path FROM points WHERE id = ANY(%s)"
                "), prior AS ("
                " SELECT t.id, new_raw.path, ("
                "   %s AND t.snap_status IS NOT NULL "
                "   AND t.started_at = %s AND t.ended_at = %s "
                "   AND t.distance_m = %s::real AND t.point_count = %s "
                "   AND ST_X(t.start_geom::geometry) IS NOT DISTINCT FROM %s "
                "   AND ST_Y(t.start_geom::geometry) IS NOT DISTINCT FROM %s "
                "   AND ST_X(t.end_geom::geometry) IS NOT DISTINCT FROM %s "
                "   AND ST_Y(t.end_geom::geometry) IS NOT DISTINCT FROM %s "
                " ) AS preserve_snap FROM trips t CROSS JOIN new_raw WHERE t.id = %s"
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
                    point_ids, same_snap_inputs,
                    trip.started_at, trip.ended_at, trip.distance_m, len(trip.points),
                    trip.start_lon, trip.start_lat, trip.end_lon, trip.end_lat, trip_id,
                ) + args,
            )

        point_ids = [p.id for p in trip.points if p.id is not None]
        await conn.execute(
            "UPDATE points SET trip_id = %s WHERE id = ANY(%s)", (trip_id, point_ids)
        )
        if previous_snap_inputs is None:
            await conn.execute(
                "UPDATE trips SET path = ("
                " SELECT ST_Simplify("
                "   ST_MakeLine(geom::geometry ORDER BY recorded_at, id), 0.0001"
                " ) FROM points WHERE id = ANY(%s)"
                ") WHERE id = %s",
                (point_ids, trip_id),
            )
        return trip_id


async def load_trip_points(conn, trip_id: int) -> list[tuple]:
    """A trip's full point sequence, sourced by the trip's own
    [started_at, ended_at] time range rather than `points.trip_id`.

    When two adjacent trips share a boundary fix (a single-point stay —
    common in OwnTracks' battery-saving mode, and every user-driven split
    deliberately recreates this pattern), `points.trip_id` is single-valued,
    so the trip written second silently steals the shared point from the
    one written first. Querying by time range instead recovers both
    boundary points for both trips. `trip_id IS NOT NULL` still excludes
    the detector's filter-rejected fixes (accuracy/teleport gates).

    Returns raw rows `(id, recorded_at, lat, lon, accuracy_m)` — shared by
    `SnapWorker` (which adapts them into its own `MatchPoint`) and the
    split-point-picker endpoint (`app/ui.py`), so neither has to depend on
    the other's types.
    """
    cur = await conn.execute(
        "SELECT p.id, p.recorded_at, ST_Y(p.geom::geometry), ST_X(p.geom::geometry), p.accuracy_m "
        "FROM points p JOIN trips t ON t.id = %s "
        "WHERE p.device = t.device AND p.recorded_at >= t.started_at "
        "  AND p.recorded_at <= t.ended_at AND p.trip_id IS NOT NULL "
        "ORDER BY p.recorded_at",
        (trip_id,),
    )
    return await cur.fetchall()


async def resolve_and_autotag(conn, trip_ids: list[int]) -> None:
    """Resolve start/end places for `trip_ids` (nearest place whose radius
    contains the endpoint) and apply auto-tag rules to whichever of those
    trips isn't human-tagged. Shared by the detector's post-write hook and
    the UI's places/rules CRUD endpoints (via `reprocess_places`) so both
    paths run the identical logic. `source = 'detected'` restricts this to
    trips with geometry — manual trips have none, so they'd never resolve a
    place anyway, but the filter keeps intent explicit.
    """
    if not trip_ids:
        return
    await conn.execute(
        "UPDATE trips t SET "
        " start_place_id = ("
        "   SELECT p.id FROM places p"
        "   WHERE t.start_geom IS NOT NULL AND ST_DWithin(t.start_geom, p.geom, p.radius_m)"
        "   ORDER BY ST_Distance(t.start_geom, p.geom) LIMIT 1"
        " ),"
        " end_place_id = ("
        "   SELECT p.id FROM places p"
        "   WHERE t.end_geom IS NOT NULL AND ST_DWithin(t.end_geom, p.geom, p.radius_m)"
        "   ORDER BY ST_Distance(t.end_geom, p.geom) LIMIT 1"
        " ) "
        "WHERE t.id = ANY(%s) AND t.source = 'detected'",
        (trip_ids,),
    )

    cur = await conn.execute(
        "SELECT t.id, t.category::text, t.tag_source::text, "
        " t.start_place_id, sp.kind::text, t.end_place_id, ep.kind::text "
        "FROM trips t "
        "LEFT JOIN places sp ON sp.id = t.start_place_id "
        "LEFT JOIN places ep ON ep.id = t.end_place_id "
        "WHERE t.id = ANY(%s) AND t.source = 'detected'",
        (trip_ids,),
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
        "FROM tag_rules ORDER BY id"
    )
    rules = [Rule(*r) for r in await cur.fetchall()]

    for result in plan_autotags(autotag_trips, rules):
        await conn.execute(
            "UPDATE trips SET category = %s, tag_source = %s, updated_at = now() WHERE id = %s",
            (result.category, result.tag_source, result.trip_id),
        )


async def reprocess_places(pool: AsyncConnectionPool) -> None:
    """Re-resolve places and re-apply auto-tag rules for every detected trip.
    Called after any places/rules CRUD from the UI, so existing trips reflect
    the new configuration immediately rather than waiting for their next
    detector reprocess.

    Serialized against the detector on the same advisory lock: both this and a
    detector run UPDATE trips.category, so they must not interleave on the same
    rows. `pg_advisory_xact_lock` blocks until any in-flight detector run's
    transaction (holding `pg_try_advisory_xact_lock`) commits or rolls back and
    releases it, then holds the lock for this transaction (auto-released on
    commit); a detector run starting meanwhile finds the lock busy and skips,
    re-running on its next debounce/sweep — the same safe skip path the
    detector already relies on. At single-instance scale the detector's runs
    are short, so the block here is brief.
    """
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        cur = await conn.execute("SELECT id FROM trips WHERE source = 'detected'")
        trip_ids = [r[0] for r in await cur.fetchall()]
        await resolve_and_autotag(conn, trip_ids)


class DetectorScheduler(PokeSweepWorker):
    """Single loop that serializes detector runs.

    poke() (from ingest) resets a debounce deadline; independently, a sweep
    fires every sweep_s to catch anything a crashed/skipped run left behind.
    The loop itself, `start`/`stop`, and the guarded-run wrapper live in
    `PokeSweepWorker` (app/worker.py) — shared with
    `SnapWorker`/`GeocodeWorker`; this class supplies `run_once()` and
    overrides `after_run_once()` to poke the snap/geocode workers once a
    detector run actually happens (not one skipped for advisory-lock
    contention) — the poke that turns "trip geometry just changed" into
    "go re-snap/re-geocode it soon" without either worker waiting for its
    own sweep.
    """

    def __init__(
        self, runner: DetectorRunner, debounce_s: float, sweep_s: float,
        snap_worker=None, geocode_worker=None,
    ):
        super().__init__(
            task_name="detector-scheduler",
            log=log,
            failure_message="detector run failed; will retry on next debounce/sweep",
            debounce_s=debounce_s,
            sweep_s=sweep_s,
        )
        self.runner = runner
        self.snap_worker = snap_worker  # None when OSRM disabled
        self.geocode_worker = geocode_worker  # None when no geocode provider is configured

    async def run_once(self) -> bool:
        return await self.runner.run_once()

    async def after_run_once(self, ran: bool) -> None:
        if ran and self.snap_worker is not None:
            self.snap_worker.poke()
        if ran and self.geocode_worker is not None:
            self.geocode_worker.poke()
