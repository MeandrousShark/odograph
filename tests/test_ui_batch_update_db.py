from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import make_pool, run_migrations
from app.ui import make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
USER = {"sub": "test"}


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/batch_update":
            return route.endpoint
    raise AssertionError("batch_update route missing")


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


async def _insert_trip(
    conn, device: str, source: str, started_at: str, category: str, purpose: str,
    tag_source: str | None, vehicle_id: int,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category, "
        "purpose, tag_source, vehicle_id) VALUES (%s, %s, %s, %s::timestamptz + "
        "interval '30 minutes', 1000, %s, %s, %s, %s) RETURNING id",
        (device, source, started_at, started_at, category, purpose, tag_source, vehicle_id),
    )
    return (await cur.fetchone())[0]


async def _rows(conn, trip_ids):
    cur = await conn.execute(
        "SELECT id, category::text, purpose, tag_source::text, vehicle_id "
        "FROM trips WHERE id = ANY(%s) ORDER BY id",
        (trip_ids,),
    )
    return await cur.fetchall()


async def _update_contract_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)

        async with pool.connection() as conn:
            second_vehicle = await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Second Car') RETURNING id"
            )
            second_vehicle_id = (await second_vehicle.fetchone())[0]
            first_id = await _insert_trip(
                conn, "A", "detected", "2026-01-02T09:00:00Z", "business",
                "First purpose", "rule", 1,
            )
            manual_id = await _insert_trip(
                conn, "B", "manual", "2026-03-04T09:00:00Z", "personal",
                "Manual purpose", None, 1,
            )
            third_id = await _insert_trip(
                conn, "C", "detected", "2026-05-06T09:00:00Z", "unclassified",
                "Third purpose", None, 1,
            )

        handler = _endpoint()
        request = _request(pool)

        response = await handler(
            request, [manual_id, first_id, first_id], "keep", str(second_vehicle_id),
            "ignored purpose", False, USER,
        )
        assert json.loads(response.body) == {"updated": 2}
        async with pool.connection() as conn:
            rows = await _rows(conn, [first_id, manual_id])
        assert rows == [
            (first_id, "business", "First purpose", "rule", second_vehicle_id),
            (manual_id, "personal", "Manual purpose", None, second_vehicle_id),
        ], "vehicle-only updates must preserve category, purpose, and tag ownership"

        await handler(request, [first_id, manual_id], "keep", "", "", False, USER)
        async with pool.connection() as conn:
            rows = await _rows(conn, [first_id, manual_id])
        assert [row[4] for row in rows] == [None, None]
        assert [row[1:4] for row in rows] == [
            ("business", "First purpose", "rule"),
            ("personal", "Manual purpose", None),
        ]

        await handler(request, [first_id, manual_id], "unclassified", "keep", "", False, USER)
        async with pool.connection() as conn:
            rows = await _rows(conn, [first_id, manual_id])
        assert [row[1:4] for row in rows] == [
            ("unclassified", "First purpose", "human"),
            ("unclassified", "Manual purpose", "human"),
        ]

        await handler(
            request, [manual_id, third_id], "keep", "keep", "  Client visits  ", True, USER,
        )
        async with pool.connection() as conn:
            rows = await _rows(conn, [manual_id, third_id])
        assert [row[1:4] for row in rows] == [
            ("unclassified", "Client visits", "human"),
            ("unclassified", "Client visits", "human"),
        ]

        await handler(request, [manual_id, third_id], "keep", "keep", "   ", True, USER)
        async with pool.connection() as conn:
            rows = await _rows(conn, [manual_id, third_id])
        assert [row[2] for row in rows] == [None, None]
        assert [row[3] for row in rows] == ["human", "human"]
    finally:
        await pool.close()


def test_batch_update_tristates_tag_ownership_and_unrestricted_selection():
    asyncio.run(_update_contract_scenario())


async def _validation_and_rollback_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)

        async with pool.connection() as conn:
            first_id = await _insert_trip(
                conn, "A", "detected", "2026-01-02T09:00:00Z", "business",
                "First purpose", "rule", 1,
            )
            second_id = await _insert_trip(
                conn, "B", "manual", "2026-03-04T09:00:00Z", "personal",
                "Second purpose", None, 1,
            )
            before = await _rows(conn, [first_id, second_id])

        handler = _endpoint()
        request = _request(pool)
        cases = [
            (([first_id, second_id], "keep", "keep", "", False, USER), "Nothing to apply"),
            (([first_id, second_id], "bogus", "keep", "", False, USER), "Unknown category"),
            (([first_id, second_id], "keep", "bogus", "", False, USER), "Invalid vehicle"),
            (([], "business", "keep", "", False, USER), "at least one"),
        ]
        for args, message in cases:
            with pytest.raises(HTTPException, match=message) as exc:
                await handler(request, *args)
            assert exc.value.status_code == 400

        with pytest.raises(HTTPException, match="no longer exist") as exc:
            await handler(
                request, [first_id, 999999], "unclassified", "keep", "", False, USER,
            )
        assert exc.value.status_code == 400
        async with pool.connection() as conn:
            assert await _rows(conn, [first_id, second_id]) == before

        with pytest.raises(HTTPException, match="No such vehicle") as exc:
            await handler(
                request, [first_id, second_id], "keep", "999999", "", False, USER,
            )
        assert exc.value.status_code == 400
        async with pool.connection() as conn:
            assert await _rows(conn, [first_id, second_id]) == before
    finally:
        await pool.close()


def test_batch_update_validation_and_atomic_400_paths():
    asyncio.run(_validation_and_rollback_scenario())


async def _single_trip_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await run_migrations(pool)

        async with pool.connection() as conn:
            trip_id = await _insert_trip(
                conn, "A", "detected", "2026-01-02T09:00:00Z", "unclassified",
                "Original purpose", None, 1,
            )

        handler = _endpoint()
        request = _request(pool)

        response = await handler(request, [trip_id], "business", "keep", "", False, USER)
        assert json.loads(response.body) == {"updated": 1}
        async with pool.connection() as conn:
            rows = await _rows(conn, [trip_id])
        assert rows == [(trip_id, "business", "Original purpose", "human", 1)], (
            "a single-trip batch update must change only the requested field"
        )
    finally:
        await pool.close()


def test_batch_update_accepts_a_single_selected_trip():
    asyncio.run(_single_trip_scenario())
