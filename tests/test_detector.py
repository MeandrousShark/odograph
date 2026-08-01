"""Trip-detection unit tests against synthetic tracks.

Boundary assertions use a tolerance of two sample intervals — exact-timestamp
assertions on clustered output are how these tests rot.
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

from app.detector.core import Override, Params, Point, detect, haversine_m
from tests.synth import START, T0, Drive, Gap, Stationary, build_track, _offset

P = Params()  # defaults: 150 m / 300 s stay, 300 m min trip, 600 s gap flag
INTERVAL = 15.0
TOL = timedelta(seconds=2 * INTERVAL)


def approx_t(actual, expected, tol=TOL):
    assert abs(actual - expected) <= tol, f"{actual} not within {tol} of {expected}"


# --- Core boundary detection -------------------------------------------------

def test_stationary_drive_stationary_one_trip():
    """Case 1: the canonical stay -> drive -> stay produces exactly one trip
    with boundaries at departure/arrival."""
    pts = build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ])
    stays, trips = detect(pts, P)

    assert len(trips) == 1
    assert len(stays) == 2
    trip = trips[0]
    approx_t(trip.started_at, T0 + timedelta(seconds=1200))          # departure
    approx_t(trip.ended_at, T0 + timedelta(seconds=1200 + 360))      # arrival (5km @ 50km/h)
    assert abs(trip.distance_m - 5000) / 5000 < 0.05
    assert not trip.has_gap
    # stay centroids near the true stationary positions
    assert haversine_m(stays[0].lat, stays[0].lon, *START) < 20
    dest = _offset(*START, east_m=5000, north_m=0)
    assert haversine_m(stays[1].lat, stays[1].lon, dest[0], dest[1]) < 20


def test_stationary_only_no_trip():
    """Case 2: parked the whole time."""
    pts = build_track([Stationary(duration_s=3600)])
    stays, trips = detect(pts, P)
    assert trips == []
    assert len(stays) == 1
    approx_t(stays[0].started_at, T0)
    approx_t(stays[0].ended_at, T0 + timedelta(seconds=3600))


def test_drive_only_held_incomplete():
    """Case 3: movement with no bracketing stays is not emitted."""
    pts = build_track([Drive(km=5.0)])
    stays, trips = detect(pts, P)
    assert stays == []
    assert trips == []


def test_two_trips_share_middle_stay():
    """Case 4: stay-drive-stay-drive-stay -> two trips meeting at the middle stay."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=3.0),
        Stationary(duration_s=1200),
        Drive(km=4.0, bearing_deg=180),
        Stationary(duration_s=900),
    ])
    stays, trips = detect(pts, P)
    assert len(stays) == 3
    assert len(trips) == 2
    t1, t2 = trips
    assert t1.ended_at <= t2.started_at
    middle = stays[1]
    assert middle.started_at <= t1.ended_at <= middle.ended_at
    assert middle.started_at <= t2.started_at <= middle.ended_at


# --- Threshold edges ----------------------------------------------------------

def test_short_stop_does_not_split():
    """Case 5: a stop of dwell - epsilon (long red light) keeps one trip."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=2.0),
        Stationary(duration_s=P.stay_min_duration_s - 45),
        Drive(km=2.0),
        Stationary(duration_s=900),
    ])
    stays, trips = detect(pts, P)
    assert len(trips) == 1
    assert len(stays) == 2
    # 10% tolerance: jitter legs during the sub-threshold stop accumulate
    # ~200 m of fake distance (~17 legs x ~14 m). Known, accepted over-read.
    assert abs(trips[0].distance_m - 4000) / 4000 < 0.10


def test_long_stop_splits():
    """Case 6: a stop of dwell + epsilon splits into two trips."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=2.0),
        Stationary(duration_s=P.stay_min_duration_s + 45),
        Drive(km=2.0),
        Stationary(duration_s=900),
    ])
    stays, trips = detect(pts, P)
    assert len(trips) == 2
    assert len(stays) == 3


