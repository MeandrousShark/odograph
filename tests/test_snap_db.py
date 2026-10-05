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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db import make_pool
from app.snap import MatchPoint, SnapWorker, downsample
from app.worker import BatchOutcome
from app.detector.runner import load_trip_points
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "TESTDEV"
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)



class _StreamHTTP:
    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        response = await self.get(url)
        response.request = httpx.Request(method, url)
        try:
            yield response
        finally:
            await response.aclose()

async def _insert_trip(conn, started_at, ended_at, point_count) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, snap_status) "
        "VALUES (%s, %s, %s, 'detected', %s, %s, 1000, %s, 2, 'pending') RETURNING id",
        (account_id(conn), 1, DEVICE, started_at, ended_at, point_count),
    )
    return (await cur.fetchone())[0]


async def _insert_point(conn, t, trip_id, accuracy=10.0, lat=47.60, lon=-122.33) -> None:
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, received_at, geom, accuracy_m, trip_id) "
        "VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
        (account_id(conn), 1, DEVICE, t, t, lon, lat, accuracy, trip_id),
    )


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
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

        worker = SnapWorker(pool, None, "http://osrm", 0.5, 250)
        async with pool.connection() as conn:
            p1 = await worker._load_points(conn, trip1)
            p2 = await worker._load_points(conn, trip2)

        assert [p.t for p in p1] == times[0:5], "trip1 lost its stolen boundary fix"
        assert all(p.t != times[2] + timedelta(seconds=5) for p in p1), (
            "filter-rejected (NULL trip_id) fix must stay excluded"
        )
        assert [p.t for p in p2] == times[4:9], "trip2 point set changed unexpectedly"
    finally:
        await raw_pool.close()


def test_snapworker_recovers_stolen_boundary_point():
    asyncio.run(_scenario())


async def _unsnappable_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=60), point_count=1)
            await _insert_point(conn, T0, trip)  # only one usable point

        worker = SnapWorker(pool, None, "http://osrm", 0.5, 250)
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
        await raw_pool.close()


def test_snapworker_marks_unsnappable_trip_failed():
    asyncio.run(_unsnappable_scenario())


async def _tidy_disabled_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, trip)
            await _insert_point(conn, T0 + timedelta(seconds=15), trip)

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"code": "NoMatch", "matchings": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250)
            await worker._snap_one(trip)

        assert "tidy=false" in captured["url"], (
            "tidy=true lets OSRM drop closely-spaced points as 'redundant', "
            "which come back as null tracepoints indistinguishable from a "
            "genuine no-match to the match_fraction gate; this misclassified "
            "every trip on a dense (~2-4s) OwnTracks ping interval as "
            "low_confidence despite ~0.98 real matching confidence"
        )
    finally:
        await raw_pool.close()


def test_snapworker_requests_osrm_match_with_tidy_disabled():
    asyncio.run(_tidy_disabled_scenario())


async def _retry_order_scenario():
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
            for i in range(21):
                start = T0 + timedelta(minutes=i)
                trip = await _insert_trip(conn, start, start + timedelta(seconds=15), 2)
                await _insert_point(conn, start, trip)
                await _insert_point(conn, start + timedelta(seconds=15), trip)

        calls = 0

        class FailingHTTP(_StreamHTTP):
            async def get(self, url):
                nonlocal calls
                calls += 1
                raise httpx.ConnectError("temporary outage")

        first = SnapWorker(pool, FailingHTTP(), "http://osrm", 0.5, 250)
        for _ in range(20):
            turn = await first.run_turn()
            assert turn.ready
            assert (turn.batch.attempted, turn.batch.completed,
                    turn.batch.retriable_failures) == (1, 0, 1)
        assert calls == 20
        async with pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id,snap_attempted_at,snapped_at FROM trips ORDER BY id"
            )).fetchall()
        assert all(row[1] is not None and row[2] is None for row in rows[:20])
        assert rows[20][1:] == (None, None)

        # A new worker has no memory of the first batch, but durable attempt
        # order still admits the 21st row before retrying early failures.
        class NoMatchHTTP(_StreamHTTP):
            async def get(self, url):
                return httpx.Response(200, json={"code": "NoMatch", "matchings": []})

        restarted = SnapWorker(pool, NoMatchHTTP(), "http://osrm", 0.5, 250)
        outcome = await restarted.run_once()
        assert (outcome.attempted, outcome.completed, outcome.retriable_failures) == (1, 1, 0)
        async with pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id,snap_status::text FROM trips ORDER BY id"
            )).fetchall()
        assert rows[20][1] == "failed"
        assert all(row[1] == "pending" for row in rows[:20])
        oldest = rows[0][0]

        # Retry eligibility survives restart and prevents a provider busy loop.
        deferred = await restarted.run_turn()
        assert not deferred.ready and deferred.deferred_until is not None
        # A steady new arrival has a later creation age than eligible old
        # failed attempts. Retrying wraps to row one, preserving durable age.
        async with pool.connection() as conn:
            start = T0 + timedelta(hours=1)
            fresh = await _insert_trip(conn, start, start + timedelta(seconds=15), 2)
            await _insert_point(conn, start, fresh)
            await _insert_point(conn, start + timedelta(seconds=15), fresh)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET snap_attempted_at = snap_attempted_at - interval '301 seconds' "
                "WHERE id = %s", (oldest,),
            )
        await restarted.run_once()
        async with pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id,snap_status::text FROM trips WHERE id IN (%s,%s) ORDER BY id",
                (oldest, fresh),
            )).fetchall()
        assert rows == [(oldest, "failed"), (fresh, "pending")]
    finally:
        await raw.close()


