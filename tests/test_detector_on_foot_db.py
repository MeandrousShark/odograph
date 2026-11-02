"""A transit leg through the real runner: batches must equal a full reprocess.

A near stay can be one point whose only evidence is the leg after it, so this
pins what the in-memory rule tests cannot: that _rewind_start, reconciliation
and the point links all still agree with a replay from the first point.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.account_context import account_id
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner, load_trip_points
from conftest import reset_account_db, seed_tracking_device
from tests.synth import Drive, Gap, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

DEVICE = "ONFOOT"
TRACK = [
    Stationary(600), Drive(km=2), Gap(900, 1), Drive(km=2),
    Stationary(1200), Drive(km=2), Stationary(900),
]


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
        # A one-point stay's fix is shared by the trips on either side and its
        # single trip_id depends on write order, so load_trip_points reads by
        # time. Compare what it returns, and the links on every unshared point.
        "trip_points": [await load_trip_points(conn, trip_id) for trip_id, in trips],
        "points": await _rows(
            conn,
            "SELECT p.recorded_at, p.trip_id FROM points p WHERE NOT EXISTS ("
            "SELECT 1 FROM trips a JOIN trips b ON a.ended_at = b.started_at "
            "WHERE a.ended_at = p.recorded_at) ORDER BY p.recorded_at",
        ),
        "expenses": await _rows(conn, "SELECT id, trip_id FROM expenses ORDER BY id"),
    }


def _batches(points, size):
    """Fixed batches, with a cut on each side of the transit leg."""
    gap_end = max(range(1, len(points)), key=lambda i: points[i].t - points[i - 1].t)
    cuts = sorted({*range(0, len(points), size), gap_end, gap_end + 1, len(points)})
    return [points[a:b] for a, b in zip(cuts, cuts[1:])]


async def _tag_transit_trip(conn):
    """The human edits a rewound window must keep: returns once tagged."""
    rows = await _rows(conn, "SELECT id FROM trips WHERE has_gap")
    if not rows:
        return False
    await conn.execute(
        "UPDATE trips SET category='business', tag_source='human' WHERE id=%s", rows[0])
    await conn.execute(
        "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, trip_id) "
        "VALUES (%s, 1, '2026-08-01', 'fuel', 22.18, 'fully_business', %s)",
        (account_id(conn), rows[0][0]),
    )
    return True


async def _run_scenario(size):
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, DEVICE)
        points = build_track(TRACK)
        runner = DetectorRunner(pool, Params())
        tagged = False
        rewound = 0
        for batch in _batches(points, size):
            async with pool.connection() as conn:
                await _insert(conn, device, batch)
            assert await runner.run_once() is True
            rewound += runner.last_stats["points"] < len(points)
            async with pool.connection() as conn:
                # Past the run's overlap margin, so the next batch's window
                # rewinds as it would on a live device.
                await conn.execute("UPDATE detector_state SET last_run_at=now()")
                tagged = tagged or await _tag_transit_trip(conn)
        assert tagged
        assert rewound > 1

        async with pool.connection() as conn:
            batched = await _output(conn)
        await runner.reprocess_device_now(device)
        async with pool.connection() as conn:
            replayed = await _output(conn)

        assert len(batched["trips"]) == 3
        assert [t[5:] for t in batched["trips"]] == [
            (False, "unclassified", None), (True, "business", "human"),
            (False, "unclassified", None)]
        assert batched["expenses"] == [(1, batched["trips"][1][0])]
        assert batched == replayed
    finally:
        await raw_pool.close()


@pytest.mark.parametrize("size", [9, 40])
def test_batches_through_a_transit_leg_match_a_full_reprocess(size):
    asyncio.run(_run_scenario(size))
