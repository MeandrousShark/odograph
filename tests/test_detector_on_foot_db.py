"""On-foot detection through the real runner: batches and late fixes must end
up where a full reprocess does.

The rule tests call detect() on one window. This pins what they cannot: that
_rewind_start, reconciliation and the point links agree with a replay from the
first point, including human tags and expenses.
"""
from __future__ import annotations

import asyncio
import math
import os
from datetime import timedelta

import pytest

from app.account_context import account_id
from app.db import make_pool
from app.detector.core import Params, Point
from app.detector.runner import DetectorRunner, load_trip_points
from conftest import reset_account_db, seed_tracking_device
from tests.synth import Drive, Gap, Stationary, _travel, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

DEVICE = "ONFOOT"
BRISK_TRACK = [
    Stationary(600), Drive(km=2), Stationary(600), Drive(km=1, speed_kmh=6.5, jitter_m=8),
    Stationary(600), Drive(km=2), Stationary(900),
]
GAP_TRACK = [Stationary(600), Drive(km=2), Gap(900, 1), Drive(km=2), Stationary(600)]


async def _insert(conn, device, points):
    for p in points:
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom, accuracy_m) "
            "VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
            (account_id(conn), device, DEVICE, p.t, p.lon, p.lat, p.accuracy_m),
        )


async def _rows(conn, sql):
    return [tuple(row) for row in await (await conn.execute(sql)).fetchall()]


async def _output(conn):
    trips = await _rows(conn, "SELECT id FROM trips ORDER BY started_at")
    return {
        "stays": await _rows(conn, "SELECT started_at, ended_at, point_count FROM stays ORDER BY started_at"),
        "trips": await _rows(
            conn,
            "SELECT id, started_at, ended_at, distance_m, point_count, has_gap, "
            "category::text, tag_source::text FROM trips ORDER BY started_at",
        ),
        # A fix shared by the trips on either side holds one trip_id, chosen by
        # write order, so load_trip_points reads by time. Compare what it
        # returns, and the links on every unshared point.
        "trip_points": [await load_trip_points(conn, trip_id) for trip_id, in trips],
        "points": await _rows(
            conn,
            "SELECT p.recorded_at, p.trip_id FROM points p WHERE NOT EXISTS ("
            "SELECT 1 FROM trips a JOIN trips b ON a.ended_at = b.started_at "
            "WHERE a.ended_at = p.recorded_at) ORDER BY p.recorded_at",
        ),
        "expenses": await _rows(conn, "SELECT id, trip_id FROM expenses ORDER BY id"),
    }


async def _tag(conn, trip_id):
    await conn.execute(
        "UPDATE trips SET category='business', tag_source='human' WHERE id=%s", (trip_id,))
    await conn.execute(
        "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, trip_id) "
        "VALUES (%s, 1, '2026-08-01', 'fuel', 22.18, 'fully_business', %s)",
        (account_id(conn), trip_id),
    )


async def _deliver(pool, runner, device, batch):
    async with pool.connection() as conn:
        await _insert(conn, device, batch)
    assert await runner.run_once() is True
    async with pool.connection() as conn:
        # Past the run's overlap margin, so the next batch's window rewinds as
        # it would on a live device.
        await conn.execute("UPDATE detector_state SET last_run_at=now()")


async def _batched_and_replayed(pool, runner, device):
    async with pool.connection() as conn:
        batched = await _output(conn)
    await runner.reprocess_device_now(device)
    async with pool.connection() as conn:
        replayed = await _output(conn)
    return batched, replayed


async def _run_batches(size):
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, DEVICE)
        points = build_track(BRISK_TRACK)
        runner = DetectorRunner(pool, Params())
        tagged = False
        rewound = 0
        for start in range(0, len(points), size):
            await _deliver(pool, runner, device, points[start:start + size])
            rewound += runner.last_stats["points"] < len(points)
            if not tagged:
                async with pool.connection() as conn:
                    trips = await _rows(conn, "SELECT id FROM trips ORDER BY started_at")
                    if trips:
                        await _tag(conn, trips[0][0])
                        tagged = True
        assert tagged
        assert rewound > 1

        batched, replayed = await _batched_and_replayed(pool, runner, device)

        assert batched == replayed
        # The middle trip is 0.93 km when a fast leg ends the brisk walk's span.
        assert [round(t[3] / 1000, 2) for t in batched["trips"]] == [1.88, 0.51, 1.86]
        assert [t[6:] for t in batched["trips"]].count(("business", "human")) == 1
        assert batched["expenses"][0][1] == batched["trips"][0][0]
    finally:
        await raw_pool.close()


@pytest.mark.parametrize("size", [9, 40])
def test_batches_match_a_full_reprocess(size):
    asyncio.run(_run_batches(size))


def _detour(a: Point, b: Point) -> list[Point]:
    """A drive through the gap, 4 km off the straight line at its middle."""
    seconds = (b.t - a.t).total_seconds()
    fixes = int(seconds // 15) - 1
    points = []
    for k in range(1, fixes + 1):
        part = k / (fixes + 1)
        lat, lon = _travel(a.lat + (b.lat - a.lat) * part, a.lon + (b.lon - a.lon) * part,
                           4000 * math.sin(math.pi * part), 0.0)
        points.append(Point(t=a.t + timedelta(seconds=15 * k), lat=lat, lon=lon, accuracy_m=10.0))
    return points


def _quick_burst(a: Point, b: Point) -> list[Point]:
    """Four fixes that make the gap's middle read fast."""
    return [
        Point(t=a.t + timedelta(seconds=seconds), lat=a.lat + (b.lat - a.lat) * part,
              lon=a.lon + (b.lon - a.lon) * part, accuracy_m=10.0)
        for seconds, part in [(200, .1), (215, .4), (230, .7), (245, .9)]
    ]


async def _run_late_fixes(late_fixes, trip_km):
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, DEVICE)
        points = build_track(GAP_TRACK)
        gap_end = max(range(1, len(points)), key=lambda i: points[i].t - points[i - 1].t)
        runner = DetectorRunner(pool, Params())
        for start in range(0, len(points), 9):
            await _deliver(pool, runner, device, points[start:start + 9])
        async with pool.connection() as conn:
            tagged = (await _rows(conn, "SELECT id FROM trips ORDER BY started_at"))[0][0]
            await _tag(conn, tagged)

        await _deliver(pool, runner, device, late_fixes(points[gap_end - 1], points[gap_end]))
        batched, replayed = await _batched_and_replayed(pool, runner, device)

        assert batched == replayed
        assert [round(t[3] / 1000, 1) for t in batched["trips"]] == trip_km
        assert [t[0] for t in batched["trips"] if t[6:] == ("business", "human")] == [tagged]
        assert [e[1] for e in batched["expenses"]] == [tagged]
    finally:
        await raw_pool.close()


@pytest.mark.parametrize(
    ("late_fixes", "trip_km"),
    [(_detour, [11.8]), (_quick_burst, [3.1, 1.7])],
    ids=["detour", "quick-burst"],
)
def test_fixes_delivered_late_inside_a_gap_match_a_full_reprocess(late_fixes, trip_km):
    asyncio.run(_run_late_fixes(late_fixes, trip_km))
