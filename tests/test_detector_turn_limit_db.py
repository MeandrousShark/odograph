"""DETECTOR_TURN_MAX_POINTS background deferral."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import pytest

import app.detector.runner as runner_module
from app.account_context import account_id
from app.account_settings import AccountSettings
from app.account_workers import AccountWorker
from app.db import make_pool
from app.detector.core import Params
from app.detector.runner import (
    DETECTOR_VERSION, WINDOW_COUNT_SQL, DetectorDeferrals, DetectorRunner,
)
from conftest import add_test_account, reset_account_db, seed_tracking_device
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

OWNER = 41
TRACK = [Stationary(900), Drive(km=2), Stationary(1200), Drive(km=2), Stationary(900)]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CountingRunner(DetectorRunner):
    counts = 0

    async def _count_window(self, conn, device, t0, limit):
        type(self).counts += 1
        return await super()._count_window(conn, device, t0, limit)


def _counting():
    class Runner(CountingRunner):
        counts = 0
    return Runner


def _run(body):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            return await body(raw)
        finally:
            await raw.close()
    return asyncio.run(scenario())


async def _insert_track(conn, device, points, *, dirty=False):
    """received_at = recorded_at keeps old fixes behind any checkpoint;
    `dirty` lets the column default mark them as newly received."""
    owner = account_id(conn)
    async with conn.cursor() as cur:
        if dirty:
            await cur.executemany(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,accuracy_m,velocity_kmh) "
                "VALUES(%s,%s,'phone',%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s)",
                [(owner, device, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh) for p in points])
        else:
            await cur.executemany(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,received_at,geom,"
                "accuracy_m,velocity_kmh) "
                "VALUES(%s,%s,'phone',%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s)",
                [(owner, device, p.t, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh) for p in points])


async def _bulk_points(conn, device, count):
    """Stationary filler, inserted server-side without per-row round trips."""
    await conn.execute(
        "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,received_at,geom,accuracy_m) "
        "SELECT %s,%s,'phone',t,t,ST_SetSRID(ST_MakePoint(-122.6,45.5),4326)::geography,10 "
        "FROM (SELECT timestamptz '2026-06-01' + i * interval '15 seconds' AS t "
        "FROM generate_series(1,%s) i) s",
        (account_id(conn), device, count))


async def _snapshot(conn):
    """Every detector-owned row, ids included, for zero-write assertions."""
    owner = account_id(conn)
    queries = {
        "state": "SELECT tracking_device_id,last_run_at,detector_version FROM detector_state "
                 "WHERE account_id=%s ORDER BY 1",
        "stays": "SELECT id,tracking_device_id,started_at,ended_at,ST_AsText(centroid::geometry),point_count "
                 "FROM stays WHERE account_id=%s ORDER BY id",
        "trips": "SELECT id,tracking_device_id,started_at,ended_at,distance_m,point_count,snap_status::text,"
                 "updated_at FROM trips WHERE account_id=%s ORDER BY id",
        "points": "SELECT id,trip_id FROM points WHERE account_id=%s ORDER BY id",
    }
    return {name: [tuple(row) for row in await (await conn.execute(sql, (owner,))).fetchall()]
            for name, sql in queries.items()}


async def _outputs(conn):
    """Detector output compared across runs, without ids or run timestamps."""
    owner = account_id(conn)
    stays = await (await conn.execute(
        "SELECT tracking_device_id,started_at,ended_at,ST_AsText(centroid::geometry),point_count "
        "FROM stays WHERE account_id=%s ORDER BY 1,2,3", (owner,))).fetchall()
    trips = await (await conn.execute(
        "SELECT tracking_device_id,started_at,ended_at,distance_m,point_count,has_gap,"
        "ST_AsText(start_geom::geometry),ST_AsText(end_geom::geometry),ST_AsText(path),"
        "detector_version,snap_status::text,category::text,tag_source::text,source::text "
        "FROM trips WHERE account_id=%s ORDER BY 1,2,3", (owner,))).fetchall()
    assigned = await (await conn.execute(
        "SELECT p.recorded_at,t.started_at FROM points p LEFT JOIN trips t ON t.id=p.trip_id "
        "WHERE p.account_id=%s ORDER BY p.recorded_at,p.id", (owner,))).fetchall()
    versions = await (await conn.execute(
        "SELECT tracking_device_id,detector_version,last_run_at IS NOT NULL FROM detector_state "
        "WHERE account_id=%s ORDER BY 1", (owner,))).fetchall()
    return [tuple(r) for r in stays], [tuple(r) for r in trips], [tuple(r) for r in assigned], \
        [tuple(r) for r in versions]


async def _drain(runner, cursor=None):
    outcomes = []
    while True:
        outcome = await runner.run_turn(cursor)
        outcomes.append(outcome)
        if not outcome.ready:
            return outcomes
        cursor = outcome.cursor


async def _stale_version(conn, device):
    await conn.execute(
        "UPDATE detector_state SET detector_version=%s, last_run_at=now() "
        "WHERE account_id=%s AND tracking_device_id=%s", (DETECTOR_VERSION - 1, account_id(conn), device))


def test_over_limit_device_defers_without_writes_or_recounting(caplog):
    caplog.set_level(logging.INFO, logger="app.detector.runner")

    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "big")
            await _insert_track(conn, device, build_track(TRACK))
        # Existing output, then a version mismatch: a full window over the limit.
        assert await DetectorRunner(pool, Params()).run_once()
        async with pool.connection() as conn:
            await _stale_version(conn, device)
            before = await _snapshot(conn)
        assert before["trips"] and before["stays"]
        clock = _Clock()
        deferrals = DetectorDeferrals(clock=clock)
        Runner = _counting()
        runner = Runner(pool, Params(), turn_max_points=50, deferrals=deferrals)

        deferred = await runner.run_turn()
        assert deferred.ready and deferred.cursor == device and deferred.deferred_until is None
        assert deferred.batch.attempted == 0 and not runner.produced_work
        assert deferrals.devices() == ((OWNER, device),)
        assert Runner.counts == 1
        idle = await runner.run_turn(deferred.cursor)
        assert not idle.ready and idle.deferred_until is None

        # Later wake cycles before the recheck skip without recounting.
        for _ in range(3):
            again = await Runner(pool, Params(), turn_max_points=50, deferrals=deferrals).run_turn()
            assert not again.ready and again.cursor is None and again.batch.attempted == 0
        assert Runner.counts == 1
        async with pool.connection() as conn:
            assert await _snapshot(conn) == before

        clock.now += runner_module.DEFERRAL_RECHECK_S
        rechecked = await runner.run_turn()
        assert rechecked.ready and rechecked.cursor == device
        assert Runner.counts == 2
        async with pool.connection() as conn:
            assert await _snapshot(conn) == before
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "deferring" in r.message]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert f"account={OWNER} device={device}" in message and ">50 points" in message
        assert "app.reprocess_detector" in message
    _run(body)


def test_account_worker_goes_idle_when_only_deferred_devices_remain():
    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            big = await seed_tracking_device(conn, "big")
            await _insert_track(conn, big, build_track(TRACK))
        deferrals = DetectorDeferrals(clock=_Clock())
        Runner = _counting()
        poked = []
        worker = AccountWorker(
            SimpleNamespace(control=pool.control_pool, runtime=pool.runtime_pool),
            AccountSettings(ZoneInfo("UTC")),
            lambda account_pool, config: Runner(account_pool, Params(), turn_max_points=50,
                                                deferrals=deferrals),
            label="detector-scheduler", debounce_s=0, sweep_s=60,
            after_run=lambda: poked.append("poked"),
        )
        deferrals.bind(worker.status)
        first = await worker.run_turn()
        assert first.ready and first.cursor == big
        assert worker._continuation_at is not None
        second = await worker.run_turn()
        assert not second.ready and second.deferred_until is None
        assert worker._continuation_at is None
        # A poke or sweep wakes the account; the deferred device is skipped
        # without a count and the account is idle again after one turn.
        for _ in range(3):
            worker.wake_cycle()
            await worker._run_guarded()
            assert not worker._continuations[OWNER].ready
            assert worker._continuation_at is None
        assert Runner.counts == 1
        assert poked == []
        assert worker.status.deferred_devices == ((OWNER, big),)
    _run(body)


def test_other_devices_and_accounts_proceed_while_one_device_is_deferred():
    async def body(raw):
        first = await reset_account_db(raw)
        second = await add_test_account(raw, 99)
        small = build_track([Stationary(600), Drive(km=2), Stationary(600)], interval_s=60)
        async with first.connection() as conn:
            big = await seed_tracking_device(conn, "big")
            other = await seed_tracking_device(conn, "small")
            await _insert_track(conn, big, build_track(TRACK))
            await _insert_track(conn, other, small)
        async with second.connection() as conn:
            remote = await seed_tracking_device(conn, "remote")
            await _insert_track(conn, remote, small)
        async with first.connection() as conn:
            before_big = [row for row in (await _snapshot(conn))["state"] if row[0] == big]
        deferrals = DetectorDeferrals(clock=_Clock())
        worker = AccountWorker(
            SimpleNamespace(control=first.control_pool, runtime=first.runtime_pool),
            AccountSettings(ZoneInfo("UTC")),
            lambda account_pool, config: DetectorRunner(account_pool, Params(), turn_max_points=50,
                                                        deferrals=deferrals),
            label="detector-scheduler", debounce_s=0, sweep_s=60,
        )
        for _ in range(10):
            await worker.run_turn()
            if worker._continuation_at is None:
                break
        assert worker._continuation_at is None
        assert deferrals.devices() == ((OWNER, big),)
        async with raw.connection() as conn:
            done = await (await conn.execute(
                "SELECT tracking_device_id,last_run_at IS NOT NULL FROM detector_state ORDER BY 1")).fetchall()
            trips = dict(await (await conn.execute(
                "SELECT tracking_device_id,count(*) FROM trips GROUP BY 1")).fetchall())
        assert dict(done) == {big: False, other: True, remote: True}
        assert trips.get(big) is None and trips[other] == 1 and trips[remote] == 1
        async with first.connection() as conn:
            assert [row for row in (await _snapshot(conn))["state"] if row[0] == big] == before_big
    _run(body)


@pytest.mark.parametrize("limit", [0, 100_000])
def test_limit_zero_and_under_limit_match_unlimited_output(limit):
    async def build(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "phone")
            await _insert_track(conn, device, build_track(TRACK))
        return pool, device

    async def baseline(raw):
        pool, _device = await build(raw)
        await _drain(DetectorRunner(pool, Params()))
        async with pool.connection() as conn:
            return await _outputs(conn)

    async def limited(raw):
        pool, _device = await build(raw)
        Runner = _counting()
        outcomes = await _drain(Runner(pool, Params(), turn_max_points=limit))
        assert Runner.counts == (0 if limit == 0 else 1)
        assert outcomes[0].batch.completed == 1
        async with pool.connection() as conn:
            return await _outputs(conn)

    assert _run(limited) == _run(baseline)


def test_raising_the_limit_clears_the_deferral():
    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "big")
            await _insert_track(conn, device, build_track(TRACK))
        deferrals = DetectorDeferrals(clock=_Clock())
        await DetectorRunner(pool, Params(), turn_max_points=50, deferrals=deferrals).run_turn()
        assert deferrals.devices() == ((OWNER, device),)
        raised = await DetectorRunner(pool, Params(), turn_max_points=100_000,
                                      deferrals=deferrals).run_turn()
        assert raised.batch.completed == 1
        assert deferrals.devices() == ()
        async with pool.connection() as conn:
            assert (await _snapshot(conn))["trips"]
    _run(body)


def test_window_count_is_index_only_and_reads_no_point_payloads():
    async def body(raw):
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            target = await seed_tracking_device(conn, "target")
            neighbour = await seed_tracking_device(conn, "neighbour")
            await _bulk_points(conn, target, 20_000)
            await _bulk_points(conn, neighbour, 20_000)

        async def plan():
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "EXPLAIN (FORMAT JSON) " + WINDOW_COUNT_SQL,
                    (account_id(conn), target, runner_module.EPOCH, 1001))
                return json.dumps((await cur.fetchone())[0])

        # Fresh rows without a visibility map still never take a sequential scan.
        fresh = await plan()
        assert "Seq Scan" not in fresh and "Index" in fresh, fresh
        async with await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True) as conn:
            # The disposable database commits asynchronously; VACUUM can only
            # mark pages all-visible once the commit WAL is flushed.
            await conn.execute("CHECKPOINT")
            await conn.execute("VACUUM (ANALYZE) points")
        vacuumed = await plan()
        assert "Seq Scan" not in vacuumed, vacuumed
        assert '"Index Only Scan"' in vacuumed, vacuumed
        assert "points_account_device_recorded_at_idx" in vacuumed, vacuumed
        async with pool.connection() as conn:
            assert await DetectorRunner(pool, Params())._count_window(
                conn, target, runner_module.EPOCH, 1000) == 1001
    _run(body)


async def _deferred_device(raw):
    pool = await reset_account_db(raw)
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, "big")
        await _insert_track(conn, device, build_track(TRACK))
    return pool, device


def test_legacy_run_once_defers_without_writes_and_warns_once(caplog):
    caplog.set_level(logging.INFO, logger="app.detector.runner")

    async def body(raw):
        pool, device = await _deferred_device(raw)
        async with pool.connection() as conn:
            before = await _snapshot(conn)
        deferrals = DetectorDeferrals(clock=_Clock())
        Runner = _counting()
        for _ in range(2):
            runner = Runner(pool, Params(), turn_max_points=50, deferrals=deferrals)
            assert await runner.run_once() is True
            assert not runner.produced_work
        assert Runner.counts == 1
        assert deferrals.devices() == ((OWNER, device),)
        async with pool.connection() as conn:
            assert await _snapshot(conn) == before
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "deferring" in r.message]
        assert len(warnings) == 1
    _run(body)


def test_failed_recheck_clears_the_deferral():
    async def body(raw):
        pool, device = await _deferred_device(raw)
        clock = _Clock()
        deferrals = DetectorDeferrals(clock=clock)
        await DetectorRunner(pool, Params(), turn_max_points=50, deferrals=deferrals).run_turn()
        assert deferrals.devices() == ((OWNER, device),)

        class Failing(DetectorRunner):
            async def _run(self, conn, device):
                raise RuntimeError("synthetic failure")

        clock.now += runner_module.DEFERRAL_RECHECK_S
        failed = await Failing(pool, Params(), turn_max_points=50, deferrals=deferrals).run_turn()
        assert failed.batch.retriable_failures == 1 and failed.cursor == device
        assert deferrals.devices() == ()
    _run(body)


def test_idle_turn_prunes_a_deferral_once_the_command_cleared_the_work():
    async def body(raw):
        pool, device = await _deferred_device(raw)
        deferrals = DetectorDeferrals(clock=_Clock())
        Runner = _counting()
        await Runner(pool, Params(), turn_max_points=50, deferrals=deferrals).run_turn()
        assert deferrals.devices() == ((OWNER, device),)
        # The command's per-device step, with no new data afterwards.
        async with pool.connection() as conn:
            assert await DetectorRunner(pool, Params()).run_pending_in(conn, device) is True
        idle = await Runner(pool, Params(), turn_max_points=50, deferrals=deferrals).run_turn()
        assert not idle.ready and idle.batch.attempted == 0
        assert deferrals.devices() == ()
        assert Runner.counts == 1
    _run(body)
