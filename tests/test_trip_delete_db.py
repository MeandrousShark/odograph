"""DB-backed contract tests for deleting manual and detected trips.

The target database is destroyed and recreated. Set TEST_DATABASE_URL only to
a throwaway Postgres/PostGIS instance.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from psycopg import errors

from app.db import make_pool
from app.account_context import account_id
from personal_support import fixture_device, personal_request
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.ui import make_router
from conftest import reset_account_db
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route missing: {path}")


async def _insert_detectable_track(conn, device: str = "DELETEDEV") -> None:
    points = build_track([
        Stationary(1200), Drive(km=3), Stationary(1200),
    ])
    for point in points:
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, "
            "geom, accuracy_m, velocity_kmh) VALUES (%s, %s, %s, %s, %s, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (
                account_id(conn),
                await fixture_device(conn, device),
                device,
                point.t,
                point.t,
                point.lon,
                point.lat,
                point.accuracy_m,
                point.velocity_kmh,
            ),
        )


async def _detected_trip(conn, device: str = "DELETEDEV"):
    cur = await conn.execute(
        "SELECT id, started_at, ended_at FROM trips "
        "WHERE device = %s AND source = 'detected' ORDER BY started_at",
        (device,),
    )
    return await cur.fetchone()


async def _discard_rows(conn, device: str = "DELETEDEV"):
    cur = await conn.execute(
        "SELECT id, range_start, range_end FROM trip_boundary_overrides "
        "WHERE device = %s AND kind::text = 'discard' ORDER BY id",
        (device,),
    )
    return await cur.fetchall()


class FakeSnapWorker:
    def __init__(self, transaction_tracker=None):
        self.pokes = 0
        self.transaction_tracker = transaction_tracker

    def poke(self):
        if self.transaction_tracker is not None:
            assert self.transaction_tracker.in_context is False
            assert self.transaction_tracker.last_committed is True
        self.pokes += 1


class TrackingPool:
    """Expose whether the route's connection context committed before poke."""

    def __init__(self, pool):
        self.pool = pool
        self.principal = pool.principal
        self.in_context = False
        self.last_committed = False

    def connection(self):
        return TrackingConnection(self, self.pool.connection())


class TrackingConnection:
    def __init__(self, tracker, context):
        self.tracker = tracker
        self.context = context

    async def __aenter__(self):
        self.tracker.in_context = True
        self.tracker.last_committed = False
        return await self.context.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            result = await self.context.__aexit__(exc_type, exc, traceback)
            self.tracker.last_committed = exc_type is None
            return result
        finally:
            self.tracker.in_context = False


def _request(pool, runner, snap_worker=None):
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, detector_runner=runner, snap_worker=snap_worker,
            config=SimpleNamespace(detector_params=Params()),
        )),
        headers={},
    ))


