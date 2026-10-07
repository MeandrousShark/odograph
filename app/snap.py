"""OSRM road-snapping. Pure core (this section) + `SnapWorker` (below),
same "pure function + thin I/O wrapper" convention as
`app/rates.py`/`app/export.py`.
"""
from __future__ import annotations

from app.account_context import account_id
from app.account_jobs import lock_device_generation

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from psycopg_pool import AsyncConnectionPool

from app.capacity import current_owner, owned_thread
from app.provider_http import SNAP_RESPONSE_MAX_BYTES, bounded_json
from app.storage import enhancement_available, is_storage_capacity_error
from app.validation import parse_finite_number
from app.worker import BatchOutcome, TurnOutcome

log = logging.getLogger(__name__)
PROVIDER_ROUTE_MAX_VERTICES = 100_000
SNAP_MIN_ROUTE_BYTES = 54  # EWKB MultiLineString with one two-vertex segment.


class ProviderOutputTooLarge(ValueError):
    """A provider route exceeds the stored geometry limit."""


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
    distance_m: Optional[float]        # None when failed, and when the match
                                       # didn't cover the trip -- see
                                       # parse_match_response's min_coverage
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


def sample_ordinals(count: int, max_coords: int) -> list[int]:
    """Zero-based positions using downsample's Python rounding, including ties."""
    if count <= max_coords:
        return list(range(count))
    if max_coords < 2:
        raise ValueError("max_coords must be at least 2")
    interior = max_coords - 2
    step = (count - 2) / (interior + 1)
    ordinals = [0]
    for k in range(1, interior + 1):
        ordinals.append(min(max(round(k * step), 1), count - 2))
    ordinals.append(count - 1)
    return ordinals


# Matches the split picker predicate, including shared boundary fixes and
# excluding detector-rejected points. Only snapping ranks and thins this set.
_POINT_PREDICATE = (
    "FROM points p JOIN trips t ON t.id = %s "
    "WHERE t.account_id = %s AND p.account_id = t.account_id "
    "AND p.tracking_device_id = t.tracking_device_id "
    "AND p.recorded_at >= t.started_at AND p.recorded_at <= t.ended_at "
    "AND p.trip_id IS NOT NULL "
)



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
    body = await bounded_json(
        http_client, "GET", url, max_bytes=SNAP_RESPONSE_MAX_BYTES,
        timeout=httpx.Timeout(4.0, connect=2.0), raise_for_status=True,
    )
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
    body = await bounded_json(
        http_client, "GET", url, max_bytes=SNAP_RESPONSE_MAX_BYTES,
        timeout=httpx.Timeout(4.0, connect=2.0), raise_for_status=True,
    )
    if current_owner() is not None:
        return await owned_thread(_parse_route_line, body)
    return _parse_route_line(body)


def _parse_route_line(body) -> Optional[RoutedLine]:
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
    if len(coordinates) > PROVIDER_ROUTE_MAX_VERTICES:
        raise ProviderOutputTooLarge("provider route exceeds 100000 vertices")

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


