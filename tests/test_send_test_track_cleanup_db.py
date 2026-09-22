"""Execute the sender's real cleanup SQL against two identically labeled streams."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg import sql, errors

from app.account_context import account_id
from app.db import make_pool
from app.tracking import create_device
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
CLEANUP_PATH = Path(__file__).resolve().parents[1] / "scripts/sql/cleanup_test_device.sql"
T0 = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)


async def _seed_device(conn, device_id: int) -> None:
    owner = account_id(conn)
    await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, distance_m) "
        "VALUES (%s, %s, 'test', 'detected', %s, %s, 2500)",
        (owner, device_id, T0, T0 + timedelta(minutes=24)),
    )
    await conn.execute(
        "INSERT INTO stays (account_id, tracking_device_id, device, started_at, ended_at, centroid, point_count) "
        "VALUES (%s, %s, 'test', %s, %s, ST_SetSRID(ST_MakePoint(-122.483, 37.7694), 4326), 11)",
        (owner, device_id, T0, T0 + timedelta(minutes=10)),
    )
    point = await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom) "
        "VALUES (%s, %s, 'test', %s, ST_SetSRID(ST_MakePoint(-122.483, 37.7694), 4326)) RETURNING id",
        (owner, device_id, T0),
    )
    await conn.execute(
        "INSERT INTO trip_boundary_overrides (account_id, tracking_device_id, device, kind, point_id) "
        "VALUES (%s, %s, 'test', 'force', %s)",
        (owner, device_id, (await point.fetchone())[0]),
    )
    await conn.execute(
        "INSERT INTO raw_messages (account_id, tracking_device_id, payload) VALUES (%s, %s, %s)",
        (owner, device_id, json.dumps({"_type": "location", "tid": "test"})),
    )


async def _counts(conn, device_id: int) -> dict[str, int]:
    result = {}
    for table in ("trips", "stays", "points", "raw_messages", "trip_boundary_overrides", "detector_state", "ingest_credentials"):
        cur = await conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE account_id = %s AND tracking_device_id = %s").format(sql.Identifier(table)),
            (account_id(conn), device_id),
        )
        result[table] = (await cur.fetchone())[0]
    return result


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            target = await create_device(conn, "test")
            other = await create_device(conn, "test")
            await _seed_device(conn, target.tracking_device_id)
            await _seed_device(conn, other.tracking_device_id)
            before = await _counts(conn, other.tracking_device_id)
        # Bind the one psql variable with psycopg's SQL literal quoting. The SQL
        # statements and transaction boundaries are the exact shipped file.
        cleanup = sql.SQL(CLEANUP_PATH.read_text().replace(":'tracking_username'", "{}"))
        with pytest.raises(errors.RaiseException, match="Expected one issued credential"):
            async with raw_pool.connection() as conn:
                await conn.execute(cleanup.format(sql.Literal("odograph_missing")), prepare=False)
        async with pool.connection() as conn:
            assert await _counts(conn, target.tracking_device_id) == before
        async with raw_pool.connection() as conn:
            await conn.execute(cleanup.format(sql.Literal(target.username)), prepare=False)
        async with pool.connection() as conn:
            assert all(value == 0 for value in (await _counts(conn, target.tracking_device_id)).values())
            assert await _counts(conn, other.tracking_device_id) == before
            cur = await conn.execute("SELECT id FROM tracking_devices ORDER BY id")
            assert await cur.fetchall() == [(other.tracking_device_id,)]
    finally:
        await raw_pool.close()


def test_cleanup_removes_only_the_explicit_owned_test_stream():
    asyncio.run(_scenario())
