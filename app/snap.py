"""OSRM road-snapping. Pure core (this section) + `SnapWorker` (below),
same "pure function + thin I/O wrapper" convention as
`app/rates.py`/`app/export.py`.
"""
from __future__ import annotations

from app.account_context import account_id
from app.account_jobs import lock_device_generation

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from psycopg_pool import AsyncConnectionPool

from app.detector.runner import load_trip_points
from app.validation import parse_finite_number
from app.worker import PokeSweepWorker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchPoint:
    t: datetime
    lat: float
    lon: float
    accuracy_m: Optional[float]


@dataclass(frozen=True)
class SnapResult:
    """`status` is never "pending" -- pending is the DB default / the
    outcome of a transport error, neither of which ever reaches
    `parse_match_response` (illegal states unrepresentable).
    """
    status: str                        # "ok" | "low_confidence" | "failed"
    path_geojson: Optional[dict]       # GeoJSON MultiLineString geometry, or None
    distance_m: Optional[float]
    reason: Optional[str] = None       # populated for low_confidence/failed; logged only


def downsample(points: list[MatchPoint], max_coords: int) -> list[MatchPoint]:
    """Keep endpoints always; thin the interior at a uniform stride so a
    long trip is evenly sampled rather than truncated. Dedupes by
    timestamp after rounding, since a repeated/out-of-order timestamp
    would corrupt OSRM's `timestamps=` parameter (must be strictly
    increasing).
    """
    n = len(points)
    if n <= max_coords or max_coords < 2:
        return points
    keep_interior = max_coords - 2
    if keep_interior <= 0:
        result = [points[0], points[-1]]
    else:
        step = (n - 2) / (keep_interior + 1)
        result = [points[0]]
        for k in range(1, keep_interior + 1):
            idx = round(k * step)
            idx = min(max(idx, 1), n - 2)
            result.append(points[idx])
        result.append(points[-1])

    seen = set()
    deduped = []
    for p in result:
        if p.t not in seen:
            seen.add(p.t)
            deduped.append(p)
    return sorted(deduped, key=lambda p: p.t)


def radiuses(points: list[MatchPoint], min_r: float = 20.0, max_r: float = 50.0) -> list[float]:
    """Per-point OSRM `radiuses` values, clamping accuracy_m into
    [min_r, max_r]. A missing accuracy_m maps to max_r -- treat "unknown"
    as "least confident," not "perfectly accurate": guessing optimistic
    would let OSRM silently snap a bad fix to the wrong nearby road.

    The `radiuses` value is OSRM's *search radius* for candidate roads, not
    the GPS error itself, so the floor is deliberately well above real GPS
    accuracy. A fix's distance to the OSM road centerline is GPS error PLUS
    a systematic baseline -- lane offset, divided-carriageway half-width, and
    OSM digitization error -- that runs ~10-15m even for a pinpoint 2-5m fix.
    A tight floor (the original 5m) made OSRM find no candidate road for
    accurate fixes sitting just off the centerline and silently drop them as
    null tracepoints, truncating the snapped route partway to the
    destination (observed on trip 17: two 5m-accuracy end points needed
    ~12m of radius to match at all). 20m clears that with headroom while
    staying tight enough to not snap onto a wrong parallel road.

    Deliberately independent of `app.config`/`MAX_ACCURACY_M` -- the
    detector's accuracy gate and this clamp are two separately-tunable
    numbers that shouldn't be spuriously coupled. 50m as a ceiling is
    tighter than the detector's 100m gate on purpose: accuracy_m near
    100 is already too coarse to trust for road identification.
    """
    out = []
    for p in points:
        a = p.accuracy_m
        out.append(max_r if a is None else min(max(a, min_r), max_r))
    return out


async def route_distance_m(
    http_client: httpx.AsyncClient, osrm_url: str,
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
) -> Optional[float]:
    """One-shot OSRM `/route` call for the missing-trip bridging
    suggestion -- unlike `/match` above, this never runs per row on the trip
    list (no O(rows) OSRM calls on page load), only once when a missing-trip
    badge's prefill link is actually followed. A dedicated short timeout,
    not `SnapWorker`'s shared 10s client default, because this blocks a page
    render a user is actively waiting on rather than a background worker's
    own loop.

    Raises on transport failure or a non-2xx status, like
    `GeocodeProvider.reverse` (app/geocode.py) -- the caller
    (app/ui/manual.py) catches broadly and degrades to no suggestion, since
    a missing hint is never worse than the badge/prefill flow it's
    decorating.
    """
    url = (
        f"{osrm_url.rstrip('/')}/route/v1/driving/"
        f"{from_lon:.6f},{from_lat:.6f};{to_lon:.6f},{to_lat:.6f}"
        "?overview=false&alternatives=false&steps=false"
    )
    resp = await http_client.get(url, timeout=httpx.Timeout(4.0, connect=2.0))
    resp.raise_for_status()
    body = resp.json()
    routes = body.get("routes") or []
    if body.get("code") != "Ok" or not routes:
        return None
    return routes[0].get("distance")