def parse_match_response(
    response_json: dict,
    min_confidence: float,
    input_count: int,
    raw_distance_m: Optional[float],
    min_coverage: float = 0.85,
) -> SnapResult:
    """Parse an OSRM `/match/v1/car/...` response. `gaps=split` can return
    multiple disjoint matchings for a trip with a real recording gap; each
    becomes a separate line in a MultiLineString rather than being joined
    into one LineString, which would draw a false straight line across
    the gap.

    `raw_distance_m` is the trip's own pre-snap distance and `min_coverage`
    the fraction of it a match must reproduce for its distance to be trusted.
    A matching's distance covers only the spans OSRM actually matched, so a
    trace that leaves the provisioned extract comes back as a short route
    confidently describing part of the drive. Below `min_coverage` the
    geometry is still returned but `distance_m` is None, which leaves
    `distance_snapped_m` NULL so `COALESCE(distance_snapped_m, distance_m)`
    falls the whole display back to the raw distance rather than handing a
    fragment's length to every mileage total and to the deduction.

    0.85 is measured rather than guessed. Across 193 `ok` trips in the
    maintainer's production data (2026-09-20), 173 snapped to 1.0-1.1 of raw
    and 19 to 0.9-1.0, with 0.8-0.9 empty, so the threshold sits in a real
    gap. Raw GPS normally runs slightly *longer* than the snapped route
    because jitter inflates it, which is why those ratios cluster just either
    side of 1 rather than below it.
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

    # A raw distance of zero or None can't be divided into, and a trip that
    # short has no mileage worth protecting, so it never declines a match.
    coverage = (
        total_distance / raw_distance_m
        if raw_distance_m and raw_distance_m > 0
        else 1.0
    )
    covers_trip = coverage >= min_coverage

    # Three gates, three different failure modes. confidence = "matched, but
    # shakily". coverage = "matched a fragment and called it the trip": the
    # only gate that costs distance, and the only one measured against the
    # trip itself rather than against the response. fraction = "didn't match
    # at all" (OSRM tends to drop unmatchable spans as null tracepoints
    # rather than emit them as a separate low-confidence matching). fraction
    # predates coverage and is kept because it still fires on a trace OSRM
    # barely recognized, even where the little it did match is long enough to
    # clear coverage; 0.8 there remains a starting heuristic.
    low_conf = not covers_trip or worst_confidence < min_confidence or match_fraction < 0.8
    if not covers_trip:
        reason = f"snapped distance is only {coverage:.0%} of the raw distance"
    elif worst_confidence < min_confidence:
        reason = f"min matching confidence {worst_confidence:.2f} < {min_confidence}"
    elif low_conf:
        reason = f"only {match_fraction:.0%} of tracepoints matched"
    else:
        reason = None

    return SnapResult(
        status="low_confidence" if low_conf else "ok",
        # The partial geometry is kept even when its distance is not: it is
        # the only record of what OSRM could place, and the trip page draws
        # it underneath the raw track rather than in place of it.
        path_geojson={"type": "MultiLineString", "coordinates": lines} if lines else None,
        distance_m=total_distance if covers_trip else None,
        reason=reason,
    )


def _parse_and_serialize_match(body, min_confidence, input_count, raw_distance_m):
    matchings = body.get("matchings") or []
    tracepoints = body.get("tracepoints") or []
    if not isinstance(matchings, list) or not isinstance(tracepoints, list):
        raise ValueError("malformed OSRM match response")
    vertices = 0
    for matching in matchings:
        if not isinstance(matching, dict):
            raise ValueError("malformed OSRM matching")
        if (parse_finite_number(matching.get("distance", 0), minimum=0) is None
                or parse_finite_number(matching.get("confidence", 0), minimum=0, maximum=1) is None):
            raise ValueError("invalid OSRM distance or confidence")
        geometry = matching.get("geometry") or {}
        if not isinstance(geometry, dict):
            raise ValueError("malformed OSRM geometry")
        coordinates = geometry.get("coordinates") or []
        if not isinstance(coordinates, list):
            raise ValueError("malformed OSRM coordinates")
        vertices += len(coordinates)
        if vertices > PROVIDER_ROUTE_MAX_VERTICES:
            raise ProviderOutputTooLarge("provider route exceeds 100000 vertices")
        for coordinate in coordinates:
            if (not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2
                    or parse_finite_number(coordinate[0], minimum=-180, maximum=180) is None
                    or parse_finite_number(coordinate[1], minimum=-90, maximum=90) is None):
                raise ValueError("invalid OSRM coordinate")
    result = parse_match_response(body, min_confidence, input_count, raw_distance_m)
    geometry_json = json.dumps(result.path_geojson) if result.path_geojson else None
    return result, geometry_json


class SnapWorker:
    """Attempt one pending trip per account turn; durable attempt age orders retries.

    The HTTP call runs between short database borrows. Generation/CAS guards
    reject a result superseded by detector or device changes.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        http_client: httpx.AsyncClient,
        osrm_url: str,
        min_confidence: float,
        max_coords: int,
        retry_s: float = 300.0,
    ):
        self.pool = pool
        self.http = http_client
        self.osrm_url = osrm_url.rstrip("/")
        self.min_confidence = min_confidence
        self.max_coords = max_coords
        if not 2 <= max_coords <= 10_000:
            raise ValueError("max_coords must be between 2 and 10000")
        self.retry_s = retry_s

    async def run_once(self) -> BatchOutcome:
        return (await self.run_turn()).batch

    async def _next_trip(self, cursor=None):
        after_clause = ""
        args = [self.retry_s]
        async with self.pool.connection() as conn:
            args.append(account_id(conn))
            if cursor is not None:
                after_clause = "AND (COALESCE(t.snap_attempted_at,t.created_at),t.id) > (%s,%s) "
                args.extend(cursor)
            args.append(self.retry_s)
            cur = await conn.execute(
                "SELECT t.id, GREATEST(0, EXTRACT(EPOCH FROM "
                "t.snap_attempted_at + %s * interval '1 second' - now())), "
                "COALESCE(t.snap_attempted_at,t.created_at), t.snap_capacity_needed_bytes "
                "FROM trips t JOIN tracking_devices d "
                "ON d.account_id=t.account_id AND d.id=t.tracking_device_id "
                "WHERE t.account_id=%s AND t.snap_status='pending' "
                "AND t.source='detected' AND NOT t.imported AND d.enabled AND d.revoked_at IS NULL "
                + after_clause + "ORDER BY (t.snap_attempted_at IS NOT NULL AND "
                "t.snap_attempted_at + %s * interval '1 second' > now()), "
                "COALESCE(t.snap_attempted_at, t.created_at), t.id LIMIT 1",
                args,
            )
            row = await cur.fetchone()
        return row

    async def run_turn(self, cursor=None) -> TurnOutcome:
        row = await self._next_trip(cursor)
        if row is None:
            if cursor is not None and await self._next_trip() is not None:
                return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + self.retry_s)
            return TurnOutcome()
        trip_id, delay, selection_age, _ = row
        if delay > 0:
            return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + float(delay))
        outcome = await self._snap_one(trip_id)
        if not outcome.attempted:
            # Continue beyond an unadmitted row without recording a provider
            # attempt. One ordered cursor covers the round, then backoff
            # clears it so earlier rows can be revalidated without spinning.
            cursor = (selection_age, trip_id)
        next_row = await self._next_trip(cursor)
        if next_row is None:
            if cursor is not None and await self._next_trip() is not None:
                return TurnOutcome(batch=outcome,
                                   deferred_until=asyncio.get_running_loop().time() + self.retry_s)
            return TurnOutcome(batch=outcome)
        delay = float(next_row[1])
        if delay > 0:
            return TurnOutcome(batch=outcome,
                               deferred_until=asyncio.get_running_loop().time() + delay)
        return TurnOutcome(batch=outcome, ready=True, cursor=cursor)

    async def _point_count(self, conn, trip_id: int) -> int:
        cur = await conn.execute("SELECT count(*) " + _POINT_PREDICATE,
                                 (trip_id, account_id(conn)))
        return (await cur.fetchone())[0]

    async def _load_points(self, conn, trip_id: int) -> list[MatchPoint]:
        """Count and fetch exact sampled positions inside the caller's snapshot.

        Ranking still scans the trip in PostgreSQL; only selected points and
        decoded coordinates are returned to the application.
        """
        count = await self._point_count(conn, trip_id)
        ordinals = sample_ordinals(count, self.max_coords)
        if not ordinals:
            return []
        cur = await conn.execute(
            "WITH ranked AS (SELECT p.recorded_at, p.geom, p.accuracy_m, "
            "row_number() OVER (ORDER BY p.recorded_at) - 1 AS ordinal "
            + _POINT_PREDICATE + ") "
            "SELECT recorded_at, ST_Y(geom::geometry), ST_X(geom::geometry), accuracy_m "
            "FROM ranked WHERE ordinal = ANY(%s) ORDER BY ordinal",
            (trip_id, account_id(conn), ordinals),
        )
        return [MatchPoint(t=r[0], lat=r[1], lon=r[2], accuracy_m=r[3])
                for r in await cur.fetchall()]

    async def _pause_for_capacity(self, trip_id, generation, device_id,
                                  device_generation, needed_bytes):
        async with self.pool.connection() as conn:
            if not await lock_device_generation(conn, device_id, device_generation):
                return
            await conn.execute(
                "UPDATE trips SET snap_capacity_needed_bytes=%s,snap_attempted_at=now() "
                "WHERE account_id=%s AND id=%s AND tracking_device_id=%s "
                "AND updated_at=%s AND snap_status='pending' "
                "AND source='detected' AND NOT imported",
                (needed_bytes, account_id(conn), trip_id, device_id, generation),
            )

    async def _snap_one(self, trip_id: int) -> BatchOutcome:
        async with self.pool.connection(consistent_snapshot=True) as conn:
            # Trip token, point count and sampled rows share one snapshot.
            # The terminal write uses a fresh transaction and rejects any
            # rewrite that committed while the snapshot/provider was active.
            cur = await conn.execute(
                "SELECT t.updated_at, t.distance_m, t.tracking_device_id, d.generation, "
                "t.snap_capacity_needed_bytes FROM trips t "
                "JOIN tracking_devices d ON d.account_id=t.account_id AND d.id=t.tracking_device_id "
                "WHERE t.account_id=%s AND t.id=%s AND t.snap_status='pending' "
                "AND t.source='detected' AND NOT t.imported AND d.enabled AND d.revoked_at IS NULL",
                (account_id(conn), trip_id),
            )
            row = await cur.fetchone()
            if row is None:
                return BatchOutcome()
            generation, raw_distance_m, device_id, device_generation, needed_bytes = row
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
                    return BatchOutcome()
                cur = await conn.execute(
                    "UPDATE trips SET snap_status = 'failed', snapped_at = now(), "
                    "snap_capacity_needed_bytes = 0 "
                    "WHERE account_id = %s AND id = %s AND updated_at = %s",
                    (account_id(conn), trip_id, generation),
                )
            if cur.rowcount == 0:
                log.info(
                    "snap: trip %s was rewritten mid-snap, discarding stale "
                    "<2-point result", trip_id,
                )
                return BatchOutcome()
            log.warning("snap: trip %s has < 2 usable points, marking failed", trip_id)
            return BatchOutcome(attempted=1, completed=1)

        async with self.pool.connection() as conn:
            can_snap = await enhancement_available(
                conn, needed_bytes=max(SNAP_MIN_ROUTE_BYTES, needed_bytes),
            )
        if not can_snap:
            await self._pause_for_capacity(
                trip_id, generation, device_id, device_generation,
                max(SNAP_MIN_ROUTE_BYTES, needed_bytes),
            )
            return BatchOutcome()

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
        async with self.pool.connection() as conn:
            if not await lock_device_generation(conn, device_id, device_generation):
                return BatchOutcome()
            cur = await conn.execute(
                "UPDATE trips SET snap_attempted_at = now(), snap_capacity_needed_bytes = 0 "
                "WHERE account_id = %s AND id = %s AND tracking_device_id = %s "
                "AND snap_status = 'pending' AND source = 'detected' AND NOT imported "
                "AND updated_at = %s",
                (account_id(conn), trip_id, device_id, generation),
            )
        if cur.rowcount == 0:
            return BatchOutcome()
        try:
            body = await bounded_json(
                self.http, "GET", url, max_bytes=SNAP_RESPONSE_MAX_BYTES,
                allowed_statuses=(400,),
            )
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
            return BatchOutcome(attempted=1, retriable_failures=1, failure_type=type(e).__name__)
        if not isinstance(body, dict) or "code" not in body:
            log.warning("snap: trip %s got an unrecognized OSRM response, leaving pending", trip_id)
            return BatchOutcome(attempted=1, retriable_failures=1, failure_type="UnrecognizedResponse")

        try:
            result, geometry_json = await owned_thread(
                _parse_and_serialize_match, body, self.min_confidence,
                len(sampled), raw_distance_m,
            )
        except (TypeError, ValueError, KeyError) as exc:
            return BatchOutcome(attempted=1, retriable_failures=1,
                                failure_type=type(exc).__name__)
        geometry_bytes = 0
        if geometry_json is not None:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT octet_length(ST_AsEWKB(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 'NDR'))",
                    (geometry_json,),
                )
                geometry_bytes = (await cur.fetchone())[0]
        try:
            async with self.pool.connection() as conn:
                if not await lock_device_generation(conn, device_id, device_generation):
                    return BatchOutcome(attempted=1)
                cur = await conn.execute(
                    "UPDATE trips SET path_snapped = ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), "
                    " distance_snapped_m = %s, snap_status = %s, snapped_at = now(), "
                    "snap_capacity_needed_bytes = 0 "
                    "WHERE account_id = %s AND id = %s AND updated_at = %s",
                    (
                        geometry_json,
                        result.distance_m, result.status, account_id(conn), trip_id, generation,
                    ),
                )
        except Exception as exc:
            if not is_storage_capacity_error(exc):
                raise
            log.info("snap: trip %s remains pending at storage capacity", trip_id)
            await self._pause_for_capacity(
                trip_id, generation, device_id, device_generation, geometry_bytes,
            )
            return BatchOutcome(attempted=1)
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
            return BatchOutcome(attempted=1)
        if result.status != "ok":
            log.info("snap: trip %s -> %s (%s)", trip_id, result.status, result.reason)
        return BatchOutcome(attempted=1, completed=1)
