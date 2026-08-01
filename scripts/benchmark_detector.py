#!/usr/bin/env python3
"""Repeatable pure-detector scale check; never connects to or mutates a DB."""
from __future__ import annotations

import argparse
import resource
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.detector.core import Params, Point, detect


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def representative_history(size: int) -> list[Point]:
    """Alternating parked/driving samples at a fixed 15-second cadence."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    points = []
    lat, lon = 47.60, -122.33
    for i in range(size):
        phase = i % 140
        if 30 <= phase < 110:
            lon += 0.0006
        points.append(Point(
            id=i + 1,
            t=t0 + timedelta(seconds=i * 15),
            lat=lat,
            lon=lon,
            accuracy_m=8.0,
            velocity_kmh=30.0 if 30 <= phase < 110 else 0.0,
        ))
    return points


def run(size: int) -> None:
    before = peak_rss_bytes()
    points = representative_history(size)
    materialized = peak_rss_bytes()
    started = time.perf_counter()
    stays, trips = detect(points, Params())
    elapsed = time.perf_counter() - started
    peak = peak_rss_bytes()
    print(
        f"points={size} stays={len(stays)} trips={len(trips)} elapsed_s={elapsed:.3f} "
        f"baseline_peak_rss_bytes={before} materialized_peak_rss_bytes={materialized} "
        f"final_peak_rss_bytes={peak} incremental_peak_rss_bytes={peak - before}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 250_000, 500_000])
    parser.add_argument("--single-size", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.single_size is not None:
        if args.single_size <= 0:
            parser.error("sizes must be positive")
        run(args.single_size)
        return
    for size in args.sizes:
        if size <= 0:
            parser.error("sizes must be positive")
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--single-size", str(size)],
            check=True,
        )


if __name__ == "__main__":
    main()
