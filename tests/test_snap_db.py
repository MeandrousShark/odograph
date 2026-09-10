"""DB-backed regression test for SnapWorker point sourcing.

Like tests/test_runner_db.py, this needs a real Postgres+PostGIS and is
skipped unless TEST_DATABASE_URL is set. It manufactures the documented
boundary-point steal (two adjacent trips sharing one stay fix, where the
single-valued points.trip_id ends up owned by the *second* trip) and asserts
SnapWorker._load_points still recovers the first trip's complete point
sequence, which fixes the route being snapped 1-3km short (the
boundary-point postmortem).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db import make_pool
from app.snap import SnapWorker
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "TESTDEV"
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)


async def _insert_trip(conn, started_at, ended_at, point_count) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, snap_status) "
        "VALUES (%s, 'detected', %s, %s, 1000, %s, 2, 'pending') RETURNING id",
        (DEVICE, started_at, ended_at, point_count),
    )
    return (await cur.fetchone())[0]


async def _insert_point(conn, t, trip_id, accuracy=10.0, lat=47.60, lon=-122.33) -> None:
    await conn.execute(
        "INSERT INTO points (device, recorded_at, received_at, geom, accuracy_m, trip_id) "
        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
        (DEVICE, t, t, lon, lat, accuracy, trip_id),
    )


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        times = [T0 + timedelta(seconds=15 * i) for i in range(9)]
        shared = times[4]  # the single-fix destination stay of trip1 / origin of trip2

        async with pool.connection() as conn:
            trip1 = await _insert_trip(conn, times[0], times[4], point_count=5)
            trip2 = await _insert_trip(conn, times[4], times[8], point_count=5)

            # trip1 owns times[0..3]; the shared boundary fix at times[4] is
            # stolen by trip2 (written second), exactly as the live bug does.
            for t in times[0:4]:
                await _insert_point(conn, t, trip1)
            for t in times[4:9]:
                await _insert_point(conn, t, trip2)

            # A filter-rejected fix inside trip1's span keeps a NULL trip_id and
            # must stay excluded (it wasn't part of the detected trip).
            await _insert_point(
                conn, times[2] + timedelta(seconds=5), None, accuracy=500.0
            )

            # Baseline: the naive trip_id query is short by the stolen point.
            naive = await conn.execute(
                "SELECT count(*) FROM points WHERE trip_id = %s", (trip1,)
            )
            assert (await naive.fetchone())[0] == 4

        worker = SnapWorker(pool, None, "http://osrm", 0.5, 250, 15.0, 300.0)
        async with pool.connection() as conn:
            p1 = await worker._load_points(conn, trip1)
            p2 = await worker._load_points(conn, trip2)

        assert [p.t for p in p1] == times[0:5], "trip1 lost its stolen boundary fix"
        assert all(p.t != times[2] + timedelta(seconds=5) for p in p1), (
            "filter-rejected (NULL trip_id) fix must stay excluded"
        )
        assert [p.t for p in p2] == times[4:9], "trip2 point set changed unexpectedly"
    finally:
        await pool.close()


def test_snapworker_recovers_stolen_boundary_point():
    asyncio.run(_scenario())


async def _unsnappable_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=60), point_count=1)
            await _insert_point(conn, T0, trip)  # only one usable point

        worker = SnapWorker(pool, None, "http://osrm", 0.5, 250, 15.0, 300.0)
        await worker._snap_one(trip)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT snap_status::text FROM trips WHERE id = %s", (trip,)
            )
            status = (await cur.fetchone())[0]
        assert status == "failed", (
            "a <2-point trip must terminate as 'failed', not stay 'pending' forever"
        )
    finally:
        await pool.close()


def test_snapworker_marks_unsnappable_trip_failed():
    asyncio.run(_unsnappable_scenario())


async def _tidy_disabled_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, trip)
            await _insert_point(conn, T0 + timedelta(seconds=15), trip)

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"code": "NoMatch", "matchings": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250, 15.0, 300.0)
            await worker._snap_one(trip)

        assert "tidy=false" in captured["url"], (
            "tidy=true lets OSRM drop closely-spaced points as 'redundant', "
            "which come back as null tracepoints indistinguishable from a "
            "genuine no-match to the match_fraction gate; this misclassified "
            "every trip on a dense (~2-4s) OwnTracks ping interval as "
            "low_confidence despite ~0.98 real matching confidence"
        )
    finally:
        await pool.close()


def test_snapworker_requests_osrm_match_with_tidy_disabled():
    asyncio.run(_tidy_disabled_scenario())


class _RewritingHTTPClient:
    """Stands in for `SnapWorker.http`. Its `.get` performs a detector-style
    rewrite of the trip (bumping `updated_at`, resetting `snap_status` back
    to 'pending') between the point load already done by `_snap_one` and the
    terminal UPDATE that's about to follow, then answers with a normal OSRM
    'Ok' match so the terminal UPDATE has a real result to (attempt to) apply.
    """

    def __init__(self, pool, trip_id):
        self.pool = pool
        self.trip_id = trip_id

    async def get(self, url):
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET updated_at = now(), snap_status = 'pending' "
                "WHERE id = %s",
                (self.trip_id,),
            )
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "matchings": [
                    {
                        "confidence": 0.95,
                        "distance": 1234.5,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-122.33, 47.60], [-122.32, 47.61]],
                        },
                    }
                ],
                "tracepoints": [{}, {}],
            },
        )


async def _stale_result_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, trip)
            await _insert_point(conn, T0 + timedelta(seconds=15), trip)

        worker = SnapWorker(
            pool, _RewritingHTTPClient(pool, trip), "http://osrm", 0.5, 250, 15.0, 300.0
        )
        await worker._snap_one(trip)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT path_snapped IS NULL, distance_snapped_m, snap_status::text "
                "FROM trips WHERE id = %s",
                (trip,),
            )
            path_is_null, distance_snapped_m, status = await cur.fetchone()
        assert path_is_null, (
            "a rewrite that landed mid-snap must not let the stale OSRM "
            "result attach a path_snapped computed from superseded points"
        )
        assert distance_snapped_m is None
        assert status == "pending", (
            "status must stay 'pending' (as the interposed rewrite left it) "
            "so the next sweep re-snaps against the current geometry"
        )
    finally:
        await pool.close()


def test_snapworker_discards_stale_result_after_concurrent_rewrite():
    asyncio.run(_stale_result_scenario())


async def _no_rewrite_scenario():
    """Control for the stale-result test above: with no intervening
    rewrite, the same OSRM response must land normally.
    """
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, trip)
            await _insert_point(conn, T0 + timedelta(seconds=15), trip)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "matchings": [
                        {
                            "confidence": 0.95,
                            "distance": 1234.5,
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-122.33, 47.60], [-122.32, 47.61]],
                            },
                        }
                    ],
                    "tracepoints": [{}, {}],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250, 15.0, 300.0)
            await worker._snap_one(trip)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT path_snapped IS NOT NULL, distance_snapped_m, snap_status::text "
                "FROM trips WHERE id = %s",
                (trip,),
            )
            path_is_set, distance_snapped_m, status = await cur.fetchone()
        assert path_is_set
        assert distance_snapped_m == 1234.5
        assert status == "ok"
    finally:
        await pool.close()


def test_snapworker_applies_result_when_no_concurrent_rewrite():
    asyncio.run(_no_rewrite_scenario())


class _RaceProbeWorker(SnapWorker):
    """Simulates a detector rewrite that commits during the point load
    itself, i.e. after `_load_points`'s own query returns but before
    `_snap_one` reads it back. Under READ COMMITTED, the generation-token
    SELECT and the point-load query are separate statements/snapshots even
    though they share a connection, so this targets the narrower window a
    naive "capture updated_at right after loading points" ordering would
    miss: generation must be captured BEFORE the point load, not after, or a
    rewrite landing in this exact gap makes `generation` reflect the
    post-rewrite value while `points` is still pre-rewrite (stale) - the
    terminal CAS would then wrongly match and apply a stale result.
    """

    def __init__(self, *args, rewrite_pool, trip_id, **kwargs):
        super().__init__(*args, **kwargs)
        self._rewrite_pool = rewrite_pool
        self._rewrite_trip_id = trip_id
        self._rewritten = False

    async def _load_points(self, conn, trip_id):
        points = await super()._load_points(conn, trip_id)
        if not self._rewritten and trip_id == self._rewrite_trip_id:
            self._rewritten = True
            async with self._rewrite_pool.connection() as rconn:
                await rconn.execute(
                    "UPDATE trips SET updated_at = now(), snap_status = 'pending' "
                    "WHERE id = %s",
                    (trip_id,),
                )
        return points


async def _rewrite_during_point_load_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, trip)
            await _insert_point(conn, T0 + timedelta(seconds=15), trip)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "matchings": [
                        {
                            "confidence": 0.95,
                            "distance": 1234.5,
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[-122.33, 47.60], [-122.32, 47.61]],
                            },
                        }
                    ],
                    "tracepoints": [{}, {}],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = _RaceProbeWorker(
                pool, client, "http://osrm", 0.5, 250, 15.0, 300.0,
                rewrite_pool=pool, trip_id=trip,
            )
            await worker._snap_one(trip)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT path_snapped IS NULL, distance_snapped_m, snap_status::text "
                "FROM trips WHERE id = %s",
                (trip,),
            )
            path_is_null, distance_snapped_m, status = await cur.fetchone()
        assert path_is_null, (
            "a rewrite landing during the point load itself (between the "
            "generation-token read and the point-load read returning) must "
            "not let the resulting stale-point OSRM result get applied"
        )
        assert distance_snapped_m is None
        assert status == "pending", (
            "status must stay 'pending' (as the interposed rewrite left it) "
            "so the next sweep re-snaps against the current geometry"
        )
    finally:
        await pool.close()


def test_snapworker_discards_stale_result_when_rewrite_lands_during_point_load():
    asyncio.run(_rewrite_during_point_load_scenario())


async def _manual_trip_immunity_scenario():
    """A manual trip's `snap_status` starts and stays NULL (migrations/
    004_snapping.sql only backfills 'pending' onto source='detected' rows),
    so run_once's `WHERE snap_status = 'pending'` selection already excludes
    it -- but a routed manual trip (this package) is the first manual trip
    to ever carry a real `path`, so this pins down that having road-like
    geometry still doesn't make it eligible for snapping.
    """
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            detected_id = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, detected_id)
            await _insert_point(conn, T0 + timedelta(seconds=15), detected_id)

            manual_row = await conn.execute(
                "INSERT INTO trips (device, source, started_at, ended_at, distance_m, "
                "path, start_geom, end_geom, snap_status) VALUES ("
                "'manual', 'manual', %s, %s, 1200, "
                "ST_SetSRID(ST_GeomFromText('LINESTRING(-122.33 47.60, -122.20 47.70)'), 4326), "
                "ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography, "
                "ST_SetSRID(ST_MakePoint(-122.20, 47.70), 4326)::geography, NULL) RETURNING id",
                (T0, T0 + timedelta(minutes=20)),
            )
            manual_id = (await manual_row.fetchone())[0]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "code": "Ok",
                "matchings": [{
                    "confidence": 0.95,
                    "distance": 1234.5,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-122.33, 47.60], [-122.32, 47.61]],
                    },
                }],
                "tracepoints": [{}, {}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250, 15.0, 300.0)
            await worker.run_once()

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT snap_status::text FROM trips WHERE id = %s", (detected_id,)
            )
            detected_status = (await cur.fetchone())[0]
            cur = await conn.execute(
                "SELECT snap_status::text, path IS NOT NULL FROM trips WHERE id = %s",
                (manual_id,),
            )
            manual_status, manual_has_path = await cur.fetchone()
        assert detected_status == "ok", "the real pending trip must still get snapped normally"
        assert manual_status is None, "a manual trip's snap_status must never be drained to a terminal value"
        assert manual_has_path is True, "its own stored path must be left untouched"
    finally:
        await pool.close()


def test_snapworker_never_drains_manual_trips_including_routed_ones():
    asyncio.run(_manual_trip_immunity_scenario())