def test_retry_attempts_advance_selection_across_restart_and_new_arrivals():
    asyncio.run(_retry_order_scenario())


async def _cancelled_attempt_scenario():
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
            trip_ids = []
            for i in range(2):
                start = T0 + timedelta(minutes=i)
                trip = await _insert_trip(conn, start, start + timedelta(seconds=15), 2)
                trip_ids.append(trip)
                await _insert_point(conn, start, trip)
                await _insert_point(conn, start + timedelta(seconds=15), trip)

        class CancelHTTP(_StreamHTTP):
            async def get(self, url):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await SnapWorker(pool, CancelHTTP(), "http://osrm", 0.5, 250).run_once()
        async with pool.connection() as conn:
            attempts = await (await conn.execute(
                "SELECT id,snap_attempted_at FROM trips ORDER BY id"
            )).fetchall()
        assert attempts[0][1] is not None and attempts[1][1] is None

        class NoMatchHTTP(_StreamHTTP):
            async def get(self, url):
                return httpx.Response(200, json={"code": "NoMatch", "matchings": []})

        await SnapWorker(pool, NoMatchHTTP(), "http://osrm", 0.5, 250).run_once()
        async with pool.connection() as conn:
            statuses = await (await conn.execute(
                "SELECT id,snap_status::text FROM trips ORDER BY id"
            )).fetchall()
        assert statuses == [(trip_ids[0], "pending"), (trip_ids[1], "failed")]
    finally:
        await raw.close()


def test_cancelled_provider_call_keeps_attempt_and_advances_next_worker():
    asyncio.run(_cancelled_attempt_scenario())


@pytest.mark.parametrize("mutation", ["rewrite", "revoke"])
def test_stale_or_revoked_trip_is_not_admitted_to_provider(mutation):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), 2)
                await _insert_point(conn, T0, trip)
                await _insert_point(conn, T0 + timedelta(seconds=15), trip)

            class Probe(SnapWorker):
                async def _load_points(self, conn, trip_id):
                    points = await super()._load_points(conn, trip_id)
                    if mutation == "rewrite":
                        await conn.execute(
                            "UPDATE trips SET updated_at = updated_at + interval '1 second' "
                            "WHERE account_id=%s AND id=%s", (account_id(conn), trip_id),
                        )
                    else:
                        await conn.execute(
                            "UPDATE tracking_devices SET revoked_at=now(), generation=generation+1 "
                            "WHERE account_id=%s AND id=1", (account_id(conn),),
                        )
                    return points

            class HTTP(_StreamHTTP):
                async def get(self, url):
                    pytest.fail("stale work reached provider")

            outcome = await Probe(pool, HTTP(), "http://osrm", 0.5, 250).run_once()
            assert outcome.attempted == 0 and outcome.completed == 0
            async with pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT snap_attempted_at,snap_status::text FROM trips WHERE id=%s", (trip,)
                )).fetchone()
            assert row == (None, "pending")
        finally:
            await raw.close()
    asyncio.run(scenario())


