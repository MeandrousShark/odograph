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
from tests.synth import T0

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