def test_sub_minimum_movement_discarded():
    """Case 7: shuffling 200 m between two stays is not a trip. The first
    cluster swallows most of the short hop, so the boundary-split merge folds
    both stops into one spanning stay — either way, the contract is no trip."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=0.2, speed_kmh=10),
        Stationary(duration_s=900),
    ])
    stays, trips = detect(pts, P)
    assert trips == []
    assert len(stays) == 1
    approx_t(stays[0].started_at, T0)
    approx_t(stays[0].ended_at, pts[-1].t)


def test_silent_stay_two_points():
    """Case 8: significant-changes mode — a 2h silent stay of two points
    splits the surrounding movement into two trips, not one."""
    pts = build_track([
        Stationary(duration_s=600),
        Drive(km=5.0),
        Stationary(duration_s=7200, silent=True),
        Drive(km=5.0),
        Stationary(duration_s=600),
    ])
    stays, trips = detect(pts, P)
    assert len(stays) == 3
    assert len(trips) == 2
    silent = stays[1]
    assert (silent.ended_at - silent.started_at) >= timedelta(seconds=7200 - 2 * INTERVAL)


# --- On-foot / walking stays (v2) ---------------------------------------------

# A hike is just sustained movement at walking pace; Drive with a low speed_kmh
# models it (4 km/h ~ 1.1 m/s, below the 2.0 m/s walk threshold).

def test_drive_hike_drive_not_bundled():
    """The reported bug: park (briefly) then hike, and the drives on either
    side must not bundle into one trip. The short park alone is under the stay
    minimum, but park + hike form one continuous low-speed span that counts as
    a stay and ends the inbound drive."""
    pts = build_track([
        Stationary(duration_s=600),                       # home
        Drive(km=10.0, speed_kmh=50),                     # drive to trailhead
        Stationary(duration_s=120),                       # brief park (< 5 min)
        Drive(km=3.0, speed_kmh=4.0),                     # hike (45 min on foot)
        Stationary(duration_s=120),                       # brief return to car
        Drive(km=10.0, speed_kmh=50, bearing_deg=180),    # drive home
        Stationary(duration_s=1800),                      # home
    ])
    stays, trips = detect(pts, P)
    assert len(trips) == 2
    for t in trips:
        assert abs(t.distance_m - 10000) / 10000 < 0.05   # each ~10 km, hike excluded
    # the trailhead stay spans park + hike + return (one continuous slow span)
    trailhead = stays[1]
    assert (trailhead.ended_at - trailhead.started_at) >= timedelta(minutes=40)


def test_continuous_drive_not_split_by_walk_detector():
    """A single uninterrupted highway drive has no sub-threshold slow span, so
    on-foot detection must not carve phantom stays out of it."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=40.0, speed_kmh=90),
        Stationary(duration_s=900),
    ])
    stays, trips = detect(pts, P)
    assert len(trips) == 1
    assert len(stays) == 2


def test_walk_below_trip_minimum_is_a_stay_not_a_trip():
    """A short walk between two drives (e.g. moving the car, walking the dog)
    that stays under walking speed is absorbed as a stay, not surfaced as a
    sub-minimum trip."""
    pts = build_track([
        Stationary(duration_s=600),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=120),
        Drive(km=0.6, speed_kmh=4.0),   # 9 min walk, > 300 m but on foot
        Stationary(duration_s=120),
        Drive(km=5.0, speed_kmh=50, bearing_deg=180),
        Stationary(duration_s=600),
    ])
    stays, trips = detect(pts, P)
    assert len(trips) == 2  # the two 5 km drives; the walk is not a trip
    for t in trips:
        assert abs(t.distance_m - 5000) / 5000 < 0.05


# --- Noise robustness ----------------------------------------------------------

def _teleported(p: Point, km_north: float) -> Point:
    lat, lon = _offset(p.lat, p.lon, east_m=0, north_m=km_north * 1000)
    return replace(p, lat=lat, lon=lon)


def test_teleport_during_stay_ignored():
    """Case 9: a single 5 km GPS jump mid-stay does not break the stay."""
    pts = build_track([Stationary(duration_s=1800)])
    mid = len(pts) // 2
    pts[mid] = _teleported(pts[mid], 5.0)
    stays, trips = detect(pts, P)
    assert trips == []
    assert len(stays) == 1
    approx_t(stays[0].started_at, T0)
    approx_t(stays[0].ended_at, T0 + timedelta(seconds=1800))


def test_low_accuracy_wander_ignored():
    """Case 10: accuracy-gated points can't fake movement."""
    pts = build_track([Stationary(duration_s=1800)])
    for i in (20, 40, 60):
        lat, lon = _offset(pts[i].lat, pts[i].lon, east_m=400, north_m=200)
        pts[i] = replace(pts[i], lat=lat, lon=lon, accuracy_m=500.0)
    stays, trips = detect(pts, P)
    assert trips == []
    assert len(stays) == 1