class _RewritingHTTPClient(_StreamHTTP):
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
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
        async with pool.connection() as conn:
            trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, trip)
            await _insert_point(conn, T0 + timedelta(seconds=15), trip)

        worker = SnapWorker(
            pool, _RewritingHTTPClient(pool, trip), "http://osrm", 0.5, 250
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
        await raw_pool.close()


def test_snapworker_discards_stale_result_after_concurrent_rewrite():
    asyncio.run(_stale_result_scenario())


async def _no_rewrite_scenario():
    """Control for the stale-result test above: with no intervening
    rewrite, the same OSRM response must land normally.
    """
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
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
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250)
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
        await raw_pool.close()


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
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
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
                pool, client, "http://osrm", 0.5, 250,
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
        await raw_pool.close()


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
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
        async with pool.connection() as conn:
            detected_id = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), point_count=2)
            await _insert_point(conn, T0, detected_id)
            await _insert_point(conn, T0 + timedelta(seconds=15), detected_id)

            manual_row = await conn.execute(
                "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
                "path, start_geom, end_geom, snap_status) VALUES ("
                "%s, 'manual', 'manual', %s, %s, 1200, "
                "ST_SetSRID(ST_GeomFromText('LINESTRING(-122.33 47.60, -122.20 47.70)'), 4326), "
                "ST_SetSRID(ST_MakePoint(-122.33, 47.60), 4326)::geography, "
                "ST_SetSRID(ST_MakePoint(-122.20, 47.70), 4326)::geography, NULL) RETURNING id",
                (account_id(conn), T0, T0 + timedelta(minutes=20)),
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
            worker = SnapWorker(pool, client, "http://osrm", 0.5, 250)
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
        await raw_pool.close()


def test_snapworker_never_drains_manual_trips_including_routed_ones():
    asyncio.run(_manual_trip_immunity_scenario())


@pytest.mark.parametrize("count, cap", [(7, 5), (12, 5), (1000, 250), (50000, 250)])
def test_bounded_loader_returns_exact_existing_sample(count, cap):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=count), count)
                await conn.execute(
                    "INSERT INTO points (account_id,tracking_device_id,device,recorded_at,received_at,"
                    "geom,accuracy_m,trip_id) "
                    "SELECT %s,1,%s,%s + i * interval '1 second',%s,"
                    "ST_SetSRID(ST_MakePoint(-122.33 + i * 0.000001,47.60),4326)::geography,"
                    "i %% 50,%s FROM generate_series(0,%s) i",
                    (account_id(conn), DEVICE, T0, T0, trip, count - 1),
                )
            worker = SnapWorker(pool, None, "http://osrm", 0.5, cap)
            async with pool.connection(consistent_snapshot=True) as conn:
                rows = await load_trip_points(conn, trip)
                expected = downsample([
                    MatchPoint(t=r[1], lat=r[2], lon=r[3], accuracy_m=r[4]) for r in rows
                ], cap)
                sampled = await worker._load_points(conn, trip)
            assert sampled == expected
            assert len(sampled) <= cap
            assert sampled[0].t == T0
            assert sampled[-1].t == T0 + timedelta(seconds=count - 1)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_sample_count_and_rank_share_snapshot_during_point_insertion():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=90), 7)
                for i in range(7):
                    await _insert_point(conn, T0 + timedelta(seconds=i * 15), trip)

            class RaceProbe(SnapWorker):
                async def _point_count(self, conn, trip_id):
                    count = await super()._point_count(conn, trip_id)
                    async with pool.connection() as other:
                        await _insert_point(other, T0 + timedelta(seconds=1), trip_id)
                    return count

            worker = RaceProbe(pool, None, "http://osrm", 0.5, 5)
            async with pool.connection(consistent_snapshot=True) as conn:
                sampled = await worker._load_points(conn, trip)
            assert [p.t for p in sampled] == [
                T0 + timedelta(seconds=i * 15) for i in [0, 1, 2, 4, 6]
            ]
            async with pool.connection() as conn:
                assert await SnapWorker(pool, None, "http://osrm", 0.5, 5)._point_count(conn, trip) == 8
        finally:
            await raw.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 503])
def test_no_match_body_obeys_provider_http_status(status):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), 2)
                await _insert_point(conn, T0, trip)
                await _insert_point(conn, T0 + timedelta(seconds=15), trip)
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={"code": "NoMatch"})
            )) as client:
                turn = await SnapWorker(pool, client, "http://osrm", 0.5, 250).run_turn()
            assert not turn.ready
            if status == 400:
                assert turn.deferred_until is None
                assert turn.batch == BatchOutcome(attempted=1, completed=1)
            else:
                assert turn.deferred_until is not None
                assert turn.batch == BatchOutcome(attempted=1, retriable_failures=1,
                                                 failure_type="HTTPStatusError")
            async with pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT snap_status::text FROM trips WHERE id=%s", (trip,),
                )).fetchone()
            assert row == (("failed" if status == 400 else "pending"),)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_unadmitted_early_trip_continues_to_later_trip_without_busy_retry():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                early = await _insert_trip(conn, T0, T0 + timedelta(seconds=15), 2)
                later = await _insert_trip(conn, T0 + timedelta(minutes=1),
                                           T0 + timedelta(minutes=1, seconds=15), 2)
                for trip, start in [(early, T0), (later, T0 + timedelta(minutes=1))]:
                    await _insert_point(conn, start, trip)
                    await _insert_point(conn, start + timedelta(seconds=15), trip)

            class AdmissionProbe(SnapWorker):
                async def _snap_one(self, trip_id):
                    if trip_id == early:
                        return BatchOutcome()
                    return await super()._snap_one(trip_id)

            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"code": "NoMatch"})
            )) as client:
                first = await AdmissionProbe(pool, client, "http://osrm", .5, 250).run_turn()
                assert first.ready and first.cursor is not None
                assert first.batch == BatchOutcome()
                # Production constructs a fresh worker for every account turn.
                second = await AdmissionProbe(pool, client, "http://osrm", .5, 250).run_turn(first.cursor)
                assert second.batch == BatchOutcome(attempted=1, completed=1)
                assert not second.ready and second.deferred_until is not None
                assert second.cursor is None
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT id,snap_status::text,snap_attempted_at IS NOT NULL "
                    "FROM trips ORDER BY id",
                )).fetchall()
            assert rows == [(early, "pending", False), (later, "failed", True)]
        finally:
            await raw.close()
    asyncio.run(scenario())
