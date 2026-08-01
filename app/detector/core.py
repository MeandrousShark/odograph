"""Pure trip-detection logic. No I/O, no DB.

Everything here operates on time-sorted point sequences and is deterministic,
which is what makes the synthetic-track unit tests meaningful.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Point:
    t: datetime  # timezone-aware
    lat: float
    lon: float
    accuracy_m: float | None = None
    velocity_kmh: float | None = None
    id: int | None = None  # DB id; None for synthetic points


@dataclass(frozen=True)
class Params:
    max_accuracy_m: float = 100.0
    max_speed_ms: float = 60.0
    stay_radius_m: float = 150.0
    stay_min_duration_s: float = 300.0
    min_trip_distance_m: float = 300.0
    gap_flag_threshold_s: float = 600.0
    # A sustained span below this speed is a stay even if it wanders beyond
    # stay_radius_m — this is what makes "park, then hike/walk" end a trip
    # instead of bundling the drives on either side.
    # 2.0 m/s = 7.2 km/h sits above hiking/brisk-walking pace but well below
    # any driving, including slow city traffic.
    walk_max_speed_ms: float = 2.0


@dataclass
class Stay:
    started_at: datetime
    ended_at: datetime
    lat: float  # centroid
    lon: float
    first_idx: int  # indices into the filtered point list
    last_idx: int
    point_count: int


@dataclass
class Trip:
    started_at: datetime  # last point inside origin stay
    ended_at: datetime    # first point inside destination stay
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    distance_m: float
    has_gap: bool
    points: list[Point] = field(repr=False, default_factory=list)


@dataclass(frozen=True)
class Override:
    """A durable detector-output instruction — replayed on
    every detect() call for a device rather than written once, since the
    dirty-window reprocess regenerates stays/trips from scratch on every
    run and would otherwise silently undo a one-off edit.

    'suppress' drops a real stay overlapping [range_start, range_end] (a
    merge — the stay used to separate two trips). 'force' pins point_id to
    act as a 1-point stay boundary even though no real stay was detected
    there (a split). 'discard' drops an assembled trip only when its span
    mutually overlaps the stored range by at least half of both durations;
    requiring both directions prevents a later, much larger trip that merely
    covers the deleted window from disappearing. point_id anchors to a
    specific `points.id` rather than a stay, because stays are
    deleted/reinserted every run and so aren't a stable reference; points
    never are.
    """
    kind: str  # "suppress" | "force" | "discard"
    range_start: datetime | None = None
    range_end: datetime | None = None
    point_id: int | None = None


def _dist(a: Point, b: Point) -> float:
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def filter_points(
    points: list[Point], params: Params, pinned_ids: frozenset[int] = frozenset()
) -> list[Point]:
    """Accuracy gate + teleport gate.

    The teleport comparison is against the last *kept* point, so a burst of
    consecutive jumped fixes is dropped as a group. Points sharing a second
    with the previous kept point are dropped (speed is undefined at dt=0).

    `pinned_ids` (force-split overrides) bypass both gates unconditionally.
    This must hold on every future run, not just the one where the split was
    made: the teleport gate's verdict on a point depends on whatever point
    precedes it, which can change as new neighboring data arrives later, so
    an unpinned split point could silently stop surviving filtering on a
    later reprocess.
    """
    kept: list[Point] = []
    for p in sorted(points, key=lambda p: p.t):
        if p.id is not None and p.id in pinned_ids:
            kept.append(p)
            continue
        if p.accuracy_m is not None and p.accuracy_m > params.max_accuracy_m:
            continue
        if kept:
            dt = (p.t - kept[-1].t).total_seconds()
            if dt <= 0:
                continue
            if _dist(kept[-1], p) / dt > params.max_speed_ms:
                continue
        kept.append(p)
    return kept


def find_stays(pts: list[Point], params: Params) -> list[Stay]:
    """Detect stays two ways, then combine.

    A stay is a period of not-driving, which happens in two forms:
    - *stationary*: points staying within stay_radius_m of an anchor (parked);
    - *on-foot*: a sustained span below walk_max_speed_ms, which may wander
      far from any anchor (walking/hiking) and so is invisible to the radius
      test — this is what stops a drive-park-hike-drive sequence from
      collapsing into one bundled trip.

    The two passes overlap (a parked stay is also low-speed); their index
    ranges are unioned, then near-touching stays split only by GPS jitter are
    merged. Dwell is always wall time between first and last point, never a
    point count — a two-point cluster hours apart (OwnTracks silent while
    parked) is a valid stay.
    """
    ranges = _stationary_ranges(pts, params) + _on_foot_ranges(pts, params)
    stays = [_build_stay(pts, a, b) for a, b in _union_ranges(ranges)]
    return _merge_boundary_splits(pts, stays, params)


def _build_stay(pts: list[Point], first: int, last: int) -> Stay:
    cluster = pts[first:last + 1]
    return Stay(
        started_at=cluster[0].t,
        ended_at=cluster[-1].t,
        lat=sum(p.lat for p in cluster) / len(cluster),
        lon=sum(p.lon for p in cluster) / len(cluster),
        first_idx=first,
        last_idx=last,
        point_count=len(cluster),
    )


def _stationary_ranges(pts: list[Point], params: Params) -> list[tuple[int, int]]:
    """Anchor-based spatial clustering: (first_idx, last_idx) index ranges."""
    ranges: list[tuple[int, int]] = []
    i, n = 0, len(pts)
    while i < n:
        j = i + 1
        while j < n and _dist(pts[j], pts[i]) <= params.stay_radius_m:
            j += 1
        if (pts[j - 1].t - pts[i].t).total_seconds() >= params.stay_min_duration_s:
            ranges.append((i, j - 1))
            i = j
        else:
            i += 1
    return ranges


def _on_foot_ranges(pts: list[Point], params: Params) -> list[tuple[int, int]]:
    """Maximal spans where every leg speed stays below walk_max_speed_ms and
    the span lasts at least stay_min_duration_s."""
    ranges: list[tuple[int, int]] = []
    i, n = 0, len(pts)
    while i < n - 1:
        j = i + 1
        while j < n:
            dt = (pts[j].t - pts[j - 1].t).total_seconds()
            if dt <= 0 or _dist(pts[j - 1], pts[j]) / dt > params.walk_max_speed_ms:
                break
            j += 1
        if (pts[j - 1].t - pts[i].t).total_seconds() >= params.stay_min_duration_s:
            ranges.append((i, j - 1))
        i = max(j, i + 1)
    return ranges


def _union_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge index ranges that overlap. Ranges that merely touch (adjacent
    indices, no shared point) are left to _merge_boundary_splits, which
    decides by physical distance rather than index adjacency."""
    merged: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _merge_boundary_splits(pts: list[Point], stays: list[Stay], params: Params) -> list[Stay]:
    """Merge consecutive stays separated only by jitter-scale movement.

    The anchor can land on the last moving fix short of the true stop, leaving
    the stationary cloud riding the radius boundary; jitter then splits one
    physical stay into two clusters a few meters apart. If the path between
    two stays is within the stay radius, it isn't a trip leaving and coming
    back — it's the same stay.
    """
    if not stays:
        return stays
    merged = [stays[0]]
    for s in stays[1:]:
        prev = merged[-1]
        seg = pts[prev.last_idx: s.first_idx + 1]
        between = sum(_dist(a, b) for a, b in zip(seg, seg[1:]))
        if between <= params.stay_radius_m:
            total = prev.point_count + s.point_count
            merged[-1] = Stay(
                started_at=prev.started_at,
                ended_at=s.ended_at,
                lat=(prev.lat * prev.point_count + s.lat * s.point_count) / total,
                lon=(prev.lon * prev.point_count + s.lon * s.point_count) / total,
                first_idx=prev.first_idx,
                last_idx=s.last_idx,
                point_count=total,
            )
        else:
            merged.append(s)
    return merged


