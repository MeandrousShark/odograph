"""DB-backed regression tests for the detector runner.

Unlike the rest of the suite, these need a real Postgres+PostGIS instance and
are skipped unless ``TEST_DATABASE_URL`` is set, so a plain ``pytest`` on a
machine without a database still runs everything else. The target database is
wiped (``DROP SCHEMA public``) on each run — point it at a throwaway DB only.

    TEST_DATABASE_URL=postgresql://mileage:pw@127.0.0.1:5432/mileage pytest tests/test_runner_db.py

Why a DB test at all when detector logic is otherwise pure: the bug covered
here lives in the *orchestration* — how `_process_device` nulls and reassigns
`points.trip_id` across an incremental reprocess window — which the pure
`detect()` tests structurally cannot reach.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

import psycopg
from fastapi import HTTPException

import app.detector.runner as runner_module
from app.autotag import AutotagResult
from app.db import make_pool, run_migrations
from app.detector.core import Params
from app.detector.runner import (
    ADVISORY_LOCK_KEY, DetectorRunner, load_trip_points, reprocess_places, resolve_and_autotag,
)
from app.ui import _validate_split_distance
from app.vehicles import deactivate_vehicle, list_vehicles, set_auto_assign_default_vehicle
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "TESTDEV"


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_points(conn, points) -> None:
    # received_at = recorded_at (i.e. "already ingested long ago") so that
    # later bumping one point's received_at is what marks the dirty window.
    for p in points:
        await conn.execute(
            "INSERT INTO points (device, recorded_at, received_at, geom, "
            " accuracy_m, velocity_kmh) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
            (DEVICE, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh),
        )


async def _trip_counts(conn):
    """(trip_id, stored point_count, live count of points carrying its
    trip_id) per detected trip, oldest first."""
    cur = await conn.execute(
        "SELECT t.id, t.point_count, count(p.id) "
        "FROM trips t LEFT JOIN points p ON p.trip_id = t.id "
        "WHERE t.source = 'detected' "
        "GROUP BY t.id, t.point_count, t.started_at "
        "ORDER BY t.started_at"
    )
    return [tuple(r) for r in await cur.fetchall()]


async def _mark_terminally_snapped(conn, trip_ids: list[int]) -> None:
    await conn.execute(
        "UPDATE trips SET snap_status = 'ok', "
        "path_snapped = ST_GeomFromText("
        " 'MULTILINESTRING((-122.33 47.60, -122.30 47.62))', 4326), "
        "distance_snapped_m = 4321.5, snapped_at = '2026-07-01T12:00:00Z' "
        "WHERE id = ANY(%s)",
        (trip_ids,),
    )


async def _snap_rows(conn) -> list[tuple]:
    cur = await conn.execute(
        "SELECT id, snap_status::text, ST_AsText(path_snapped), "
        "distance_snapped_m, snapped_at FROM trips "
        "WHERE source = 'detected' ORDER BY started_at, id"
    )
    return [tuple(row) for row in await cur.fetchall()]


async def _run_unchanged_full_reprocess_preserves_snap_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([
            Stationary(900), Drive(km=2), Stationary(1200),
            Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trip_ids = [row[0] for row in await _trip_counts(conn)]
            assert len(trip_ids) == 2
            await _mark_terminally_snapped(conn, trip_ids)
            before = await _snap_rows(conn)

        await runner.reprocess_device_now(DEVICE)

        async with pool.connection() as conn:
            after = await _snap_rows(conn)
        assert after == before
    finally:
        await pool.close()


def test_unchanged_matched_trips_preserve_terminal_snap_across_full_reprocess():
    asyncio.run(_run_unchanged_full_reprocess_preserves_snap_scenario())


async def _run_legacy_raw_path_preserves_snap_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([
            Stationary(900), Drive(km=3), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            trip_id = (await _trip_counts(conn))[0][0]
            await _mark_terminally_snapped(conn, [trip_id])
            await conn.execute(
                "UPDATE trips SET path = ST_GeomFromText("
                " 'LINESTRING(-122.33 47.60, -122.32 47.61)', 4326) "
                "WHERE id = %s",
                (trip_id,),
            )
            cur = await conn.execute(
                "SELECT ST_AsEWKB(path), snap_status::text, ST_AsText(path_snapped), "
                "distance_snapped_m, snapped_at FROM trips WHERE id = %s",
                (trip_id,),
            )
            legacy_path, *snap_before = await cur.fetchone()

        await runner.reprocess_device_now(DEVICE)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT ST_AsEWKB(path), snap_status::text, ST_AsText(path_snapped), "
                "distance_snapped_m, snapped_at FROM trips WHERE id = %s",
                (trip_id,),
            )
            corrected_path, *snap_after = await cur.fetchone()
        assert corrected_path != legacy_path
        assert snap_after == snap_before
    finally:
        await pool.close()


def test_legacy_raw_path_is_rewritten_without_invalidating_unchanged_snap_inputs():
    asyncio.run(_run_legacy_raw_path_preserves_snap_scenario())


async def _run_incremental_reprocess_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)

        # stay A -> drive -> stay B -> drive -> stay C: two detectable trips.
        pts = build_track([
            Stationary(1200),
            Drive(km=2),
            Stationary(1200),
            Drive(km=2),
            Stationary(1200),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)

        runner = DetectorRunner(pool, Params())

        # First pass: detector_state seeds detector_version=0 != DETECTOR_VERSION,
        # so this takes the full-reprocess path (t0 = epoch) and assigns every
        # point, including both trips' arrival boundary points.
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            before = await _trip_counts(conn)
        assert len(before) == 2, f"expected 2 trips after full reprocess, got {before}"
        for tid, pc, live in before:
            assert live == pc, f"full reprocess already inconsistent: {before}"
        first_trip_id = before[0][0]
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET purpose='Client meeting', notes='keep separate' WHERE id=%s",
                (first_trip_id,),
            )

        # Now a late point lands in the LAST stay (its received_at becomes
        # "now", after the run we just committed). The next run is incremental
        # and its window rewinds to the start of the MIDDLE stay — whose first
        # point is exactly the first trip's arrival boundary. That trip is not
        # in the window and is never re-emitted, so the reset must not orphan
        # its arrival point.
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE points SET received_at = now() WHERE device = %s AND "
                "recorded_at = (SELECT max(recorded_at) FROM points WHERE device = %s)",
                (DEVICE, DEVICE),
            )
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            after = await _trip_counts(conn)
        by_id = {tid: (pc, live) for tid, pc, live in after}
        assert first_trip_id in by_id, f"first trip vanished after reprocess: {after}"
        pc, live = by_id[first_trip_id]
        assert live == pc, (
            f"trip {first_trip_id} orphaned its arrival boundary point after an "
            f"incremental reprocess: point_count={pc} but only {live} points "
            f"still carry its trip_id"
        )
        for tid, pc, live in after:
            assert live == pc, f"trip {tid} point_count/trip_id mismatch: {after}"
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT purpose, notes FROM trips WHERE id=%s", (first_trip_id,)
            )
            assert await cur.fetchone() == ("Client meeting", "keep separate")
    finally:
        await pool.close()


def test_incremental_reprocess_keeps_arrival_boundary_point():
    asyncio.run(_run_incremental_reprocess_scenario())


async def _run_reprocess_places_lock_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    holder = await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True)
    try:
        await _reset_schema(pool)

        # Hold the detector's advisory lock on a separate session, mimicking an
        # in-flight detector run. reprocess_places must block on it, not barge
        # in and race the concurrent trips.category writes.
        await holder.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))

        task = asyncio.create_task(reprocess_places(pool))
        await asyncio.sleep(0.5)
        assert not task.done(), (
            "reprocess_places ran while the detector lock was held — it must "
            "wait for the detector to finish before re-tagging trips"
        )

        # Releasing the lock lets it proceed and finish promptly.
        await holder.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and task.exception() is None
    finally:
        await holder.close()
        await pool.close()


def test_reprocess_places_waits_for_detector_lock():
    asyncio.run(_run_reprocess_places_lock_scenario())


async def _run_lock_released_on_sql_error_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([Stationary(900), Drive(km=2), Stationary(900)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)

        runner = DetectorRunner(pool, Params())
        real_process_device = runner._process_device
        calls = {"n": 0}

        async def _process_device_first_call_broken(conn, device, dirty_from, full):
            calls["n"] += 1
            if calls["n"] == 1:
                # A genuine SQL error (not a mocked Python exception), so the
                # connection's transaction is actually left aborted by
                # Postgres -- the same server-side state the pre-fix
                # try/finally unlock choked on.
                await conn.execute("SELECT 1/0")
            return await real_process_device(conn, device, dirty_from, full)

        runner._process_device = _process_device_first_call_broken

        with pytest.raises(psycopg.errors.DivisionByZero):
            await runner.run_once()

        # The failed run's connection is returned to the pool and rolled
        # back there, which -- now that the lock is xact-scoped -- must
        # release it. A fresh session-level try-lock on a separate
        # connection proves it didn't leak.
        holder = await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True)
        try:
            cur = await holder.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            assert (await cur.fetchone())[0] is True, (
                "advisory lock leaked after a SQL error mid-_run"
            )
            await holder.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        finally:
            await holder.close()

        # A subsequent run_once() (a different connection borrowed from the
        # pool) must be able to acquire the lock and actually run.
        runner._process_device = real_process_device
        assert await runner.run_once() is True
    finally:
        await pool.close()


def test_advisory_lock_not_leaked_on_sql_error_mid_run():
    """A SQL error mid-`_run` must propagate the original exception (not
    `InFailedSqlTransaction` from a session-lock unlock choking on the
    aborted transaction) and must not leak the advisory lock."""
    asyncio.run(_run_lock_released_on_sql_error_scenario())


# --- merge/split overrides, via the DB-backed runner ------

async def _run_merge_via_override_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            before = await _trip_counts(conn)
        assert len(before) == 2

        async with pool.connection() as conn:
            await _mark_terminally_snapped(conn, [row[0] for row in before])

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT started_at, ended_at FROM stays WHERE device=%s ORDER BY started_at",
                (DEVICE,),
            )
            middle_start, middle_end = (await cur.fetchall())[1]
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, range_start, range_end) "
                "VALUES (%s, 'suppress', %s, %s)",
                (DEVICE, middle_start, middle_end),
            )

        await runner.reprocess_device_now(DEVICE)

        async with pool.connection() as conn:
            after = await _trip_counts(conn)
            snap_after = await _snap_rows(conn)
        assert len(after) == 1, f"expected the override to merge into one trip, got {after}"
        tid, pc, live = after[0]
        assert live == pc
        assert snap_after == [(tid, "pending", None, None, None)]
    finally:
        await pool.close()


def test_suppress_override_merges_via_reprocess_device_now():
    """Case 26: a suppress override for the exact stay separating two
    adjacent detected trips merges them into one via reprocess_device_now."""
    asyncio.run(_run_merge_via_override_scenario())


async def _run_split_via_override_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([Stationary(1200), Drive(km=5, speed_kmh=50), Stationary(1800)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            before = await _trip_counts(conn)
        assert len(before) == 1
        trip_id = before[0][0]

        async with pool.connection() as conn:
            await _mark_terminally_snapped(conn, [trip_id])

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM points WHERE device=%s AND trip_id=%s ORDER BY recorded_at",
                (DEVICE, trip_id),
            )
            point_ids = [r[0] for r in await cur.fetchall()]
        split_id = point_ids[len(point_ids) // 2]
        assert split_id not in (point_ids[0], point_ids[-1])  # not a no-op boundary pin (case 22)

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, point_id) VALUES (%s, 'force', %s)",
                (DEVICE, split_id),
            )

        await runner.reprocess_device_now(DEVICE)

        async with pool.connection() as conn:
            after = await _trip_counts(conn)
            assert len(after) == 2, f"expected the override to split into two trips, got {after}"
            points_a = await load_trip_points(conn, after[0][0])
            points_b = await load_trip_points(conn, after[1][0])
            snap_after = await _snap_rows(conn)

        ids_a = {r[0] for r in points_a}
        ids_b = {r[0] for r in points_b}
        assert split_id in ids_a and split_id in ids_b, (
            "the pinned split point is the shared boundary of both halves, "
            "so load_trip_points (time-range) must return it for both"
        )
        assert len(points_a) == after[0][1]  # matches stored point_count
        assert len(points_b) == after[1][1]
        # points_a/points_b double-count the shared boundary point (one
        # query per side); trip_id is single-valued, so the naive live
        # count from _trip_counts can only ever credit it to one side —
        # this is exactly the boundary-point-stealing bug load_trip_points
        # (not `WHERE trip_id = X`) fixes.
        naive_a, naive_b = after[0][2], after[1][2]
        assert naive_a + naive_b == len(points_a) + len(points_b) - 1
        assert snap_after == [
            (after[0][0], "pending", None, None, None),
            (after[1][0], "pending", None, None, None),
        ]
    finally:
        await pool.close()


def test_force_override_splits_via_reprocess_device_now():
    """Case 27: a force override on a mid-drive point splits one trip into
    two; load_trip_points (not points.trip_id) returns the correct,
    non-stolen point set for both halves."""
    asyncio.run(_run_split_via_override_scenario())


async def _run_merge_survives_incremental_reprocess_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([
            Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900),
        ])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            before = await _trip_counts(conn)
        assert len(before) == 2

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT started_at, ended_at FROM stays WHERE device=%s ORDER BY started_at",
                (DEVICE,),
            )
            middle_start, middle_end = (await cur.fetchall())[1]
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, range_start, range_end) "
                "VALUES (%s, 'suppress', %s, %s)",
                (DEVICE, middle_start, middle_end),
            )
        await runner.reprocess_device_now(DEVICE)
        async with pool.connection() as conn:
            merged = await _trip_counts(conn)
        assert len(merged) == 1, f"expected the override to merge into one trip, got {merged}"

        # A late point lands inside the now-suppressed stay's original span,
        # ingested via a *normal* dirty-window reprocess (run_once, not the
        # merge endpoint's reprocess_device_now) -- rewind lands back at the
        # origin stay (the only stay still ending before this dirty point),
        # so this run genuinely re-derives across the merged boundary. The
        # merge must hold, not silently revert.
        mid_stay_pts = [p for p in pts if middle_start <= p.t <= middle_end]
        late_t = middle_start + timedelta(seconds=5)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO points (device, recorded_at, received_at, geom, accuracy_m, velocity_kmh) "
                "VALUES (%s, %s, now(), ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
                (DEVICE, late_t, mid_stay_pts[0].lon, mid_stay_pts[0].lat, 10.0, 0.0),
            )
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            after = await _trip_counts(conn)
        assert len(after) == 1, (
            f"merge should survive a normal incremental reprocess touching its span, got {after}"
        )
    finally:
        await pool.close()


def test_merge_survives_incremental_reprocess():
    """Case 28: the regression test for this milestone's core promise -- a
    late point landing inside an already-merged span, handled by a normal
    incremental reprocess (not the merge endpoint), must not undo the
    merge."""
    asyncio.run(_run_merge_survives_incremental_reprocess_scenario())


async def _run_split_survives_incremental_reprocess_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([Stationary(1200), Drive(km=5, speed_kmh=50), Stationary(1800)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            before = await _trip_counts(conn)
        assert len(before) == 1
        trip_id = before[0][0]

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM points WHERE device=%s AND trip_id=%s ORDER BY recorded_at",
                (DEVICE, trip_id),
            )
            point_ids = [r[0] for r in await cur.fetchall()]
        split_id = point_ids[len(point_ids) // 2]
        assert split_id not in (point_ids[0], point_ids[-1])

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trip_boundary_overrides (device, kind, point_id) VALUES (%s, 'force', %s)",
                (DEVICE, split_id),
            )
        await runner.reprocess_device_now(DEVICE)
        async with pool.connection() as conn:
            split = await _trip_counts(conn)
        assert len(split) == 2, f"expected the override to split into two trips, got {split}"

        # A late point lands right at the very start of the track (so the
        # incremental rewind can't find any settled stay ending before it
        # and falls all the way back to EPOCH), ingested via a normal
        # dirty-window reprocess -- this forces a genuine full re-derivation
        # across the pinned split point, alongside a brand-new neighboring
        # point that could otherwise perturb what "the point before it" is.
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO points (device, recorded_at, received_at, geom, accuracy_m, velocity_kmh) "
                "VALUES (%s, %s, now(), ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
                (DEVICE, pts[0].t + timedelta(seconds=5), pts[0].lon, pts[0].lat, 10.0, 0.0),
            )
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            after = await _trip_counts(conn)
        assert len(after) == 2, (
            f"split should survive a normal incremental reprocess, got {after}"
        )
    finally:
        await pool.close()


def test_split_survives_incremental_reprocess():
    """Case 29: same regression as case 28, for a force-split -- the pinned
    point must still survive filtering (and the split must hold) once new
    neighboring data arrives via a normal incremental reprocess."""
    asyncio.run(_run_split_survives_incremental_reprocess_scenario())


async def _run_split_distance_validation_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([Stationary(1200), Drive(km=5, speed_kmh=50), Stationary(1800)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True
        async with pool.connection() as conn:
            trip_id = (await _trip_counts(conn))[0][0]
            points = await load_trip_points(conn, trip_id)
            with pytest.raises(HTTPException, match="too close"):
                await _validate_split_distance(
                    conn, trip_id, points[-2][0], Params().min_trip_distance_m
                )
            first_m, second_m = await _validate_split_distance(
                conn, trip_id, points[len(points) // 2][0], Params().min_trip_distance_m
            )
        assert first_m >= Params().min_trip_distance_m
        assert second_m >= Params().min_trip_distance_m
    finally:
        await pool.close()


def test_split_distance_validation_rejects_short_half():
    asyncio.run(_run_split_distance_validation_scenario())


async def _vehicle_id(conn) -> int:
    return (await list_vehicles(conn))[0]["id"]  # 008_vehicles.sql seeds "My Car"


async def _run_auto_assign_on_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            default_id = await _vehicle_id(conn)
            await set_auto_assign_default_vehicle(conn, True)
        pts = build_track([Stationary(900), Drive(km=2), Stationary(900)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT vehicle_id FROM trips WHERE source = 'detected'"
            )
            rows = await cur.fetchall()
        assert rows == [(default_id,)]
    finally:
        await pool.close()


def test_detector_insert_assigns_default_vehicle_when_auto_assign_is_on():
    asyncio.run(_run_auto_assign_on_scenario())


async def _run_auto_assign_off_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        pts = build_track([Stationary(900), Drive(km=2), Stationary(900)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT vehicle_id FROM trips WHERE source = 'detected'"
            )
            rows = await cur.fetchall()
        assert rows == [(None,)]
    finally:
        await pool.close()


def test_detector_insert_leaves_vehicle_null_when_auto_assign_is_off():
    """auto_assign_default_vehicle defaults to false (migrations/018), so a
    fresh install's detected trips must stay unassigned."""
    asyncio.run(_run_auto_assign_off_scenario())


