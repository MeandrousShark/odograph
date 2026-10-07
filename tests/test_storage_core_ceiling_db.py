"""Real detector reconstruction remains funded at an exact account ceiling."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import os

import pytest

from app.account_context import account_id
from app.db import make_pool
from app.detector.core import Params, Point
from app.detector.runner import DetectorRunner
from app.storage import storage_status
from tests.test_storage_envelope_db import (
    LONG_LABEL, _assert_envelope, _fixture, _force, _insert_points,
)
from tests.synth import Drive, Stationary, T0, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable TEST_DATABASE_URL")


def test_forced_core_reconstruction_preserves_human_rows_at_exact_ceiling():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool, device = await _fixture(raw)
            points = [Point(T0 + timedelta(seconds=15 * i), 45.5, -122.6 + .004 * i)
                      for i in range(37)]
            async with pool.connection() as conn:
                owner = account_id(conn)
                ids = await _insert_points(conn, device, points, LONG_LABEL)
                await _force(conn, device, ids, LONG_LABEL)
                for source, imported in (("manual", False), ("detected", True)):
                    await conn.execute(
                        "INSERT INTO trips(account_id,tracking_device_id,device,source,imported,"
                        "started_at,ended_at,distance_m,purpose,notes) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,1000,'human purpose','human notes')",
                        (owner, device, LONG_LABEL, source, imported, points[0].t, points[-1].t),
                    )
                personal = await (await conn.execute(
                    "SELECT id,source::text,imported,purpose,notes FROM trips ORDER BY id"
                )).fetchall()
                before = await storage_status(conn)
            async with raw.connection() as conn:
                await conn.execute("UPDATE storage_grants SET account_limit_bytes=%s,"
                                   "raw_limit_bytes=1,enhancement_limit_bytes=1 WHERE account_id=%s",
                                   (before["total_bytes"], owner))
            params = replace(Params(), min_trip_distance_m=0)
            assert await DetectorRunner(pool, params).run_once()
            await DetectorRunner(pool, params).reprocess_device_now(device)
            async with pool.connection() as conn:
                envelope = await _assert_envelope(conn, device, 400)
                assert envelope[:3] == (37, 37, 36)
                assert (await storage_status(conn))["total_bytes"] == before["total_bytes"]
                assert await (await conn.execute(
                    "SELECT id,source::text,imported,purpose,notes FROM trips "
                    "WHERE source='manual' OR imported ORDER BY id"
                )).fetchall() == personal
            async with raw.connection() as conn:
                assert (await (await conn.execute("SELECT public.storage_usage_consistent()"))
                        .fetchone())[0]
        finally:
            await raw.close()

    asyncio.run(scenario())


def test_restarted_dirty_suffix_and_full_reprocess_fit_exact_account_ceiling():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool, device = await _fixture(raw)
            first = build_track([Stationary(600), Drive(2), Stationary(600),
                                 Drive(2), Stationary(600)])
            async with pool.connection() as conn:
                await _insert_points(conn, device, first, LONG_LABEL)
            assert await DetectorRunner(pool, Params()).run_once()

            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                first_trip = tuple(await (await conn.execute(
                    "SELECT id,started_at,ended_at,ST_AsEWKB(path) FROM trips "
                    "WHERE source='detected' ORDER BY started_at LIMIT 1"
                )).fetchone())
                human_notes = LONG_LABEL * 10
                await conn.execute(
                    "UPDATE trips SET purpose='client visit',notes=%s,category='business',"
                    "tag_source='human' WHERE id=%s", (human_notes, first_trip[0]),
                )
                for source, imported in (("manual", False), ("detected", True)):
                    await conn.execute(
                        "INSERT INTO trips(account_id,tracking_device_id,device,source,imported,"
                        "started_at,ended_at,distance_m,purpose,notes) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,1000,'preserved purpose','preserved notes')",
                        (account_id(conn), device, LONG_LABEL * 3, source, imported,
                         first[0].t, first[-1].t),
                    )
                personal_before = await (await conn.execute(
                    "SELECT id,source::text,imported,device,purpose,notes FROM trips "
                    "WHERE source='manual' OR imported ORDER BY id"
                )).fetchall()
                await conn.execute(
                    "UPDATE points SET received_at='2020-01-01' WHERE tracking_device_id=%s",
                    (device,),
                )

            suffix = build_track([Drive(3), Stationary(600)],
                                 start=(first[-1].lat, first[-1].lon),
                                 t0=first[-1].t)[1:]
            async with pool.connection() as conn:
                await _insert_points(conn, device, suffix, LONG_LABEL)
                owner = account_id(conn)
                pending = await (await conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM points p JOIN detector_state s "
                    "ON s.account_id=p.account_id AND s.tracking_device_id=p.tracking_device_id "
                    "WHERE p.tracking_device_id=%s AND p.received_at>"
                    "COALESCE(s.last_run_at,'-infinity'::timestamptz))",
                    (device,),
                )).fetchone()
                assert pending[0]
                before = await storage_status(conn)
                assert before["reserved_bytes"] > 0
                ceiling = before["total_bytes"]
            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE storage_grants SET account_limit_bytes=%s,raw_limit_bytes=1,"
                    "enhancement_limit_bytes=1 WHERE account_id=%s",
                    (ceiling, owner),
                )
            async with pool.connection() as conn:
                at_ceiling = await storage_status(conn)
                assert at_ceiling["total_bytes"] == ceiling
                assert at_ceiling["account_limit_bytes"] == ceiling
                assert at_ceiling["account_blocked"]

            # A fresh runner models a process restart: it resumes the persisted dirty window.
            restarted = DetectorRunner(pool, Params())
            assert await restarted.run_once()
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                after_suffix = await storage_status(conn)
                assert after_suffix["total_bytes"] <= ceiling
                assert after_suffix["account_limit_bytes"] == ceiling
                preserved = await (await conn.execute(
                    "SELECT id,started_at,ended_at,ST_AsEWKB(path) FROM trips WHERE id=%s",
                    (first_trip[0],),
                )).fetchone()
                assert tuple(preserved) == first_trip
                second_ceiling = after_suffix["total_bytes"]
            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE storage_grants SET account_limit_bytes=%s WHERE account_id=%s",
                    (second_ceiling, owner),
                )
            async with pool.connection() as conn:
                at_second_ceiling = await storage_status(conn)
                assert at_second_ceiling["total_bytes"] == second_ceiling
                assert at_second_ceiling["account_limit_bytes"] == second_ceiling
                assert at_second_ceiling["account_blocked"]

            # Full reconstruction also runs after a fresh runner is created.
            await DetectorRunner(pool, Params()).reprocess_device_now(device)
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                after_reprocess = await storage_status(conn)
                assert after_reprocess["total_bytes"] <= second_ceiling
                assert after_reprocess["account_limit_bytes"] == second_ceiling
                personal_after = await (await conn.execute(
                    "SELECT id,source::text,imported,device,purpose,notes FROM trips "
                    "WHERE source='manual' OR imported ORDER BY id"
                )).fetchall()
                assert personal_after == personal_before
                human = await (await conn.execute(
                    "SELECT purpose,notes,category::text,tag_source::text FROM trips WHERE id=%s",
                    (first_trip[0],),
                )).fetchone()
                assert tuple(human) == ("client visit", human_notes, "business", "human")
            async with raw.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT public.storage_usage_consistent()"
                )).fetchone())[0]
        finally:
            await raw.close()

    asyncio.run(scenario())