@dataclass(frozen=True)
class RoutedLine:
    distance_m: float
    geojson: dict          # GeoJSON LineString geometry


async def route_line(
    http_client: httpx.AsyncClient, osrm_url: str,
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
) -> Optional[RoutedLine]:
    """One-shot OSRM `/route` call for routed manual trip entry -- same
    caller-facing shape as `route_distance_m` above (dedicated short
    timeout, raises on transport failure or non-2xx status so the caller
    degrades on its own terms), but also asking for the route geometry so
    the manual trip can store a real road-following line instead of a
    straight line between the two endpoints.

    Returns None for anything short of a clean, well-formed route rather
    than raising, since a malformed OSRM body (bad distance, missing or
    malformed geometry) is a "can't route this" outcome for the caller,
    not a transport failure -- the same distinction `route_distance_m`
    draws between its `raise_for_status()` and its own `None` return.
    """
    url = (
        f"{osrm_url.rstrip('/')}/route/v1/driving/"
        f"{from_lon:.6f},{from_lat:.6f};{to_lon:.6f},{to_lat:.6f}"
        "?overview=full&geometries=geojson&alternatives=false&steps=false"
    )
    resp = await http_client.get(url, timeout=httpx.Timeout(4.0, connect=2.0))
    resp.raise_for_status()
    body = resp.json()
    routes = body.get("routes") or []
    if body.get("code") != "Ok" or not routes:
        return None

    route = routes[0]
    distance = parse_finite_number(route.get("distance"), minimum=0)
    # A distance of exactly zero only happens when the two endpoints
    # coincide (the same place picked twice, or two coincident map clicks),
    # which is not a real route to store. parse_manual_trip_input already
    # requires a strictly positive distance for the hand-entered path, so
    # letting a routed zero through here would store a trip the manual-entry
    # contract would otherwise refuse.
    if not distance:
        return None

    geometry = route.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None

    # Rebuilt from validated floats rather than passing the parsed response
    # through: this is what guarantees nothing unvalidated and no extra
    # keys from the OSRM response can reach the database later.
    parsed_coords: list[list[float]] = []
    for entry in coordinates:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return None
        lon = parse_finite_number(entry[0], minimum=-180, maximum=180)
        lat = parse_finite_number(entry[1], minimum=-90, maximum=90)
        if lon is None or lat is None:
            return None
        parsed_coords.append([lon, lat])

    return RoutedLine(
        distance_m=distance,
        geojson={"type": "LineString", "coordinates": parsed_coords},
    )


def parse_match_response(response_json: dict, min_confidence: float, input_count: int) -> SnapResult:
    """Parse an OSRM `/match/v1/car/...` response. `gaps=split` can return
    multiple disjoint matchings for a trip with a real recording gap; each
    becomes a separate line in a MultiLineString rather than being joined
    into one LineString, which would draw a false straight line across
    the gap.
    """
    code = response_json.get("code")
    matchings = response_json.get("matchings") or []
    if code != "Ok" or not matchings:
        return SnapResult(
            status="failed", path_geojson=None, distance_m=None,
            reason=f"osrm code={code!r}, {len(matchings)} matchings",
        )

    lines: list[list[list[float]]] = []
    total_distance = 0.0
    worst_confidence = 1.0
    for m in matchings:
        geom = m.get("geometry") or {}
        leg_coords = geom.get("coordinates") or []
        if leg_coords:
            lines.append(leg_coords)
        total_distance += m.get("distance", 0.0)
        worst_confidence = min(worst_confidence, m.get("confidence", 0.0))

    # tracepoints has one entry per *input* coordinate, null where OSRM
    # couldn't match that tracepoint to anything at all.
    tracepoints = response_json.get("tracepoints") or []
    matched_count = sum(1 for tp in tracepoints if tp is not None)
    match_fraction = (matched_count / input_count) if input_count else 0.0

    # Both gates are kept since they catch different failure modes:
    # confidence = "matched, but shakily"; fraction = "didn't match at
    # all" (OSRM tends to drop unmatchable spans as null tracepoints
    # rather than emit them as a separate low-confidence matching, so
    # this is likely the gate that fires more often in practice). 0.8 is
    # a starting heuristic, untuned against real data so far -- revisit
    # once there's a backlog of real low_confidence results to eyeball.
    low_conf = worst_confidence < min_confidence or match_fraction < 0.8
    if low_conf:
        reason = (
            f"min matching confidence {worst_confidence:.2f} < {min_confidence}"
            if worst_confidence < min_confidence
            else f"only {match_fraction:.0%} of tracepoints matched"
        )
    else:
        reason = None

    return SnapResult(
        status="low_confidence" if low_conf else "ok",
        path_geojson={"type": "MultiLineString", "coordinates": lines} if lines else None,
        distance_m=total_distance,
        reason=reason,
    )