async def _delete_and_restore_scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        runner = DetectorRunner(pool, Params())
        tracking_pool = TrackingPool(pool)
        snap_worker = FakeSnapWorker(tracking_pool)
        request = _request(tracking_pool, runner, snap_worker)
        delete = _endpoint("/trips/{trip_id}/delete")
        restore = _endpoint("/settings/boundary_overrides/{override_id}/delete")

        async with pool.connection() as conn:
            manual = await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m) "
                "VALUES (%s, 'manual', 'manual', now() - interval '1 hour', now(), 1000) RETURNING "
                "id", (account_id(conn),)
            )
            manual_id = (await manual.fetchone())[0]
            await conn.execute(
                "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, "
                "treatment, notes, trip_id) VALUES (%s, 1, '2026-05-04', 'fuel', 17.23, "
                "'business_use_allocated', 'keep after trip deletion', %s)",
                (account_id(conn), manual_id,),
            )

        response = await delete(request, manual_id, {"sub": "test"})
        assert response.status_code == 204
        assert snap_worker.pokes == 0
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM trips WHERE id = %s", (manual_id,))
            assert await cur.fetchone() is None
            assert await _discard_rows(conn) == []
            cur = await conn.execute(
                "SELECT vehicle_id, incurred_on, category::text, amount, treatment::text, "
                "notes, trip_id FROM expenses"
            )
            assert await cur.fetchone() == (
                1, date(2026, 5, 4), "fuel", Decimal("17.23"),
                "business_use_allocated", "keep after trip deletion", None,
            )

        async with pool.connection() as conn:
            fragment_manual = await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m) "
                "VALUES (%s, 'manual', 'manual', now() - interval '1 hour', now(), 1000) RETURNING "
                "id", (account_id(conn),)
            )
            fragment_manual_id = (await fragment_manual.fetchone())[0]
        response = await delete(request, fragment_manual_id, {"sub": "test"}, True)
        assert response.status_code == 200
        assert response.headers["X-Archive-Write"] == "success"
        assert "HX-Redirect" not in response.headers
        assert response.body == b""

        async with pool.connection() as conn:
            dashboard_manual = await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m) "
                "VALUES (%s, 'manual', 'manual', now() - interval '1 hour', now(), 1000) RETURNING "
                "id", (account_id(conn),)
            )
            dashboard_manual_id = (await dashboard_manual.fetchone())[0]
        response = await delete(
            request, dashboard_manual_id, {"sub": "test"}, True, "2026-07-13"
        )
        assert response.status_code == 200
        assert response.headers["HX-Refresh"] == "true"
        assert response.body == b""

        async with pool.connection() as conn:
            await _insert_detectable_track(conn)
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            trip = await _detected_trip(conn)
        assert trip is not None
        trip_id, started_at, ended_at = trip

        original_reprocess = runner.reprocess_device_in

        async def unexpected_reprocess(conn, device):
            raise AssertionError("detected deletion must not reprocess the device")

        runner.reprocess_device_in = unexpected_reprocess
        response = await delete(request, trip_id, {"sub": "test"})
        runner.reprocess_device_in = original_reprocess
        assert response.status_code == 204
        assert snap_worker.pokes == 0
        async with pool.connection() as conn:
            assert await _detected_trip(conn) is None
            discard = await _discard_rows(conn)
        assert [(r[1], r[2]) for r in discard] == [(started_at, ended_at)]

        async with pool.connection() as conn:
            device_id = await fixture_device(conn, "DELETEDEV")
        await runner.reprocess_device_now(device_id)
        async with pool.connection() as conn:
            assert await _detected_trip(conn) is None

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE detector_state SET detector_version = 0 "
                "WHERE account_id = %s AND tracking_device_id = %s",
                (account_id(conn), device_id),
            )
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            assert await _detected_trip(conn) is None

        response = await restore(request, discard[0][0], {"sub": "test"})
        assert response.status_code == 204
        assert snap_worker.pokes == 1
        async with pool.connection() as conn:
            restored = await _detected_trip(conn)
            assert restored is not None
            assert (restored[1], restored[2]) == (started_at, ended_at)
            assert await _discard_rows(conn) == []
    finally:
        await raw_pool.close()


def test_manual_delete_and_detected_delete_persistence_and_restore():
    asyncio.run(_delete_and_restore_scenario())


async def _delete_rollback_scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await _insert_detectable_track(conn)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            before = await _detected_trip(conn)
        assert before is not None

        async with pool.admin_pool.connection() as conn:
            await conn.execute(
                "CREATE FUNCTION reject_trip_delete() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'forced delete failure'; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER reject_trip_delete BEFORE DELETE ON trips "
                "FOR EACH ROW EXECUTE FUNCTION reject_trip_delete()"
            )
        try:
            delete = _endpoint("/trips/{trip_id}/delete")
            snap_worker = FakeSnapWorker()
            with pytest.raises(errors.RaiseException, match="forced delete failure"):
                await delete(
                    _request(pool, runner, snap_worker), before[0], {"sub": "test"}
                )
            assert snap_worker.pokes == 0

            async with pool.connection() as conn:
                assert await _detected_trip(conn) == before
                assert await _discard_rows(conn) == []
        finally:
            # The reset between tests truncates data but leaves schema
            # objects alone (see tests/conftest.py), so a trigger/function
            # created here to force this one failure must be dropped here
            # too, not left for a later test's reset to clean up.
            async with pool.admin_pool.connection() as conn:
                await conn.execute("DROP TRIGGER reject_trip_delete ON trips")
                await conn.execute("DROP FUNCTION reject_trip_delete()")
    finally:
        await raw_pool.close()


def test_detected_delete_rolls_back_override_when_target_delete_fails():
    asyncio.run(_delete_rollback_scenario())


