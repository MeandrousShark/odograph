"""DB-backed pin test for scripts/send_test_track.sh's `--cleanup` deletion
logic. The four DELETE statements below are copied verbatim from the
script -- keep them byte-for-byte identical to what the script runs via
`psql`, so a schema change that breaks the script's SQL breaks this test
first.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.db import make_pool, run_migrations

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

UTC = timezone.utc
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)

# Verbatim copy of the four -c arguments scripts/send_test_track.sh passes
# to psql for --cleanup.
CLEANUP_SQL = [
    "DELETE FROM trips WHERE device = 'test';",
    "DELETE FROM stays WHERE device = 'test';",
    "DELETE FROM points WHERE device = 'test';",
    "DELETE FROM raw_messages WHERE payload->>'tid' = 'test';",
]


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _seed_device(conn, device: str) -> None:
    await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, point_count) "
        "VALUES (%s, 'detected', %s, %s, 2500, 37)",
        (device, T0, T0 + timedelta(minutes=24)),
    )
    await conn.execute(
        "INSERT INTO stays (device, started_at, ended_at, centroid, point_count) "
        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(-122.483, 37.7694), 4326)::geography, 11)",
        (device, T0, T0 + timedelta(minutes=10)),
    )
    await conn.execute(
        "INSERT INTO points (device, recorded_at, geom, accuracy_m) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(-122.483, 37.7694), 4326)::geography, 10)",
        (device, T0),
    )
    await conn.execute(
        "INSERT INTO raw_messages (payload) VALUES (%s)",
        (json.dumps({"_type": "location", "tid": device, "lat": 37.7694, "lon": -122.483, "tst": 0}),),
    )


async def _counts(conn, device: str) -> dict[str, int]:
    result = {}
    for table, where in (
        ("trips", "device = %s"),
        ("stays", "device = %s"),
        ("points", "device = %s"),
        ("raw_messages", "payload->>'tid' = %s"),
    ):
        cur = await conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", (device,))
        result[table] = (await cur.fetchone())[0]
    return result


async def _scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _seed_device(conn, "test")
            await _seed_device(conn, "phone")

        async with pool.connection() as conn:
            before_test = await _counts(conn, "test")
            before_phone = await _counts(conn, "phone")
        assert before_test == {"trips": 1, "stays": 1, "points": 1, "raw_messages": 1}
        assert before_phone == {"trips": 1, "stays": 1, "points": 1, "raw_messages": 1}

        async with pool.connection() as conn:
            for stmt in CLEANUP_SQL:
                await conn.execute(stmt)

        async with pool.connection() as conn:
            after_test = await _counts(conn, "test")
            after_phone = await _counts(conn, "phone")
        assert after_test == {"trips": 0, "stays": 0, "points": 0, "raw_messages": 0}
        # The unrelated device is completely untouched by the cleanup.
        assert after_phone == before_phone
    finally:
        await pool.close()


def test_cleanup_sql_removes_only_the_test_device():
    asyncio.run(_scenario())
