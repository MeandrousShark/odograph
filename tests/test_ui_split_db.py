"""DB-backed contract test for split_trip's transaction ordering.

The target database is destroyed and recreated. Set TEST_DATABASE_URL only to
a throwaway Postgres/PostGIS instance.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException

from app.db import DETECTOR_ADVISORY_LOCK_KEY, make_pool
from app.detector.core import Params
from app.detector.runner import DetectorRunner, load_trip_points
from app.ui import make_router
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "SPLITDEV"


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route missing: {path}")


def _request(pool, runner):
    return SimpleNamespace(
        state=SimpleNamespace(account_pool=pool, detector_runner=runner),
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner,
            config=SimpleNamespace(detector_params=Params()),
        )),
        headers={},
    )


async def _split_holds_lock_scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        pts = build_track([Stationary(1200), Drive(km=5, speed_kmh=50), Stationary(1800)])
        async with pool.connection() as conn:
            stream = await seed_tracking_device(conn, DEVICE)
            for p in pts:
                await conn.execute(
                    "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, "
                    " accuracy_m, velocity_kmh) "
                    "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
                    "%s, %s)",
                    (account_id(conn), stream, DEVICE, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
                )
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM trips WHERE device = %s AND source = 'detected'", (DEVICE,)
            )
            trip_id = (await cur.fetchone())[0]
            trip_points = await load_trip_points(conn, trip_id)
        mid_point_id = trip_points[len(trip_points) // 2][0]

        original_reprocess_device_in = runner.reprocess_device_in
        checked = {"ran": False}

        async def _checking_reprocess_device_in(conn, device):
            # split_trip must have taken the advisory lock before this point
            # (before the point-membership check, the distance validation,
            # and the override insert above it), and must still hold it here
            # -- a concurrent pg_try_advisory_xact_lock on a separate session
            # must fail while split's transaction is still open.
            holder = await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True)
            try:
                cur = await holder.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)",
                    (DETECTOR_ADVISORY_LOCK_KEY,),
                )
                got_lock = (await cur.fetchone())[0]
            finally:
                await holder.close()
            checked["ran"] = True
            assert got_lock is False, (
                "a concurrent session acquired the detector advisory lock while "
                "split_trip's transaction was still open -- the lock must be held "
                "across validation and the override write"
            )
            return await original_reprocess_device_in(conn, device)

        runner.reprocess_device_in = _checking_reprocess_device_in
        split = _endpoint("/trips/{trip_id}/split")
        response = await split(
            _request(pool, runner), trip_id, mid_point_id, {"sub": "test"}
        )
        assert response.status_code == 204
        assert checked["ran"] is True

        # The lock is released on commit; a fresh try-lock now succeeds.
        holder = await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True)
        try:
            cur = await holder.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (DETECTOR_ADVISORY_LOCK_KEY,),
            )
            assert (await cur.fetchone())[0] is True
        finally:
            await holder.close()

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE device = %s AND source = 'detected'",
                (DEVICE,),
            )
            (count,) = await cur.fetchone()
        assert count == 2, "the split must have actually applied"
    finally:
        await raw_pool.close()


def test_split_holds_advisory_lock_across_validation_and_override_write():
    asyncio.run(_split_holds_lock_scenario())


async def _split_reads_trip_fresh_under_lock_scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        pts = build_track([Stationary(1200), Drive(km=5, speed_kmh=50), Stationary(1800)])
        async with pool.connection() as conn:
            stream = await seed_tracking_device(conn, DEVICE)
            for p in pts:
                await conn.execute(
                    "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, "
                    " accuracy_m, velocity_kmh) "
                    "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
                    "%s, %s)",
                    (account_id(conn), stream, DEVICE, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
                )
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM trips WHERE device = %s AND source = 'detected'", (DEVICE,)
            )
            trip_id = (await cur.fetchone())[0]
            trip_points = await load_trip_points(conn, trip_id)
        mid_point_id = trip_points[len(trip_points) // 2][0]

        # Hold the advisory lock on a separate, uncommitted transaction so
        # split_trip's own lock acquisition genuinely blocks -- not just
        # races past a plain read.
        holder = await psycopg.AsyncConnection.connect(TEST_DB)
        try:
            await holder.execute(
                "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
            )

            split = _endpoint("/trips/{trip_id}/split")
            task = asyncio.create_task(
                split(_request(pool, runner), trip_id, mid_point_id, {"sub": "test"})
            )
            await asyncio.sleep(0.5)
            assert not task.done(), (
                "split_trip should block waiting for the advisory lock, not "
                "proceed while a concurrent session holds it"
            )

            # Delete the trip out from under the blocked split, then commit
            # the delete and release the advisory lock together. If
            # split_trip trusted a pre-lock read, it would already have the
            # (now stale) row in hand and would proceed past this point
            # instead of 404ing.
            await holder.execute("DELETE FROM trips WHERE id = %s", (trip_id,))
            await holder.commit()
        finally:
            await holder.close()

        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, timeout=5)
        assert exc.value.status_code == 404
    finally:
        await raw_pool.close()


def test_split_trip_rereads_trip_fresh_inside_advisory_lock():
    """A pre-lock trip read would have captured the row before the delete
    below and proceeded past it; only a fresh in-lock read notices the trip
    is gone and 404s.
    """
    asyncio.run(_split_reads_trip_fresh_under_lock_scenario())
