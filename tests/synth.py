"""Synthetic GPS track builder for detector tests.

Deterministic (seeded RNG). Times and distances are exact at segment
boundaries; Gaussian jitter is applied per emitted point only, so the true
underlying trajectory never drifts.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.detector.core import Point

M_PER_DEG_LAT = 111_320.0

T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
START = (45.5000, -122.6000)


@dataclass
class Stationary:
    duration_s: float
    jitter_m: float = 8.0
    # silent=True models OwnTracks significant-changes mode: the phone sends
    # nothing while parked, so the stay is only the arrival point (previous
    # segment's last emission) plus one fix on departure.
    silent: bool = False


@dataclass
class Drive:
    km: float
    speed_kmh: float = 50.0
    bearing_deg: float = 90.0
    jitter_m: float = 3.0


@dataclass
class Gap:
    """No emissions; time passes and (optionally) position changes."""
    duration_s: float
    move_km: float = 0.0
    bearing_deg: float = 90.0


def _offset(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    return (
        lat + north_m / M_PER_DEG_LAT,
        lon + east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat))),
    )


def build_track(
    segments,
    start: tuple[float, float] = START,
    t0: datetime = T0,
    interval_s: float = 15.0,
    seed: int = 42,
) -> list[Point]:
    rng = random.Random(seed)
    lat, lon = start
    points: list[Point] = []

    def emit(t: datetime, plat: float, plon: float, vel_kmh: float, jitter_m: float):
        jlat, jlon = _offset(plat, plon, rng.gauss(0, jitter_m), rng.gauss(0, jitter_m))
        points.append(Point(t=t, lat=jlat, lon=jlon, accuracy_m=10.0, velocity_kmh=vel_kmh))

    t_last = t0
    emit(t_last, lat, lon, 0.0, 8.0)  # anchor fix at track start

    for seg in segments:
        if isinstance(seg, Stationary):
            end = t_last + timedelta(seconds=seg.duration_s)
            if seg.silent:
                emit(end, lat, lon, 0.0, seg.jitter_m)
            else:
                t = t_last + timedelta(seconds=interval_s)
                while t <= end:
                    emit(t, lat, lon, 0.0, seg.jitter_m)
                    t += timedelta(seconds=interval_s)
                if points[-1].t < end:
                    emit(end, lat, lon, 0.0, seg.jitter_m)
            t_last = end
        elif isinstance(seg, Drive):
            speed_ms = seg.speed_kmh / 3.6
            total_m = seg.km * 1000.0
            duration = total_m / speed_ms
            elapsed = interval_s
            while elapsed <= duration + 1e-9:
                d = speed_ms * elapsed
                plat, plon = _travel(lat, lon, d, seg.bearing_deg)
                emit(t_last + timedelta(seconds=elapsed), plat, plon, seg.speed_kmh, seg.jitter_m)
                elapsed += interval_s
            if elapsed - interval_s < duration - 1e-9:  # exact arrival fix
                plat, plon = _travel(lat, lon, total_m, seg.bearing_deg)
                emit(t_last + timedelta(seconds=duration), plat, plon, seg.speed_kmh, seg.jitter_m)
            lat, lon = _travel(lat, lon, total_m, seg.bearing_deg)
            t_last += timedelta(seconds=duration)
        elif isinstance(seg, Gap):
            lat, lon = _travel(lat, lon, seg.move_km * 1000.0, seg.bearing_deg)
            t_last += timedelta(seconds=seg.duration_s)
        else:
            raise TypeError(f"unknown segment {seg!r}")

    return points


def _travel(lat: float, lon: float, dist_m: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    return _offset(lat, lon, math.sin(b) * dist_m, math.cos(b) * dist_m)