async def _run_auto_assign_default_deactivated_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            default_id = await _vehicle_id(conn)
            await set_auto_assign_default_vehicle(conn, True)
            await deactivate_vehicle(conn, default_id)
        pts = build_track([Stationary(900), Drive(km=2), Stationary(900)])
        async with pool.connection() as conn:
            await _insert_points(conn, pts)
        runner = DetectorRunner(pool, Params())
        assert await runner.run_once() is True

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT vehicle_id FROM trips WHERE source = 'detected'"
            )
            rows = await cur.fetchall()
        assert rows == [(None,)]
    finally:
        await pool.close()


def test_detector_insert_leaves_vehicle_null_when_default_is_deactivated():
    """deactivate_vehicle clears is_default, so the auto-assign subselect's
    `v.is_default AND v.active` guard yields nothing even with the setting
    still on."""
    asyncio.run(_run_auto_assign_default_deactivated_scenario())


# --- reprocess_places / resolve_and_autotag vs. imported trips ------

T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)


async def _run_reprocess_places_imported_guard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO places (name, kind, geom) VALUES "
                "('Home', 'home', ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography), "
                "('Work', 'work', ST_SetSRID(ST_MakePoint(-122.30, 47.62), 4326)::geography) "
                "RETURNING id"
            )
            home_id, work_id = [r[0] for r in await cur.fetchall()]

            # A portable-imported trip (app/portable.py): resolved places and
            # a rule tag, but no geometry, since the bundle format carries
            # none. reprocess_places must leave it exactly as imported, not
            # null out its places (start_geom/end_geom IS NULL, so the
            # resolution subselects would otherwise match nothing) and
            # cascade into reverting its tag.
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " imported, start_place_id, end_place_id, category, tag_source) "
                "VALUES (%s, 'detected', %s, %s, 1000, true, %s, %s, 'personal', 'rule') "
                "RETURNING id",
                (DEVICE, T0, T0 + timedelta(minutes=20), home_id, work_id),
            )
            imported_id = (await cur.fetchone())[0]

            # A normal, non-imported detected trip with real geometry at the
            # same two places but not yet resolved, standing in for a fresh
            # detector insert awaiting its first reprocess. The guard must
            # not stop this one from being resolved and autotagged exactly
            # as before.
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " start_geom, end_geom) "
                "VALUES (%s, 'detected', %s, %s, 1000, "
                " ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography, "
                " ST_SetSRID(ST_MakePoint(-122.30, 47.62), 4326)::geography) "
                "RETURNING id",
                (DEVICE, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=20)),
            )
            live_id = (await cur.fetchone())[0]

        await reprocess_places(pool)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT start_place_id, end_place_id, category::text, tag_source::text "
                "FROM trips WHERE id = %s",
                (imported_id,),
            )
            assert await cur.fetchone() == (home_id, work_id, "personal", "rule"), (
                "reprocess_places must not touch an imported trip's places or tag"
            )

            cur = await conn.execute(
                "SELECT start_place_id, end_place_id, category::text, tag_source::text "
                "FROM trips WHERE id = %s",
                (live_id,),
            )
            assert await cur.fetchone() == (home_id, work_id, "personal", "rule"), (
                "a normal detected trip must still be resolved and autotagged"
            )
    finally:
        await pool.close()