def _apply_overrides(pts: list[Point], stays: list[Stay], overrides: list[Override]) -> list[Stay]:
    """Merge/split overrides, applied strictly after find_stays()'s full
    output — including its internal _merge_boundary_splits jitter-merge —
    and deliberately never followed by another jitter-merge pass. A user
    typically picks a split point at exactly the kind of brief, sub-radius
    stop that jitter-merging exists to collapse; re-running that pass here
    would immediately re-merge the injected boundary into its neighbor,
    silently no-opping every split at the cases it's meant for.
    """
    suppress = [o for o in overrides if o.kind == "suppress"]
    force = [o for o in overrides if o.kind == "force"]

    kept = [
        s for s in stays
        if not any(o.range_start <= s.ended_at and s.started_at <= o.range_end for o in suppress)
    ]

    for o in force:
        idx = next((i for i, p in enumerate(pts) if p.id == o.point_id), None)
        if idx is None:
            continue  # point not in this run's window — no-op, not an error
        p = pts[idx]
        if any(o.range_start <= p.t <= o.range_end for o in suppress):
            continue  # a suppress range covering this point means the merge wins
        if any(s.first_idx <= idx <= s.last_idx for s in kept):
            continue  # already inside a surviving stay
        kept.append(Stay(
            started_at=p.t, ended_at=p.t, lat=p.lat, lon=p.lon,
            first_idx=idx, last_idx=idx, point_count=1,
        ))

    return sorted(kept, key=lambda s: s.first_idx)


