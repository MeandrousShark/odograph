"""Trailing arrival stays: a region or visit fix ends a trip before the phone's
next fix from the destination.

Boundary assertions use the same two-interval tolerance as test_detector.py.
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

from app.detector.core import Override, Params, Point, detect
from tests.synth import START, T0, Drive, Gap, Stationary, build_track, _offset

P = Params()
OFF = replace(P, arrival_triggers=frozenset())
INTERVAL = 15.0
TOL = timedelta(seconds=2 * INTERVAL)


def approx_t(actual, expected, tol=TOL):
    assert abs(actual - expected) <= tol, f"{actual} not within {tol} of {expected}"


def mark(points: list[Point], index: int, trigger: str) -> list[Point]:
    points = list(points)
    points[index] = replace(points[index], trigger=trigger)
    return points


def arrival(arrive_s: float, trigger: str | None = "v", at: int = -1) -> list[Point]:
    """Parked, a 5 km drive, then `arrive_s` at the destination."""
    pts = build_track([Stationary(duration_s=1200), Drive(km=5.0), Stationary(duration_s=arrive_s)])
    return mark(pts, at, trigger) if trigger else pts


def test_arrival_fix_ends_the_trip_with_a_provisional_stay():
    stays, trips = detect(arrival(120), P)

    assert len(trips) == 1
    assert [s.provisional for s in stays] == [False, True]
    approx_t(trips[0].ended_at, T0 + timedelta(seconds=1200 + 360))
    assert abs(trips[0].distance_m - 5000) / 5000 < 0.05


def test_region_fix_counts_like_a_visit():
    _, trips = detect(arrival(120, "c", at=-4), P)
    assert len(trips) == 1


def test_no_arrival_fix_waits_for_the_stay_minimum():
    stays, trips = detect(arrival(120, trigger=None), P)
    assert trips == []
    assert not any(s.provisional for s in stays)


def test_other_triggers_do_not_count():
    for trigger in ("t", "p", "u", "C"):
        _, trips = detect(arrival(120, trigger), P)
        assert trips == [], trigger


def test_arrival_under_the_minimum_span_waits():
    _, trips = detect(arrival(45), P)
    assert trips == []


def test_arrival_rule_can_be_turned_off():
    _, trips = detect(arrival(120), OFF)
    assert trips == []


def test_drive_through_region_fix_does_not_end_the_trip():
    pts = build_track([Stationary(duration_s=1200), Drive(km=5.0)])
    pts = mark(pts, len(pts) - 8, "c")
    _, trips = detect(pts, P)
    assert trips == []


def test_region_fix_at_the_newest_moving_point_does_not_end_the_trip():
    pts = build_track([Stationary(duration_s=1200), Drive(km=5.0)])
    _, trips = detect(mark(pts, -1, "c"), P)
    assert trips == []


def test_marker_before_an_unmarked_stop_does_not_count():
    """The region fix must be inside the trailing cluster, not just after
    the last stay."""
    pts = build_track([Stationary(duration_s=1200), Drive(km=5.0), Stationary(duration_s=120)])
    stop = next(i for i in range(len(pts) - 1, 0, -1) if (pts[-1].t - pts[i].t).total_seconds() > 150)
    _, trips = detect(mark(pts, stop - 3, "c"), P)
    assert trips == []


def test_overshoot_and_return_anchors_where_the_real_stay_will():
    """Passing the destination and coming back: the anchor is the first point
    whose radius run reaches the newest point, as the later real stay's is."""
    pts = build_track([
        Stationary(duration_s=1200), Drive(km=5.0), Drive(km=0.25, speed_kmh=20),
        Drive(km=0.25, speed_kmh=20, bearing_deg=270), Stationary(duration_s=120),
    ])
    pts = mark(pts, -1, "v")
    early_stays, early_trips = detect(pts, P)
    later = pts + [replace(pts[-1], t=pts[-1].t + timedelta(hours=1), trigger=None)]
    late_stays, late_trips = detect(later, P)
    assert early_stays[-1].provisional
    assert early_stays[-1].first_idx == late_stays[-1].first_idx
    assert early_trips == late_trips


def test_departure_after_a_short_stop_rejoins_one_trip():
    """A two-minute stop at a region, then driving on: the stop is not the end."""
    first = build_track([Stationary(duration_s=1200), Drive(km=5.0), Stationary(duration_s=120)])
    stop = len(first) - 1
    pts = build_track([
        Stationary(duration_s=1200), Drive(km=5.0), Stationary(duration_s=120),
        Drive(km=3.0), Stationary(duration_s=1800),
    ])
    stays, trips = detect(mark(pts, stop, "v"), P)

    assert len(trips) == 1
    assert abs(trips[0].distance_m - 8000) / 8000 < 0.05
    assert not any(s.provisional for s in stays)


def test_later_real_stay_keeps_the_trip_end():
    """When the next fix arrives an hour later, the provisional end stands."""
    pts = arrival(120)
    early_stays, early_trips = detect(pts, P)
    later = pts + [replace(pts[-1], t=pts[-1].t + timedelta(hours=1), trigger=None)]
    late_stays, late_trips = detect(later, P)

    assert early_stays[-1].provisional and not late_stays[-1].provisional
    assert early_stays[-1].first_idx == late_stays[-1].first_idx
    assert early_trips == late_trips