def test_reprocess_places_skips_imported_trips_but_still_resolves_live_ones():
    """migrations/019_trip_imported.sql's NOT imported guard reached the
    reconcile and merge paths but missed reprocess_places and
    resolve_and_autotag: any places/rules CRUD in Settings would null out an
    imported trip's places (its start_geom/end_geom is NULL, so the
    resolution subselects match nothing) and, via plan_autotags's revert
    branch, wipe its rule tag back to unclassified. This is the regression
    test for that guard, alongside proof that a genuine non-imported trip is
    still processed normally in the same reprocess_places call."""
    asyncio.run(_run_reprocess_places_imported_guard_scenario())


async def _run_reprocess_places_human_tag_survives_matching_rule_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO places (name, kind, geom) VALUES "
                "('Home', 'home', ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography), "
                "('Work', 'work', ST_SetSRID(ST_MakePoint(-122.30, 47.62), 4326)::geography)"
            )
            await conn.execute(
                "INSERT INTO tag_rules (a_kind, b_kind, category) VALUES ('home', 'work', 'business')"
            )
            # A human-classified trip whose endpoints resolve to exactly this
            # rule's places, so plan_autotags would reclassify it to
            # 'business'/'rule' if the human tag weren't protected.
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " start_geom, end_geom, category, tag_source) "
                "VALUES (%s, 'detected', %s, %s, 1000, "
                " ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography, "
                " ST_SetSRID(ST_MakePoint(-122.30, 47.62), 4326)::geography, "
                " 'personal', 'human') RETURNING id",
                (DEVICE, T0, T0 + timedelta(minutes=20)),
            )
            trip_id = (await cur.fetchone())[0]

        await reprocess_places(pool)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, tag_source::text FROM trips WHERE id = %s", (trip_id,)
            )
            assert await cur.fetchone() == ("personal", "human"), (
                "a human classification must never be overwritten by autotag, "
                "even when a rule matches"
            )
    finally:
        await pool.close()