def test_teleport_burst_during_drive():
    """Case 11: a 3-point teleport burst mid-drive is dropped as a group and
    doesn't inflate distance."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=5.0),
        Stationary(duration_s=900),
    ])
    mid = len(pts) // 2
    for i in range(mid, mid + 3):
        pts[i] = _teleported(pts[i], 5.0)
    stays, trips = detect(pts, P)
    assert len(trips) == 1
    assert abs(trips[0].distance_m - 5000) / 5000 < 0.05


# --- Ordering & gaps -----------------------------------------------------------

def test_shuffled_input_identical_output():
    """Case 12: detection is a function of the point set, not arrival order."""
    pts = build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0),
        Stationary(duration_s=1200),
    ])
    shuffled = pts[:]
    random.Random(7).shuffle(shuffled)
    stays_a, trips_a = detect(pts, P)
    stays_b, trips_b = detect(shuffled, P)
    assert [(s.started_at, s.ended_at) for s in stays_a] == \
           [(s.started_at, s.ended_at) for s in stays_b]
    assert [(t.started_at, t.ended_at, t.distance_m) for t in trips_a] == \
           [(t.started_at, t.ended_at, t.distance_m) for t in trips_b]


def _with_ids(pts: list[Point]) -> list[Point]:
    """Synthetic points default to id=None; overrides anchor on points.id,
    so override tests need real (if arbitrary) ids to pin/match against."""
    return [replace(p, id=i) for i, p in enumerate(pts)]


# --- merge/split overrides --------------------------------------------------

def test_suppress_override_merges_two_trips():
    """Case 16: suppressing the stay between two organic trips merges them
    into one, with combined distance and no leftover middle stay."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=3.0),
        Stationary(duration_s=400),  # short: keeps jitter-leg accumulation small
        Drive(km=4.0, bearing_deg=180),
        Stationary(duration_s=900),
    ])
    stays0, trips0 = detect(pts, P)
    assert len(trips0) == 2
    middle = stays0[1]

    override = Override(kind="suppress", range_start=middle.started_at, range_end=middle.ended_at)
    stays1, trips1 = detect(pts, P, overrides=[override])
    assert len(stays1) == 2
    assert len(trips1) == 1
    # The merged trip's path now crosses the former stay's dwell, whose many
    # jittered legs (Stationary's default jitter_m=8) add real accumulated
    # distance on top of the 7 km of actual driving — a bigger tolerance
    # than the 5% used for pure-drive assertions elsewhere in this file.
    assert abs(trips1[0].distance_m - 7000) / 7000 < 0.15


