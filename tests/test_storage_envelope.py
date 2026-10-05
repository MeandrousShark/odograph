"""Exercise the detector's structural storage bound using its real output."""
from dataclasses import replace
from datetime import timedelta
import random
import struct

import pytest

from app.detector.core import Override, Params, Point, detect
from tests.synth import Drive, Gap, Stationary, T0, build_track


def _point_ewkb(lon, lat):
    return struct.pack('<BII2d', 1, 0x20000001, 4326, lon, lat)


def _path_ewkb(points):
    return struct.pack('<BIII', 1, 0x20000002, 4326, len(points)) + b''.join(
        struct.pack('<2d', p.lon, p.lat) for p in points
    )


def _assert_output_fits(points, params, overrides=(), label='phone'):
    stays, trips = detect(points, params, list(overrides))
    n = len(points)
    vertices = sum(len(t.points) for t in trips)
    assert len(stays) <= n
    assert len(trips) <= max(n - 1, 0)
    assert vertices <= 2 * n
    copied_label = label.encode('utf-8')
    core = sum(256 + len(_point_ewkb(s.lon, s.lat)) + len(copied_label) for s in stays)
    core += sum(
        512 + len(_point_ewkb(t.start_lon, t.start_lat))
        + len(_point_ewkb(t.end_lon, t.end_lat)) + len(_path_ewkb(t.points))
        + len(copied_label) for t in trips
    )
    assert core <= n * (1024 + 2 * len(copied_label))
    return stays, trips, core


@pytest.mark.parametrize('n', [0, 1, 2, 3, 31, 257])
@pytest.mark.parametrize('label', ['phone', '\U0001f680' * 100])
def test_every_point_can_be_a_shared_forced_boundary(n, label):
    points = [Point(T0 + timedelta(seconds=15 * i), 45.5, -122.6 + i * .004,
                    id=i + 1) for i in range(n)]
    params = replace(Params(), min_trip_distance_m=0)
    overrides = [Override('force', point_id=p.id) for p in points]
    stays, trips, core = _assert_output_fits(points, params, overrides, label)
    assert len(stays) == n
    assert len(trips) == max(n - 1, 0)
    assert sum(len(t.points) for t in trips) == 2 * max(n - 1, 0)
    if n == 257 and len(label.encode('utf-8')) == 400:
        assert core > n * 1024  # A flat reserve cannot fund valid copied labels.


@pytest.mark.parametrize('seed', range(12))
def test_mixed_stays_gaps_filters_and_overrides_fit_each_retained_window(seed):
    rng = random.Random(seed)
    segments = []
    for i in range(7):
        segments.extend([Stationary(450, silent=i % 2 == 0),
                         Drive(rng.uniform(.5, 3), bearing_deg=rng.randrange(360))])
        if i % 3 == 0:
            segments.append(Gap(900))
    segments.append(Stationary(600))
    points = [replace(p, id=i + 1, accuracy_m=500 if i % 17 == 0 else p.accuracy_m)
              for i, p in enumerate(build_track(segments, seed=seed))]
    overrides = [Override('force', point_id=p.id) for p in points[::3]]
    overrides += [Override('force', point_id=p.id) for p in points[::3]]
    overrides.append(Override('suppress', range_start=points[len(points) // 3].t,
                              range_end=points[len(points) // 2].t))
    overrides.append(Override('discard', range_start=points[-40].t,
                              range_end=points[-1].t))
    # Prefixes model successive arrivals; suffixes exercise re-detection windows.
    windows = [points[:stop] for stop in (1, len(points) // 3, len(points) // 2, len(points))]
    windows += [points[start:] for start in (1, len(points) // 3, len(points) // 2)]
    for window in windows:
        _assert_output_fits(window, Params(), overrides, '\U0001f680' * 100)
