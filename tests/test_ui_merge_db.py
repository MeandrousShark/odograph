from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import make_pool, run_migrations
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.ui import make_router
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")


class FakeSnapWorker:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/merge_selected":
            return route.endpoint
    raise AssertionError("merge_selected route missing")


async def _insert_points(conn, points, device):
    for point in points:
        await conn.execute(
            "INSERT INTO points (device, recorded_at, received_at, geom, accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (device, point.t, point.t, point.lon, point.lat, point.accuracy_m, point.velocity_kmh),
        )


async def _trips(conn, device):
    cur = await conn.execute(
        "SELECT id, started_at, ended_at FROM trips WHERE device=%s AND source='detected' ORDER BY started_at",
        (device,),
    )
    return await cur.fetchall()


async def _override_rows(conn, device):
    cur = await conn.execute(
        "SELECT id, kind::text, point_id, range_start, range_end "
        "FROM trip_boundary_overrides WHERE device=%s ORDER BY id",
        (device,),
    )
    return await cur.fetchall()


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2),
            Stationary(1200), Drive(km=2), Stationary(900),
        ])
        other = build_track([Stationary(900), Drive(km=2), Stationary(900)])
        async with pool.connection() as conn:
            await _insert_points(conn, track, "A")
            await _insert_points(conn, other, "B")
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            a = await _trips(conn, "A")
            b = await _trips(conn, "B")
            manual = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m) "
                "VALUES ('A', 'manual', %s, %s, 1000) RETURNING id",
                (a[0][2], a[1][1]),
            )
            manual_id = (await manual.fetchone())[0]

        handler = _endpoint()
        snap_worker = FakeSnapWorker()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner, snap_worker=snap_worker,
        )))
        cases = [
            ([a[0][0], a[2][0]], "contiguous"),
            ([a[0][0], b[0][0]], "same device"),
            ([a[0][0], manual_id], "detected"),
        ]
        for trip_ids, message in cases:
            with pytest.raises(HTTPException, match=message):
                await handler(request, trip_ids, "unclassified", "", "", "keep", {"sub": "test"})

        response = await handler(
            request, [a[0][0], a[1][0]], "business", "  Client visit  ",
            "handler", "keep", {"sub": "test"},
        )
        merged_id = json.loads(response.body)["trip_id"]
        async with pool.connection() as conn:
            after = await _trips(conn, "A")
            row = await conn.execute(
                "SELECT category::text, purpose, notes, tag_source::text FROM trips WHERE id=%s",
                (merged_id,),
            )
            values = await row.fetchone()
        assert len(after) == 2
        assert values == ("business", "Client visit", "handler", "human")
        assert snap_worker.pokes == 1
    finally:
        await pool.close()


def test_merge_handler_validation_and_valid_merge():
    asyncio.run(_scenario())


async def _reprocess_failure_rolls_back_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, track, "A")
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            a = await _trips(conn, "A")
        assert len(a) == 2

        # An opposing force-split override sitting inside the range the
        # merge's override-write phase would suppress -- exactly what a
        # failed merge must NOT actually delete.
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM points WHERE device='A' "
                "AND recorded_at BETWEEN %s AND %s ORDER BY recorded_at LIMIT 1",
                (a[0][2], a[1][1]),
            )
            force_point_id = (await cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, point_id) "
                "VALUES ('A', 'force', %s)",
                (force_point_id,),
            )
            before_overrides = await _override_rows(conn, "A")
            before_trips = await _trips(conn, "A")

        # Force the reprocess step -- which in the fixed handler runs on the
        # SAME connection/transaction as the override writes above it -- to
        # fail. If it isn't actually atomic, the override writes already
        # executed real SQL and would still land, independent of this.
        async def _broken_reprocess_device_in(conn, device):
            raise RuntimeError("forced reprocess failure mid-merge")

        runner.reprocess_device_in = _broken_reprocess_device_in

        handler = _endpoint()
        snap_worker = FakeSnapWorker()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner, snap_worker=snap_worker,
        )))
        with pytest.raises(RuntimeError, match="forced reprocess failure"):
            await handler(
                request, [a[0][0], a[1][0]], "business", "Client visit",
                "handler", "keep", {"sub": "test"},
            )

        async with pool.connection() as conn:
            after_overrides = await _override_rows(conn, "A")
            after_trips = await _trips(conn, "A")
        assert after_overrides == before_overrides, (
            "a failed reprocess must roll back the merge's override writes too "
            "(both the suppress insert and the force delete)"
        )
        assert after_trips == before_trips, "trips must be untouched by a rolled-back merge"
        assert snap_worker.pokes == 0
    finally:
        await pool.close()


