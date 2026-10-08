"""Per-account detector exclusion across real account transactions.

The target database is destroyed and recreated. Set TEST_DATABASE_URL only to
a throwaway Postgres/PostGIS instance.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest

from app.db import DETECTOR_ADVISORY_LOCK_KEY
from app.detector.core import Params
from app.detector.lock import lock_detector
from app.detector.runner import DetectorRunner, load_trip_points
from app.portable import importer
from app.ui import make_router as ui_router
from app.ui.merge_split import _merge_trips_core
from app.worker import BatchOutcome
from tests.test_ownership_integration_db import _bundle, _fixture, _seed_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests"),
]


def _endpoint(path):
    return next(route.endpoint for route in ui_router().routes if route.path == path)


def _request(bound, runner):
    return SimpleNamespace(
        state=SimpleNamespace(account_pool=bound, detector_runner=runner),
        app=SimpleNamespace(state=SimpleNamespace(
            detector_runner=runner, config=SimpleNamespace(detector_params=Params()),
        )),
        headers={},
    )


async def _detected_trips(bound):
    async with bound.connection() as conn:
        rows = await (await conn.execute(
            "SELECT id FROM trips WHERE source='detected' ORDER BY started_at")).fetchall()
    return [row[0] for row in rows]


async def _mid_point(bound, trip_id):
    async with bound.connection() as conn:
        points = await load_trip_points(conn, trip_id)
    return points[len(points) // 2][0]


def test_other_account_detects_and_edits_while_account_holds_detector_lock():
    async def run():
        async with _fixture() as (_owner, _pools, _state, a, b):
            await _seed_track(b, two_trips=True)
            detector = DetectorRunner(b, Params())
            request = _request(b, detector)
            async with a.connection() as held:
                await lock_detector(held)
                outcome = await asyncio.wait_for(detector.run_turn(), 5)
                assert not outcome.skipped and outcome.batch == BatchOutcome(1, 1)
                trips = await _detected_trips(b)
                assert len(trips) == 2
                merged = await asyncio.wait_for(_merge_trips_core(request, trips), 5)
                split = _endpoint("/trips/{trip_id}/split")
                await asyncio.wait_for(
                    split(request, merged, await _mid_point(b, merged), {"sub": "test"}), 5)
                create_place = _endpoint("/places")
                await asyncio.wait_for(create_place(
                    request, name="Office", kind="work", lat=47.6, lon=-122.3,
                    radius_m=150.0, user={}), 5)
                batch_delete = _endpoint("/trips/batch_delete")
                trips = await _detected_trips(b)
                assert trips
                await asyncio.wait_for(batch_delete(request, trips, user={}), 5)
            assert await _detected_trips(b) == []
    asyncio.run(run())


def test_other_account_imports_while_account_holds_detector_lock():
    async def run():
        async with _fixture() as (_owner, _pools, _state, a, b):
            async with a.connection() as held:
                await lock_detector(held)
                async with b.connection() as conn:
                    await asyncio.wait_for(importer._apply_import(conn, _bundle()), 5)
            async with b.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 1
    asyncio.run(run())


def test_same_account_detector_and_edits_still_exclude_each_other():
    async def run():
        async with _fixture() as (_owner, _pools, _state, a, _b):
            await _seed_track(a)
            detector = DetectorRunner(a, Params())
            entered = asyncio.Event()

            async def contend():
                async with a.connection() as conn:
                    await lock_detector(conn)
                    entered.set()

            async with a.connection() as held:
                await lock_detector(held)
                # Reentrant within the holding transaction.
                await lock_detector(held)
                assert (await detector.run_turn()).skipped
                waiter = asyncio.create_task(contend())
                await asyncio.sleep(0.5)
                assert not entered.is_set()
            await asyncio.wait_for(waiter, 5)
            outcome = await detector.run_turn()
            assert outcome.batch == BatchOutcome(1, 1)
    asyncio.run(run())


def test_exclusive_global_key_still_excludes_every_account():
    """Older processes and scripts/sql/cleanup_test_device.sql hold it exclusively."""
    async def run():
        async with _fixture() as (_owner, _pools, _state, a, b):
            await _seed_track(a)
            await _seed_track(b)
            holder = await psycopg.AsyncConnection.connect(TEST_DB)
            try:
                await holder.execute("SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,))
                assert (await DetectorRunner(a, Params()).run_turn()).skipped
                assert (await DetectorRunner(b, Params()).run_turn()).skipped
            finally:
                await holder.close()
            assert (await DetectorRunner(b, Params()).run_turn()).batch == BatchOutcome(1, 1)
    asyncio.run(run())