def test_force_override_splits_one_trip():
    """Case 17: pinning an arbitrary mid-drive point forces a split into two
    trips, each still well above the minimum trip distance."""
    pts = _with_ids(build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ]))
    stays0, trips0 = detect(pts, P)
    assert len(trips0) == 1

    mid = trips0[0].points[len(trips0[0].points) // 2]
    override = Override(kind="force", point_id=mid.id)
    stays1, trips1 = detect(pts, P, overrides=[override])
    assert len(trips1) == 2
    assert trips1[0].ended_at == mid.t == trips1[1].started_at
    total = trips1[0].distance_m + trips1[1].distance_m
    assert abs(total - 5000) / 5000 < 0.10


def test_force_override_survives_accuracy_gate():
    """Case 18: a pinned point that would normally fail the accuracy gate is
    dropped as usual with no override, but forces a split once pinned."""
    pts = _with_ids(build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ]))
    _, baseline_trips = detect(pts, P)
    assert len(baseline_trips) == 1
    # Pick a point from the assembled trip's own point list, so it's
    # guaranteed to sit mid-*drive* rather than inside either bracketing
    # stay (a plain index into the whole track could land in either).
    mid = baseline_trips[0].points[len(baseline_trips[0].points) // 2]
    degraded = replace(mid, accuracy_m=500.0)
    pts = [degraded if p.id == mid.id else p for p in pts]

    _, baseline_trips2 = detect(pts, P)
    assert len(baseline_trips2) == 1  # gate drops it as always, no override

    override = Override(kind="force", point_id=degraded.id)
    _, trips1 = detect(pts, P, overrides=[override])
    assert len(trips1) == 2


def test_force_override_survives_teleport_gate():
    """Case 19: a pinned point that would normally fail the teleport gate is
    dropped as usual with no override, but still forces a split once pinned
    (even though the resulting geometry is nonsensical — this tests the
    filtering mechanism, not physical realism)."""
    pts = _with_ids(build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ]))
    _, baseline_trips = detect(pts, P)
    assert len(baseline_trips) == 1
    mid = baseline_trips[0].points[len(baseline_trips[0].points) // 2]
    lat, lon = _offset(mid.lat, mid.lon, east_m=0, north_m=5000)
    teleported = replace(mid, lat=lat, lon=lon)
    pts = [teleported if p.id == mid.id else p for p in pts]

    _, baseline_trips2 = detect(pts, P)
    assert len(baseline_trips2) == 1  # gate drops it as always, no override

    override = Override(kind="force", point_id=teleported.id)
    _, trips1 = detect(pts, P, overrides=[override])
    assert len(trips1) == 2


def test_suppress_range_drift_robustness():
    """Case 20: late data arriving after the stored suppress range's original
    end shifts the recomputed stay's exact boundary; the override's overlap
    test (not an exact-range match) still finds and suppresses it."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=3.0),
        Stationary(duration_s=1200),
        Drive(km=4.0, bearing_deg=180),
        Stationary(duration_s=900),
    ])
    stays0, trips0 = detect(pts, P)
    assert len(trips0) == 2
    middle = stays0[1]
    override = Override(kind="suppress", range_start=middle.started_at, range_end=middle.ended_at)

    # Late-arriving point at the same stay location, timestamped just after
    # the originally recomputed stay's end -> this run's recomputed stay
    # extends later than the override's stored range_end.
    extra = Point(
        t=middle.ended_at + timedelta(seconds=1), lat=middle.lat, lon=middle.lon,
        accuracy_m=10.0, velocity_kmh=0.0,
    )
    pts2 = pts + [extra]
    stays_plain, _ = detect(pts2, P)
    assert stays_plain[1].ended_at > middle.ended_at  # confirms drift happened

    stays2, trips2 = detect(pts2, P, overrides=[override])
    assert len(trips2) == 1  # merge still holds despite the drift


def test_two_suppress_overrides_merge_three_trips():
    """Case 21: two suppress overrides compose to merge three organic trips
    into one."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=3.0),
        Stationary(duration_s=1200),
        Drive(km=4.0, bearing_deg=180),
        Stationary(duration_s=900),
        Drive(km=2.0, bearing_deg=90),
        Stationary(duration_s=900),
    ])
    stays0, trips0 = detect(pts, P)
    assert len(trips0) == 3
    assert len(stays0) == 4

    overrides = [
        Override(kind="suppress", range_start=stays0[1].started_at, range_end=stays0[1].ended_at),
        Override(kind="suppress", range_start=stays0[2].started_at, range_end=stays0[2].ended_at),
    ]
    stays1, trips1 = detect(pts, P, overrides=overrides)
    assert len(stays1) == 2
    assert len(trips1) == 1


def test_force_override_inside_surviving_stay_is_noop():
    """Case 22: a force override pinning a point already inside a surviving
    stay changes nothing."""
    pts = _with_ids(build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ]))
    stays0, trips0 = detect(pts, P)
    origin_stay_point = pts[stays0[0].first_idx]

    override = Override(kind="force", point_id=origin_stay_point.id)
    stays1, trips1 = detect(pts, P, overrides=[override])
    assert len(stays1) == len(stays0)
    assert len(trips1) == len(trips0)
    assert trips1[0].started_at == trips0[0].started_at
    assert trips1[0].ended_at == trips0[0].ended_at


def test_overrides_matching_nothing_are_a_noop():
    """Case 23: a suppress range and a force point_id that both match
    nothing in this run's window leave output identical to no overrides."""
    pts = _with_ids(build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ]))
    stays0, trips0 = detect(pts, P)

    overrides = [
        Override(kind="suppress", range_start=T0 - timedelta(days=1), range_end=T0 - timedelta(hours=1)),
        Override(kind="force", point_id=999_999),
    ]
    stays1, trips1 = detect(pts, P, overrides=overrides)
    assert [(s.started_at, s.ended_at) for s in stays1] == \
           [(s.started_at, s.ended_at) for s in stays0]
    assert [(t.started_at, t.ended_at, t.distance_m) for t in trips1] == \
           [(t.started_at, t.ended_at, t.distance_m) for t in trips0]