def test_reprocess_places_never_overwrites_human_tag_even_with_matching_rule():
    """The rider fix to resolve_and_autotag's apply-loop UPDATE (AND
    tag_source IS DISTINCT FROM 'human'): plan_autotags already skips
    human-tagged trips in Python, but this proves the invariant holds at the
    SQL layer too, for a trip whose start/end genuinely match a real rule."""
    asyncio.run(_run_reprocess_places_human_tag_survives_matching_rule_scenario())


async def _run_apply_loop_sql_guard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                " category, tag_source) "
                "VALUES (%s, 'detected', %s, %s, 1000, 'personal', 'human') RETURNING id",
                (DEVICE, T0, T0 + timedelta(minutes=20)),
            )
            trip_id = (await cur.fetchone())[0]

            # Bypass plan_autotags' own human-tag_source filter (already
            # covered by tests/test_autotag.py) to isolate the apply loop's
            # SQL WHERE clause as the thing actually under test here: a
            # stale row read (e.g. one taken just before a concurrent
            # human-tag write commits) must not let a forced "rule" result
            # overwrite a trip that is, at UPDATE time, tag_source='human'.
            original_plan_autotags = runner_module.plan_autotags
            runner_module.plan_autotags = lambda trips, rules: [
                AutotagResult(trip_id=trip_id, category="business", tag_source="rule")
            ]
            try:
                await resolve_and_autotag(conn, [trip_id])
            finally:
                runner_module.plan_autotags = original_plan_autotags

            cur = await conn.execute(
                "SELECT category::text, tag_source::text FROM trips WHERE id = %s", (trip_id,)
            )
            assert await cur.fetchone() == ("personal", "human"), (
                "the apply loop's UPDATE must refuse to overwrite tag_source='human' "
                "on its own, independent of plan_autotags' own filtering"
            )
    finally:
        await pool.close()


def test_resolve_and_autotag_apply_loop_sql_guard_blocks_stale_rule_result():
    """Isolates the new `AND tag_source IS DISTINCT FROM 'human'` guard from
    plan_autotags' pre-existing Python-level filter by forcing plan_autotags
    to (unrealistically) hand back a 'rule' result for an already
    human-tagged trip -- the guard must still refuse the UPDATE."""
    asyncio.run(_run_apply_loop_sql_guard_scenario())