class SnapWorker(PokeSweepWorker):
    """Poke+sweep background worker draining `snap_status='pending'` trips
    against a self-hosted OSRM instance. The poke/debounce/sweep loop,
    `start`/`stop`, and guarded-run wrapper live in `PokeSweepWorker`
    (app/worker.py) -- shared with `DetectorScheduler` and `GeocodeWorker`,
    which need the identical machinery; this class only supplies `run_once()`.

    No advisory lock and no cross-process claim (unlike the detector, which
    needs a lock because a *skipped* run must never falsely advance its
    checkpoint or a dirty window gets silently dropped). A pending row just
    stays pending until a terminal UPDATE lands it, which is what removes it
    from the pending set -- nothing else needs to coordinate at this app's
    single-instance scale. The batch SELECT deliberately does NOT hold a
    `FOR UPDATE` lock across processing: the claiming connection is released
    back to the pool before any OSRM call, so a row lock taken there would be
    gone during the work it was meant to protect (an earlier version took one
    here, which did nothing). If a second replica were ever added the two
    could double-process an overlapping batch -- wasteful, but not unsafe,
    since each trip's terminal UPDATE is idempotent. A real claim (a transient
    status, or a lock genuinely held across the multi-second OSRM request) is
    left until that scale actually exists.

    Snapping never blocks or shares a transaction with the detector's --
    each DB write below is its own short connection borrow, and the OSRM
    HTTP call happens between borrows, never while holding one.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        http_client: httpx.AsyncClient,
        osrm_url: str,
        min_confidence: float,
        max_coords: int,
        debounce_s: float,
        sweep_s: float,
        batch_size: int = 20,
    ):
        super().__init__(
            task_name="snap-worker",
            log=log,
            failure_message="snap worker run failed; will retry on next debounce/sweep",
            debounce_s=debounce_s,
            sweep_s=sweep_s,
        )
        self.pool = pool
        self.http = http_client
        self.osrm_url = osrm_url.rstrip("/")
        self.min_confidence = min_confidence
        self.max_coords = max_coords
        self.batch_size = batch_size

    async def run_once(self) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT t.id FROM trips t JOIN tracking_devices d "
                "ON d.account_id=t.account_id AND d.id=t.tracking_device_id "
                "WHERE t.account_id=%s AND t.snap_status='pending' "
                "AND t.source='detected' AND NOT t.imported AND d.enabled AND d.revoked_at IS NULL "
                "ORDER BY t.id LIMIT %s",
                (account_id(conn), self.batch_size),
            )
            trip_ids = [r[0] for r in await cur.fetchall()]
        if not trip_ids:
            return
        for trip_id in trip_ids:
            await self._snap_one(trip_id)

    async def _load_points(self, conn, trip_id: int) -> list[MatchPoint]:
        """Adapts the shared time-range point query (`load_trip_points`,
        app/detector/runner.py -- see its docstring for why this isn't a
        plain `points.trip_id = trip_id` query) into this module's own
        `MatchPoint` shape.
        """
        rows = await load_trip_points(conn, trip_id)
        return [MatchPoint(t=r[1], lat=r[2], lon=r[3], accuracy_m=r[4]) for r in rows]

    async def _snap_one(self, trip_id: int) -> None:
        async with self.pool.connection() as conn:
            # Read BEFORE loading points, not after: the pool runs at READ
            # COMMITTED, so each statement on this connection gets its own
            # snapshot, and a detector rewrite (app/detector/runner.py) could
            # commit between two statements here. Reading updated_at first
            # means a rewrite landing during or after the point load can only
            # make `generation` older than the row's current value by the
            # time we reach the terminal write below, never newer/matching -
            # so the residual race falls in the safe direction (an
            # unnecessary discard, re-snapped next sweep) rather than letting
            # a stale result through. `updated_at` doubles as a generation
            # token: the rewrite always bumps it, but this worker's own
            # terminal UPDATEs below never do, so a mismatch at write time
            # means a rewrite superseded the points this result was computed
            # from.
            cur = await conn.execute(
                "SELECT t.updated_at, t.tracking_device_id, d.generation FROM trips t "
                "JOIN tracking_devices d ON d.account_id=t.account_id AND d.id=t.tracking_device_id "
                "WHERE t.account_id=%s AND t.id=%s AND t.snap_status='pending' "
                "AND t.source='detected' AND NOT t.imported AND d.enabled AND d.revoked_at IS NULL",
                (account_id(conn), trip_id),
            )
            row = await cur.fetchone()
            if row is None:
                return
            generation, device_id, device_generation = row
            points = await self._load_points(conn, trip_id)
        if len(points) < 2:
            # A detected trip should always have >= 2 points; if one somehow
            # doesn't, OSRM can never match it, so mark it terminally failed
            # rather than leaving it pending to be re-queried every sweep
            # forever. Same CAS guard as the terminal write below: a rewrite
            # landing here means the point count itself may be stale
            # (e.g. the rewrite's own point set is >= 2), so don't clobber it.
            async with self.pool.connection() as conn:
                if not await lock_device_generation(conn, device_id, device_generation):
                    return
                cur = await conn.execute(
                    "UPDATE trips SET snap_status = 'failed', snapped_at = now() "
                    "WHERE account_id = %s AND id = %s AND updated_at = %s",
                    (account_id(conn), trip_id, generation),
                )
            if cur.rowcount == 0:
                log.info(
                    "snap: trip %s was rewritten mid-snap, discarding stale "
                    "<2-point result", trip_id,
                )
                return
            log.warning("snap: trip %s has < 2 usable points, marking failed", trip_id)
            return

        sampled = downsample(points, self.max_coords)
        coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in sampled)
        timestamps = ";".join(str(int(p.t.timestamp())) for p in sampled)
        radii = ";".join(f"{r:.1f}" for r in radiuses(sampled))
        # tidy=false deliberately: OSRM's tidy pass is free to treat closely-
        # spaced points as redundant and drop them from consideration, and a
        # dropped point comes back as a null tracepoint in the response -
        # indistinguishable, from parse_match_response's match_fraction gate,
        # from a point that genuinely couldn't be matched to any road. That
        # was silently misclassifying every trip on a dense OwnTracks ping
        # interval (~2-4s) as low_confidence despite ~0.98 real matching
        # confidence (confirmed live against a real OSRM instance: trips
        # with dense pings showed tidy=true nulled 55-58% of tracepoints on
        # excellent matches; tidy=false nulled zero, with no regression on
        # old sparse-ping trips, which matched identically either way).
        url = (
            f"{self.osrm_url}/match/v1/car/{coords}"
            f"?geometries=geojson&overview=full&steps=false&annotations=false"
            f"&gaps=split&tidy=false&timestamps={timestamps}&radiuses={radii}"
        )
        try:
            resp = await self.http.get(url)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            # Connection/timeout error, or a response we can't even parse as
            # JSON: leave pending, retry later. Deliberately does NOT call
            # resp.raise_for_status() first -- confirmed live against a real
            # OSRM instance that /match answers a genuine "can't match this
            # trace" with HTTP 400 + a proper {"code": "NoMatch", ...} body,
            # not 200. Gating on status here would misclassify that as a
            # transport failure and leave the trip pending forever (retried
            # every sweep, same NoMatch every time) instead of landing on
            # the correct terminal 'failed' via parse_match_response below.
            log.warning("snap: trip %s OSRM call failed (%s), leaving pending", trip_id, type(e).__name__)
            return
        if not isinstance(body, dict) or "code" not in body:
            log.warning("snap: trip %s got an unrecognized OSRM response, leaving pending", trip_id)
            return

        result = parse_match_response(body, self.min_confidence, len(sampled))
        async with self.pool.connection() as conn:
            if not await lock_device_generation(conn, device_id, device_generation):
                return
            cur = await conn.execute(
                "UPDATE trips SET path_snapped = ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), "
                " distance_snapped_m = %s, snap_status = %s, snapped_at = now() "
                "WHERE account_id = %s AND id = %s AND updated_at = %s",
                (
                    json.dumps(result.path_geojson) if result.path_geojson else None,
                    result.distance_m, result.status, account_id(conn), trip_id, generation,
                ),
            )
        if cur.rowcount == 0:
            # A detector rewrite landed between the point load and this write
            # (or the trip vanished). It already reset snap_status back to
            # 'pending' with a fresh updated_at, so the next sweep re-snaps
            # against the current geometry; applying this result now would
            # silently attach a road-snapped path computed from stale points.
            log.info(
                "snap: trip %s was rewritten mid-snap, discarding stale result "
                "(would have been %s)", trip_id, result.status,
            )
            return
        if result.status != "ok":
            log.info("snap: trip %s -> %s (%s)", trip_id, result.status, result.reason)
