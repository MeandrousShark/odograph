"""Trailing arrival stays through the runner: provisional trips, rejoins and tags."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta

import pytest

from app.account_context import account_id
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from conftest import reset_account_db, seed_tracking_device
from tests.synth import Drive, Stationary, build_track
from tests.test_detector_turn_limit_db import _run

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")

HEAD = [Stationary(1200), Drive(km=5.0), Stationary(120)]


def _head():
    pts = build_track(HEAD)
    return pts[:-1] + [replace(pts[-1], trigger="v")]


def _continue(head, segments):
    return build_track(segments, start=(head[-1].lat, head[-1].lon), t0=head[-1].t, seed=7)[1:]


async def _insert(conn, device, points):
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,accuracy_m,"
            "velocity_kmh,trigger) "
            "VALUES(%s,%s,'phone',%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s,%s)",
            [(account_id(conn), device, p.t, p.lon, p.lat, p.accuracy_m, p.velocity_kmh, p.trigger)
             for p in points])


async def _settle(conn, device):
    """Later points are dirty on their own, as after real elapsed time."""
    await conn.execute(
        "UPDATE detector_state SET last_run_at=now() WHERE account_id=%s AND tracking_device_id=%s",
        (account_id(conn), device))


async def _place(conn, name, kind, point):
    await conn.execute(
        "INSERT INTO places(account_id,name,kind,geom,radius_m) "
        "VALUES(%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,100)",
        (account_id(conn), name, kind, point.lon, point.lat))


async def _trips(conn):
    return await (await conn.execute(
        "SELECT id,started_at,ended_at,round(distance_m),category::text,tag_source::text "
        "FROM trips WHERE account_id=%s AND source='detected' ORDER BY started_at",
        (account_id(conn),))).fetchall()


async def _trip_points(conn, trip_id):
    return (await (await conn.execute(
        "SELECT count(*) FROM points WHERE account_id=%s AND trip_id=%s",
        (account_id(conn), trip_id))).fetchone())[0]


async def _stays(conn):
    return await (await conn.execute(
        "SELECT started_at,ended_at FROM stays WHERE account_id=%s ORDER BY started_at",
        (account_id(conn),))).fetchall()


async def _provisional_trip(raw):
    """Home, a 5 km drive and two minutes at work ending in a visit fix."""
    pool = await reset_account_db(raw)
    head = _head()
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, "phone")
        await _place(conn, "Home", "home", head[0])
        await _place(conn, "Work", "work", head[-1])
        await _insert(conn, device, head)
    assert await DetectorRunner(pool, Params()).run_once()
    async with pool.connection() as conn:
        await _settle(conn, device)
    return pool, device, head


def test_arrival_fix_creates_the_trip_without_persisting_its_stay():
    async def body(raw):
        pool, _device, head = await _provisional_trip(raw)
        async with pool.connection() as conn:
            trips = await _trips(conn)
            stays = await _stays(conn)
        assert len(trips) == 1
        _id, _start, ended, distance, category, tag_source = trips[0]
        assert abs(ended - (head[0].t + timedelta(seconds=1200 + 360))) <= timedelta(seconds=30)
        assert abs(distance - 5000) < 250
        assert (category, tag_source) == ("personal", "rule")
        assert len(stays) == 1 and stays[0][1] < ended
    _run(body)


@pytest.mark.parametrize("human", [True, False])
def test_driving_on_rejoins_the_trip_in_place(human):
    """Without the origin stay in the next window, the rest of the drive
    would be lost; the rejoined trip keeps its id and any human tag, while a
    rule tag follows the new end place."""
    async def body(raw):
        pool, device, head = await _provisional_trip(raw)
        async with pool.connection() as conn:
            [(trip_id, *_)] = await _trips(conn)
            if human:
                await conn.execute(
                    "UPDATE trips SET category='business', tag_source='human' WHERE id=%s", (trip_id,))
            await _insert(conn, device, _continue(head, [Drive(km=3.0), Stationary(1800)]))
        assert await DetectorRunner(pool, Params()).run_once()
        async with pool.connection() as conn:
            trips = await _trips(conn)
            stays = await _stays(conn)
            unassigned = (await (await conn.execute(
                "SELECT count(*) FROM points p WHERE p.account_id=%s AND p.trip_id IS NULL "
                "AND p.recorded_at > %s AND p.recorded_at < %s",
                (account_id(conn), trips[0][1], trips[0][2]))).fetchone())[0]
        assert len(trips) == 1
        rejoined_id, _start, _end, distance, category, tag_source = trips[0]
        assert rejoined_id == trip_id
        assert abs(distance - 8000) < 400
        assert unassigned == 0
        assert len(stays) == 2
        if human:
            assert (category, tag_source) == ("business", "human")
        else:
            assert (category, tag_source) == ("unclassified", None)
    _run(body)


def test_a_run_mid_drive_keeps_the_provisional_trip_for_the_rejoin():
    """The debounce fires while still driving on from the stop: the open drive
    has no destination yet, and the provisional trip must survive it."""
    async def body(raw):
        pool, device, head = await _provisional_trip(raw)
        rest = _continue(head, [Drive(km=3.0), Stationary(1800)])
        split = len(rest) // 4
        async with pool.connection() as conn:
            [(trip_id, *_)] = await _trips(conn)
            await conn.execute(
                "UPDATE trips SET category='business', tag_source='human' WHERE id=%s", (trip_id,))
            await _insert(conn, device, rest[:split])
            points_before = await _trip_points(conn, trip_id)
        assert await DetectorRunner(pool, Params()).run_once()
        async with pool.connection() as conn:
            mid = await _trips(conn)
            assert await _trip_points(conn, trip_id) == points_before
            await _settle(conn, device)
        # A later transaction, so the new points' received_at passes now().
        async with pool.connection() as conn:
            await _insert(conn, device, rest[split:])
        assert await DetectorRunner(pool, Params()).run_once()
        async with pool.connection() as conn:
            trips = await _trips(conn)
        assert [t[0] for t in mid] == [trip_id]
        assert len(trips) == 1
        rejoined_id, _start, _end, distance, category, tag_source = trips[0]
        assert rejoined_id == trip_id
        assert abs(distance - 8000) < 400
        assert (category, tag_source) == ("business", "human")
    _run(body)


def test_next_fix_an_hour_later_confirms_the_trip_unchanged():
    async def body(raw):
        pool, device, head = await _provisional_trip(raw)
        async with pool.connection() as conn:
            before = await _trips(conn)
            await conn.execute("UPDATE trips SET snap_status='ok' WHERE id=%s", (before[0][0],))
            await _insert(conn, device, [replace(head[-1], t=head[-1].t + timedelta(hours=1), trigger=None)])
        assert await DetectorRunner(pool, Params()).run_once()
        async with pool.connection() as conn:
            after = await _trips(conn)
            stays = await _stays(conn)
            snap = (await (await conn.execute(
                "SELECT snap_status::text FROM trips WHERE id=%s", (after[0][0],))).fetchone())[0]
        assert after == before
        assert snap == "ok"  # unchanged inputs keep the snap
        assert len(stays) == 2 and stays[1][0] == after[0][2]
    _run(body)


def test_manual_trips_are_untouched():
    async def body(raw):
        pool = await reset_account_db(raw)
        head = _head()
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "phone")
            manual = (await (await conn.execute(
                "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,has_gap) "
                "VALUES(%s,'manual','manual',%s,%s,1000,false) RETURNING id",
                (account_id(conn), head[-1].t - timedelta(minutes=5), head[-1].t))).fetchone())[0]
            await _insert(conn, device, head)
        assert await DetectorRunner(pool, Params()).run_once()
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT source::text,started_at,ended_at,distance_m FROM trips WHERE id=%s",
                (manual,))).fetchone()
            detected = await _trips(conn)
        assert row == ("manual", head[-1].t - timedelta(minutes=5), head[-1].t, 1000)
        assert len(detected) == 1
    _run(body)
