"""The on-foot rule: the capped speed window, the departure trim and transit legs.

The first half fixes distances exactly by treating a point's latitude as meters
along a line, so a boundary sits on its boundary. The second half runs the real
detect() over synthetic tracks.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.detector import core
from app.detector.core import Params, Point, detect, haversine_m
from tests.synth import START, T0, Drive, Gap, Stationary, _travel, build_track

P = Params()
PARK = Stationary(duration_s=600)


@pytest.fixture
def line(monkeypatch):
    monkeypatch.setattr(core, "_dist", lambda a, b: abs(a.lat - b.lat))


def _line(*legs: tuple[float, float]) -> list[Point]:
    """Points from (seconds, meters) legs."""
    t, meters = T0, 0.0
    pts = [Point(t=t, lat=meters, lon=0.0)]
    for seconds, length in legs:
        t += timedelta(seconds=seconds)
        meters += length
        pts.append(Point(t=t, lat=meters, lon=0.0))
    return pts


def _ranges(*legs: tuple[float, float], params: Params = P) -> list[tuple[int, int]]:
    return core._on_foot_ranges(_line(*legs), params)


SLOW = (300, 300)  # alone qualifies as a stay at 1 m/s


def test_leg_exactly_at_the_cap_is_forgiven(line):
    legs = [(60, 60)] * 5 + [(30, 90)] + [(60, 60)] * 5
    assert _ranges(*legs) == [(0, 11)]


def test_leg_over_the_cap_ends_the_span_whatever_the_average(line):
    legs = [(60, 60)] * 5 + [(30, 91)] + [(60, 60)] * 5
    assert _ranges(*legs) == [(0, 5), (6, 11)]


def test_average_exactly_at_the_limit_passes(line):
    assert _ranges((240, 420), (60, 180), SLOW) == [(0, 3)]


def test_average_over_the_limit_ends_the_span(line):
    assert _ranges((240, 421), (60, 180), SLOW) == [(2, 3)]


def test_less_history_than_the_window_averages_what_exists(line):
    assert _ranges((10, 20), (10, 30), SLOW) == [(2, 3)]


def test_oldest_leg_counts_whole(line):
    # Clipping the oldest leg to the window, or dropping it, averages over 2 m/s.
    assert _ranges((1000, 100), (140, 280), (150, 440), SLOW) == [(0, 4)]


def test_window_follows_the_stay_duration(line):
    legs = [(60, 60), (60, 120), (30, 90), SLOW]
    assert _ranges(*legs) == [(0, 4)]
    assert _ranges(*legs, params=Params(stay_min_duration_s=30.0)) == [(0, 2), (3, 4)]


def test_broken_span_restarts_with_no_history(line):
    assert _ranges(SLOW, (10, 100), (10, 30), SLOW) == [(0, 1), (3, 4)]


def test_history_stops_at_a_transit_leg(line):
    assert _ranges(SLOW, (600, 900), (10, 30), SLOW) == [(0, 1), (3, 4)]


def test_a_cap_below_the_walk_limit_forgives_nothing(line):
    legs = [(60, 60)] * 5 + [(30, 90)] + [(60, 60)] * 5
    assert _ranges(*legs, params=Params(walk_leg_max_speed_ms=1.0)) == [(0, 5), (6, 11)]


def test_nonpositive_leg_still_ends_a_span(line):
    pts = _line(SLOW)
    pts += [Point(t=pts[-1].t, lat=301.0, lon=0.0), Point(t=pts[-1].t + timedelta(seconds=300), lat=500.0, lon=0.0)]
    assert core._on_foot_ranges(pts, P) == [(0, 1), (2, 3)]


def test_pass_stays_linear(line, monkeypatch):
    calls = 0

    def counted(a, b):
        nonlocal calls
        calls += 1
        return abs(a.lat - b.lat)

    monkeypatch.setattr(core, "_dist", counted)
    legs = [(10, 10)] * 20_000
    assert _ranges(*legs) == [(0, 20_000)]
    assert calls <= 3 * len(legs)


def test_span_end_moves_back_over_a_forgiven_leg(line):
    assert _ranges(SLOW, (60, 170), (60, 170), (10, 100), SLOW) == [(0, 1), (4, 5)]


def test_span_end_moves_back_at_the_end_of_the_data(line):
    assert _ranges(SLOW, (60, 170)) == [(0, 1)]


def test_trim_can_leave_too_little_to_be_a_stay(line):
    assert _ranges((200, 200), (60, 170), (60, 170), (10, 100)) == []


def test_transit_leg_splits_a_span(line):
    assert _ranges(SLOW, (900, 1000), SLOW) == [(0, 1), (2, 3)]


def test_piece_before_a_transit_leg_is_a_stay_at_one_point(line):
    assert _ranges((900, 1000), SLOW) == [(0, 0), (1, 2)]


def test_piece_after_the_last_transit_leg_must_qualify_alone(line):
    assert _ranges(SLOW, (900, 1000), (299, 299)) == [(0, 1)]


def test_every_piece_before_a_transit_leg_is_a_stay(line):
    assert _ranges((900, 1000), (10, 10), (900, 1000)) == [(0, 0), (1, 2)]


@pytest.mark.parametrize(
    ("seconds", "meters", "params", "transit"),
    [
        (900, 801, P, True),
        (900, 800, P, False),
        (1800, 801, P, True),
        (1801, 801, P, False),
        (300, 401, Params(transit_leg_min_m=400.0), True),
        (299, 401, Params(transit_leg_min_m=400.0), False),
        (900, 401, Params(transit_leg_min_m=400.0, transit_leg_max_s=899.0), False),
    ],
)
def test_transit_leg_limits(line, seconds, meters, params, transit):
    expected = [(0, 1), (2, 3)] if transit else [(0, 3)]
    assert _ranges(SLOW, (seconds, meters), SLOW, params=params) == expected


def _km(trips) -> float:
    return round(sum(t.distance_m for t in trips) / 1000, 2)


def _uneven_walk(speed_ms: float, every_m: float = 80.0) -> list[Point]:
    """A report per every_m out and back, positions even and times not, so one
    leg reads fast and the next slow while the true pace holds."""
    pts = [Point(t=T0 + timedelta(seconds=15 * i), lat=START[0], lon=START[1], accuracy_m=10.0)
           for i in range(40)]
    t, (lat, lon) = pts[-1].t, START
    spread = (0.7, 1.3, 0.8, 1.2, 0.6, 1.4)
    for i in range(44):
        t += timedelta(seconds=spread[i % len(spread)] * every_m / speed_ms)
        lat, lon = _travel(lat, lon, every_m, 90.0 if i < 22 else 270.0)
        pts.append(Point(t=t, lat=lat, lon=lon, accuracy_m=10.0))
    pts += [Point(t=t + timedelta(seconds=15 * (i + 1)), lat=lat, lon=lon, accuracy_m=10.0)
            for i in range(40)]
    return pts


@pytest.mark.parametrize("speed_ms", [1.5, 1.7])
def test_uneven_sparse_walk_makes_no_trip(speed_ms):
    pts = _uneven_walk(speed_ms)
    assert any(haversine_m(a.lat, a.lon, b.lat, b.lon) / (b.t - a.t).total_seconds() > 2.0
               for a, b in zip(pts, pts[1:]))

    assert detect(pts, P)[1] == []
    assert detect(pts, Params(walk_leg_max_speed_ms=0.0))[1] != []


def _jam(stopped_s: float, creep_m: float) -> list:
    segments, elapsed = [], 0.0
    while elapsed < 900:
        segments += [Stationary(duration_s=stopped_s, jitter_m=4.0),
                     Drive(km=creep_m / 1000, speed_kmh=28.8)]
        elapsed += stopped_s + creep_m / 8.0
    return segments


@pytest.mark.parametrize(
    ("stopped_s", "creep_m", "trips", "km"),
    [(45, 40, 1, 5.00), (90, 60, 1, 4.94), (150, 120, 2, 4.13)],
)
def test_jams_detect_as_before(stopped_s, creep_m, trips, km):
    pts = build_track([PARK, Drive(2), *_jam(stopped_s, creep_m), Drive(2), PARK])

    detected = detect(pts, P)[1]

    assert (len(detected), _km(detected)) == (trips, km)


@pytest.mark.parametrize(
    ("around_gap", "trips", "km"),
    [
        ([Gap(900, 1)], 2, 4.87),
        ([Drive(.01, 1.5), Gap(900, 1)], 2, 4.88),
        ([Gap(900, 1), Drive(.01, 1.5)], 2, 4.89),
        ([Stationary(600), Gap(900, 1)], 2, 4.76),
        ([Gap(900, 1), Stationary(600)], 3, 4.88),
    ],
    ids=["gap", "slow-then-gap", "gap-then-slow", "stop-then-gap", "gap-then-stop"],
)
def test_transit_leg_keeps_its_miles_and_its_flag(around_gap, trips, km):
    pts = build_track([PARK, Drive(2), *around_gap, Drive(2), PARK])

    detected = detect(pts, P)[1]

    assert (len(detected), _km(detected)) == (trips, km)
    assert sum(t.has_gap for t in detected) == 1


@pytest.mark.parametrize(
    "gap",
    [Gap(28800, .5), Gap(1801, 1), Gap(360, .5)],
    ids=["silent-8-hours", "over-30-minutes", "under-the-floor"],
)
def test_other_slow_legs_stay_inside_a_stay(gap):
    detected = detect(build_track([PARK, Drive(2), gap, Drive(2), PARK]), P)[1]

    assert len(detected) == 2
    assert not any(t.has_gap for t in detected)
    assert _km(detected) < 4


def test_silent_stay_gives_no_trip_an_overnight_leg():
    detected = detect(build_track([PARK, Drive(2), Gap(28800, 5), Drive(2), PARK]), P)[1]

    assert len(detected) == 2
    assert max(t.distance_m for t in detected) < 2500


def test_departure_after_a_forgiven_leg_stays_in_the_trip():
    # The walk ends with a leg the window forgives, then the drive pulls away.
    pts = build_track([PARK, Drive(1, 5, jitter_m=0.5)])
    t, lat, lon = pts[-1].t, pts[-1].lat, pts[-1].lon
    for seconds, meters in [(30, 80), (15, 300), (15, 300), (15, 300)]:
        t += timedelta(seconds=seconds)
        lat, lon = _travel(lat, lon, meters, 90.0)
        pts.append(Point(t=t, lat=lat, lon=lon, accuracy_m=10.0))
    forgiven_from = pts[-5].t
    pts += build_track([Stationary(600, jitter_m=0.5)], start=(lat, lon), t0=t)[1:]

    trip = detect(pts, P)[1][0]

    assert trip.started_at == forgiven_from


@pytest.mark.parametrize(
    "track",
    [
        [PARK, Drive(2), Gap(900, 1), Drive(2), PARK],
        [PARK, Drive(2), Drive(.01, 1.5), Gap(900, 1), Drive(2), PARK],
        [PARK, Drive(2), Gap(900, 1), Stationary(600), Drive(2), PARK],
        [PARK, Drive(1, 7, jitter_m=8), Drive(2), PARK],
    ],
    ids=["gap", "slow-then-gap", "gap-then-stop", "brisk-then-drive"],
)
def test_every_stay_start_rebuilds_the_rest_of_the_track(track):
    """The runner rewinds to a settled stay's start. A window opened there must
    rebuild the same later stays and trips, or the next run loses them."""
    pts = build_track(track)
    stays, trips = detect(pts, P)
    assert stays

    for stay in stays:
        window = [p for p in pts if p.t >= stay.started_at]
        rebuilt_stays, rebuilt_trips = detect(window, P)
        assert [(s.started_at, s.ended_at) for s in rebuilt_stays] == [
            (s.started_at, s.ended_at) for s in stays if s.started_at >= stay.started_at]
        assert [(t.started_at, t.ended_at, t.distance_m) for t in rebuilt_trips] == [
            (t.started_at, t.ended_at, t.distance_m) for t in trips
            if t.started_at >= stay.started_at]