def assemble_trips(pts: list[Point], stays: list[Stay], params: Params) -> list[Trip]:
    """Trips are the segments between consecutive stays.

    Leading/trailing segments (before the first stay, after the last) are not
    emitted — they are incomplete by construction and resolve on a later run.
    """
    trips: list[Trip] = []
    for origin, dest in zip(stays, stays[1:]):
        seg = pts[origin.last_idx: dest.first_idx + 1]
        distance = sum(_dist(a, b) for a, b in zip(seg, seg[1:]))
        if distance < params.min_trip_distance_m:
            continue
        has_gap = any(
            (b.t - a.t).total_seconds() > params.gap_flag_threshold_s
            for a, b in zip(seg, seg[1:])
        )
        trips.append(Trip(
            started_at=seg[0].t,
            ended_at=seg[-1].t,
            start_lat=seg[0].lat,
            start_lon=seg[0].lon,
            end_lat=seg[-1].lat,
            end_lon=seg[-1].lon,
            distance_m=distance,
            has_gap=has_gap,
            points=seg,
        ))
    return trips


def _apply_discard_overrides(trips: list[Trip], overrides: list[Override]) -> list[Trip]:
    """Drop only the trip represented by each durable deletion range.

    Overlap must cover at least half of *both* spans. The detector's normal
    reconcile matcher uses half of the shorter span, which is intentionally
    too permissive here: after boundary changes, that rule could erase a
    materially larger real trip just because it contains the old deleted
    trip's window.
    """
    discard = [
        o for o in overrides
        if o.kind == "discard" and o.range_start is not None and o.range_end is not None
    ]
    if not discard:
        return trips

    def matches(trip: Trip, override: Override) -> bool:
        overlap_s = (
            min(trip.ended_at, override.range_end)
            - max(trip.started_at, override.range_start)
        ).total_seconds()
        trip_duration_s = (trip.ended_at - trip.started_at).total_seconds()
        override_duration_s = (override.range_end - override.range_start).total_seconds()
        return (
            overlap_s >= 0.5 * trip_duration_s
            and overlap_s >= 0.5 * override_duration_s
        )

    return [trip for trip in trips if not any(matches(trip, o) for o in discard)]


def detect(
    points: list[Point], params: Params, overrides: list[Override] | None = None
) -> tuple[list[Stay], list[Trip]]:
    """filter -> cluster stays -> apply merge/split overrides -> assemble
    trips. Input order is irrelevant. `overrides` defaults to None so every
    existing call site (the synthetic-track tests included) is unaffected."""
    overrides = overrides or []
    pinned_ids = frozenset(o.point_id for o in overrides if o.kind == "force" and o.point_id is not None)
    pts = filter_points(points, params, pinned_ids)
    stays = find_stays(pts, params)
    if overrides:
        stays = _apply_overrides(pts, stays, overrides)
    trips = assemble_trips(pts, stays, params)
    return stays, _apply_discard_overrides(trips, overrides)