def test_overrides_none_empty_and_omitted_are_identical():
    """Case 24: back-compat guard — every existing call site (overrides
    omitted) must behave identically to overrides=None and overrides=[]."""
    pts = build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ])
    a = detect(pts, P)
    b = detect(pts, P, overrides=None)
    c = detect(pts, P, overrides=[])
    for stays, trips in (b, c):
        assert [(s.started_at, s.ended_at) for s in stays] == \
               [(s.started_at, s.ended_at) for s in a[0]]
        assert [(t.started_at, t.ended_at) for t in trips] == \
               [(t.started_at, t.ended_at) for t in a[1]]


def test_split_then_merge_back_yields_one_trip():
    pts = _with_ids(build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ]))
    _, original = detect(pts, P)
    mid = original[0].points[len(original[0].points) // 2]
    force = Override(kind="force", point_id=mid.id)
    _, split = detect(pts, P, overrides=[force])
    assert len(split) == 2

    suppress = Override(kind="suppress", range_start=mid.t, range_end=mid.t)
    _, merged = detect(pts, P, overrides=[force, suppress])
    assert len(merged) == 1
    assert (merged[0].started_at, merged[0].ended_at) == (
        original[0].started_at, original[0].ended_at
    )


# --- Durable detected-trip deletion overrides -------------------------------

def _single_trip_track() -> list[Point]:
    return build_track([
        Stationary(duration_s=1200),
        Drive(km=5.0, speed_kmh=50),
        Stationary(duration_s=1800),
    ])


def test_discard_override_drops_exact_trip_without_changing_stays():
    pts = _single_trip_track()
    original_stays, original_trips = detect(pts, P)
    assert len(original_trips) == 1
    trip = original_trips[0]

    override = Override(
        kind="discard", range_start=trip.started_at, range_end=trip.ended_at
    )
    stays, trips = detect(pts, P, overrides=[override])

    assert trips == []
    assert [(s.started_at, s.ended_at) for s in stays] == [
        (s.started_at, s.ended_at) for s in original_stays
    ]


def test_discard_override_requires_half_of_both_spans_inclusively():
    pts = _single_trip_track()
    _, original = detect(pts, P)
    trip = original[0]
    duration = trip.ended_at - trip.started_at

    exact_half = Override(
        kind="discard",
        range_start=trip.started_at + duration / 2,
        range_end=trip.ended_at + duration / 2,
    )
    _, at_boundary = detect(pts, P, overrides=[exact_half])
    assert at_boundary == []

    below_half = Override(
        kind="discard",
        range_start=trip.started_at + duration / 2 + timedelta(seconds=1),
        range_end=trip.ended_at + duration / 2 + timedelta(seconds=1),
    )
    _, below_boundary = detect(pts, P, overrides=[below_half])
    assert len(below_boundary) == 1


def test_discard_override_does_not_swallow_larger_covering_trip_or_noop_range():
    pts = _single_trip_track()
    _, original = detect(pts, P)
    trip = original[0]
    duration = trip.ended_at - trip.started_at

    small_covered_window = Override(
        kind="discard",
        range_start=trip.started_at + duration * 0.4,
        range_end=trip.started_at + duration * 0.6,
    )
    nonoverlapping = Override(
        kind="discard",
        range_start=trip.ended_at + timedelta(hours=1),
        range_end=trip.ended_at + timedelta(hours=2),
    )
    _, trips = detect(pts, P, overrides=[small_covered_window, nonoverlapping])

    assert len(trips) == 1
    assert (trips[0].started_at, trips[0].ended_at) == (
        trip.started_at, trip.ended_at
    )


def test_moving_gap_flags_trip():
    """Case 14: a 15-min recording gap with large displacement stays one trip,
    flagged has_gap (contrast with case 8: small displacement + gap = stay)."""
    pts = build_track([
        Stationary(duration_s=900),
        Drive(km=2.0),
        Gap(duration_s=900, move_km=2.0),
        Drive(km=2.0),
        Stationary(duration_s=900),
    ])
    stays, trips = detect(pts, P)
    assert len(stays) == 2
    assert len(trips) == 1
    assert trips[0].has_gap
    assert abs(trips[0].distance_m - 6000) / 6000 < 0.05