async def _surviving_snap_results_scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        track = build_track([
            Stationary(900), Drive(km=2), Stationary(1200),
            Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            for point in track:
                await conn.execute(
                    "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, "
                    "received_at, geom, accuracy_m, velocity_kmh) VALUES (%s, %s, 'DELETEDEV', %s, "
                    "%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
                    (
                        account_id(conn),
                        await fixture_device(conn, 'DELETEDEV'),
                        point.t,
                        point.t,
                        point.lon,
                        point.lat,
                        point.accuracy_m,
                        point.velocity_kmh,
                    ),
                )

        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM trips WHERE device = 'DELETEDEV' "
                "AND source = 'detected' ORDER BY started_at"
            )
            trip_ids = [row[0] for row in await cur.fetchall()]
            assert len(trip_ids) == 3
            await conn.execute(
                "UPDATE trips SET snap_status = 'low_confidence', "
                "path_snapped = ST_GeomFromText("
                " 'MULTILINESTRING((-122.33 47.60, -122.30 47.62))', 4326), "
                "distance_snapped_m = id::real * 10, "
                "snapped_at = '2026-07-01T12:00:00Z' WHERE id = ANY(%s)",
                (trip_ids,),
            )
            cur = await conn.execute(
                "SELECT id, snap_status::text, ST_AsEWKB(path_snapped), "
                "distance_snapped_m, snapped_at FROM trips "
                "WHERE id = ANY(%s) ORDER BY id",
                (trip_ids,),
            )
            before = {row[0]: tuple(row[1:]) for row in await cur.fetchall()}
            cur = await conn.execute(
                "SELECT id, device, started_at, ended_at, ST_AsEWKB(centroid::geometry), "
                "point_count FROM stays ORDER BY id"
            )
            stays_before = await cur.fetchall()

        snap_worker = FakeSnapWorker()
        async def unexpected_reprocess(conn, device):
            raise AssertionError("detected deletion must not reprocess the device")

        runner.reprocess_device_in = unexpected_reprocess
        delete = _endpoint("/trips/{trip_id}/delete")
        await delete(
            _request(pool, runner, snap_worker), trip_ids[1], {"sub": "test"}
        )
        assert snap_worker.pokes == 0

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, snap_status::text, ST_AsEWKB(path_snapped), "
                "distance_snapped_m, snapped_at FROM trips "
                "WHERE device = 'DELETEDEV' AND source = 'detected' ORDER BY id"
            )
            after = {row[0]: tuple(row[1:]) for row in await cur.fetchall()}
            cur = await conn.execute(
                "SELECT id, device, started_at, ended_at, ST_AsEWKB(centroid::geometry), "
                "point_count FROM stays ORDER BY id"
            )
            stays_after = await cur.fetchall()
        assert after == {
            trip_ids[0]: before[trip_ids[0]],
            trip_ids[2]: before[trip_ids[2]],
        }
        assert stays_after == stays_before
    finally:
        await raw_pool.close()


def test_detected_delete_preserves_terminal_snaps_on_unchanged_surviving_trips():
    asyncio.run(_surviving_snap_results_scenario())


async def _split_pokes_snap_after_commit_scenario() -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await _insert_detectable_track(conn)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trip = await _detected_trip(conn)
            assert trip is not None
            cur = await conn.execute(
                "SELECT id FROM points WHERE device = 'DELETEDEV' "
                "AND recorded_at > %s AND recorded_at < %s ORDER BY recorded_at",
                (trip[1], trip[2]),
            )
            interior_ids = [row[0] for row in await cur.fetchall()]
        point_id = interior_ids[len(interior_ids) // 2]

        snap_worker = FakeSnapWorker()
        split = _endpoint("/trips/{trip_id}/split")
        response = await split(
            _request(pool, runner, snap_worker), trip[0], point_id, {"sub": "test"}
        )
        assert response.status_code == 204
        assert snap_worker.pokes == 1

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE device = 'DELETEDEV' "
                "AND source = 'detected'"
            )
            assert (await cur.fetchone())[0] == 2
    finally:
        await raw_pool.close()


def test_successful_split_pokes_snap_worker_after_reprocess_commit():
    asyncio.run(_split_pokes_snap_after_commit_scenario())