def test_merge_rolls_back_atomically_when_reprocess_fails():
    """Override writes, reprocess, and the tag UPDATE run
    in one transaction, so a failure in the reprocess step leaves no partial
    state -- not a committed suppress-override, not a deleted force-override,
    not a changed trip."""
    asyncio.run(_reprocess_failure_rolls_back_scenario())


async def _set_vehicle(conn, trip_id, vehicle_id):
    await conn.execute("UPDATE trips SET vehicle_id = %s WHERE id = %s", (vehicle_id, trip_id))


async def _vehicle_tristate_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)

        def _two_trip_track():
            return build_track([
                Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
            ])

        devices = ["K", "E", "N", "X"]
        async with pool.connection() as conn:
            for device in devices:
                await _insert_points(conn, _two_trip_track(), device)
            second_vehicle = await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Second Car') RETURNING id"
            )
            second_vehicle_id = (await second_vehicle.fetchone())[0]

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        handler = _endpoint()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner
        )))

        async with pool.connection() as conn:
            trips_by_device = {d: await _trips(conn, d) for d in devices}
            for d in devices:
                assert len(trips_by_device[d]) == 2

        # "keep" must reproduce today's inheritance behavior exactly: don't
        # touch vehicle_id at all. Direct handler calls can't literally omit
        # the form field (Form(...)'s sentinel is only resolved by FastAPI's
        # own request parsing, not a plain function call), so "keep" is
        # passed explicitly here; `test_merge_endpoint_vehicle_default.py`
        # separately asserts that all three merge endpoints declare "keep"
        # as the actual wire-level default a genuinely missing field
        # resolves to.
        k_ids = [r[0] for r in trips_by_device["K"]]
        async with pool.connection() as conn:
            for trip_id in k_ids:
                await _set_vehicle(conn, trip_id, 1)
        response = await handler(
            request, k_ids, "unclassified", "", "", "keep", {"sub": "test"},
        )
        k_merged = json.loads(response.body)["trip_id"]

        # An explicit vehicle id joins the same UPDATE the tag/purpose/notes
        # already use, still inside the merge's one transaction.
        e_ids = [r[0] for r in trips_by_device["E"]]
        response = await handler(
            request, e_ids, "unclassified", "", "", str(second_vehicle_id), {"sub": "test"},
        )
        e_merged = json.loads(response.body)["trip_id"]

        # The empty string is a real, distinct choice ("no vehicle"), not a
        # synonym for "keep".
        n_ids = [r[0] for r in trips_by_device["N"]]
        async with pool.connection() as conn:
            for trip_id in n_ids:
                await _set_vehicle(conn, trip_id, 1)
        response = await handler(
            request, n_ids, "unclassified", "", "", "", {"sub": "test"},
        )
        n_merged = json.loads(response.body)["trip_id"]

        async with pool.connection() as conn:
            rows = await conn.execute(
                "SELECT id, vehicle_id FROM trips WHERE id = ANY(%s)",
                ([k_merged, e_merged, n_merged],),
            )
            by_id = {r[0]: r[1] for r in await rows.fetchall()}
        assert by_id[k_merged] == 1, "keep (field omitted) must preserve the inherited vehicle_id"
        assert by_id[e_merged] == second_vehicle_id
        assert by_id[n_merged] is None

        # A nonexistent vehicle id must 400 and leave the merge entirely
        # unapplied -- same atomicity guarantee as the reprocess-failure
        # case, just triggered by the final UPDATE
        # instead of the reprocess step.
        x_ids = [r[0] for r in trips_by_device["X"]]
        async with pool.connection() as conn:
            before_overrides = await _override_rows(conn, "X")
            before_trips = await _trips(conn, "X")
        with pytest.raises(HTTPException, match="No such vehicle"):
            await handler(
                request, x_ids, "unclassified", "", "", "999999", {"sub": "test"},
            )
        async with pool.connection() as conn:
            after_overrides = await _override_rows(conn, "X")
            after_trips = await _trips(conn, "X")
        assert after_overrides == before_overrides
        assert after_trips == before_trips
    finally:
        await pool.close()


def test_merge_vehicle_tristate():
    """A digit vehicle_id sets it, "" nulls it, and "keep" (or the field
    being absent, matching a stale client) leaves whatever reconcile's
    longest-trip inheritance already put there. A nonexistent vehicle id
    400s without applying any part of the merge."""
    asyncio.run(_vehicle_tristate_scenario())