def test_never_left_is_not_an_arrival():
    """Just past the stay radius, but the path from the stay is shorter than it."""
    pts = build_track([
        Stationary(duration_s=1200, jitter_m=2.0), Drive(km=0.16, speed_kmh=10, jitter_m=1.0),
        Stationary(duration_s=120, jitter_m=2.0),
    ])
    stays, trips = detect(mark(pts, -1, "v"), P)
    assert trips == []
    assert not any(s.provisional for s in stays)


def test_merge_override_suppresses_a_provisional_stay():
    pts = arrival(120)
    end = pts[-1].t
    override = Override(kind="suppress", range_start=end - timedelta(seconds=90), range_end=end)
    stays, trips = detect(pts, P, [override])
    assert trips == []
    assert not any(s.provisional for s in stays)


# --- Arrival shapes from real traces, as relative offsets ---------------------

def _approach(rest_offsets: list[tuple[float, float, float | None, str | None]]) -> list[Point]:
    """A parked origin and a drive east, then points `(seconds, metres west
    of the resting fix, km/h, trigger)` relative to the newest fix."""
    pts = build_track([Stationary(duration_s=1200), Drive(km=6.0)])
    rest_t = pts[-1].t + timedelta(seconds=200)
    rest = _offset(pts[-1].lat, pts[-1].lon, east_m=170, north_m=0)
    for seconds, west_m, kmh, trigger in rest_offsets:
        lat, lon = _offset(*rest, east_m=-west_m, north_m=0)
        pts.append(Point(t=rest_t + timedelta(seconds=seconds), lat=lat, lon=lon,
                         accuracy_m=5.0, velocity_kmh=kmh, trigger=trigger))
    return pts


def test_traced_arrival_with_region_and_visit_fixes_closes():
    pts = _approach([
        (-143, 87, 18, None), (-141, 79, 11, "t"), (-135, 65, 21, "c"),
        (-133, 59, 25, "c"), (-38, 29, 0, None), (0, 0, None, "v"),
    ])
    stays, trips = detect(pts, P)
    assert len(trips) == 1
    assert stays[-1].provisional
    # Closed already at the stationary fix, before the visit fix.
    _, earlier = detect(pts[:-1], P)
    assert len(earlier) == 1


def test_traced_arrival_with_region_fixes_only_closes():
    pts = _approach([
        (-137, 143, 0, "t"), (-108, 27, 25, "c"), (-105, 14, 25, "c"), (0, 0, None, None),
    ])
    _, trips = detect(pts, P)
    assert len(trips) == 1


# --- Completed history is unchanged ------------------------------------------

def _random_track(rng: random.Random) -> list[Point]:
    segments = [Stationary(duration_s=rng.choice((600, 1200)))]
    for _ in range(rng.randint(2, 6)):
        segments.append(Drive(km=rng.uniform(0.2, 6.0), speed_kmh=rng.choice((20, 50, 90)),
                              bearing_deg=rng.uniform(0, 360)))
        kind = rng.random()
        if kind < 0.5:
            segments.append(Stationary(duration_s=rng.choice((30, 60, 90, 150, 240, 299))))
        elif kind < 0.8:
            segments.append(Stationary(duration_s=rng.choice((300, 900, 3600)), silent=rng.random() < 0.3))
        else:
            segments.append(Gap(duration_s=rng.uniform(60, 1800), move_km=rng.uniform(0, 2)))
    pts = build_track(segments, seed=rng.randrange(1 << 30))
    return [replace(p, trigger=rng.choice(("c", "v", "t", "p"))) if rng.random() < 0.15 else p
            for p in pts]


def test_arrival_rule_only_adds_a_trailing_stay_and_trip():
    """For any track, the rule may add one stay over the newest point and one
    trip into it; every earlier stay and trip is exactly as without it."""
    rng = random.Random(78)
    added = 0
    for _ in range(300):
        pts = _random_track(rng)
        old_stays, old_trips = detect(pts, OFF)
        new_stays, new_trips = detect(pts, P)

        assert new_stays[:len(old_stays)] == old_stays
        assert new_trips[:len(old_trips)] == old_trips
        assert len(new_stays) - len(old_stays) in (0, 1)
        assert len(new_trips) - len(old_trips) in (0, 1)
        if len(new_stays) > len(old_stays):
            assert new_stays[-1].provisional
            assert new_stays[-1].ended_at == max(p.t for p in pts)
            added += 1
        if len(new_trips) > len(old_trips):
            assert new_trips[-1].ended_at == new_stays[-1].started_at
    assert added > 10  # the property was exercised, not vacuous


def test_tracks_extended_with_a_departure_detect_as_without_the_rule():
    """The plan's history guarantee: once a later drive leaves the newest
    cluster, the rule changes nothing, so no reprocessing is needed."""
    rng = random.Random(7834)
    for _ in range(300):
        pts = _random_track(rng)
        last = pts[-1]
        departure = build_track(
            [Drive(km=rng.uniform(0.5, 3.0), speed_kmh=rng.choice((30, 50)), bearing_deg=rng.uniform(0, 360))],
            start=(last.lat, last.lon), t0=last.t, seed=rng.randrange(1 << 30))[1:]
        extended = pts + departure
        assert detect(extended, P) == detect(extended, OFF)
