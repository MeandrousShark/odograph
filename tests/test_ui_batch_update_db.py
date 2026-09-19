from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from psycopg.errors import RaiseException

from app.db import make_pool
from app.account_context import account_id
from personal_support import fixture_device, personal_request
from app.ui import make_router
from conftest import reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")
USER = {"sub": "test"}


def _endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/batch_update":
            return route.endpoint
    raise AssertionError("batch_update route missing")


def _delete_endpoint():
    for route in make_router().routes:
        if getattr(route, "path", None) == "/trips/batch_delete":
            return route.endpoint
    raise AssertionError("batch_delete route missing")


def _request(pool):
    return personal_request(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool))))


async def _insert_trip(
    conn, device: str, source: str, started_at: str, category: str, purpose: str,
    tag_source: str | None, vehicle_id: int,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, "
        "distance_m, category, purpose, tag_source, vehicle_id) VALUES (%s, %s, %s, %s, %s, "
        "%s::timestamptz + interval '30 minutes', 1000, %s, %s, %s, %s) RETURNING id",
        (
            account_id(conn),
            await fixture_device(conn, device),
            device,
            source,
            started_at,
            started_at,
            category,
            purpose,
            tag_source,
            vehicle_id,
        ),
    )
    return (await cur.fetchone())[0]


async def _rows(conn, trip_ids):
    cur = await conn.execute(
        "SELECT id, category::text, purpose, tag_source::text, vehicle_id "
        "FROM trips WHERE id = ANY(%s) ORDER BY id",
        (trip_ids,),
    )
    return await cur.fetchall()


async def _exclusions(conn, trip_ids):
    cur = await conn.execute(
        "SELECT id, exclusion::text FROM trips WHERE id = ANY(%s) ORDER BY id",
        (trip_ids,),
    )
    return await cur.fetchall()


async def _update_contract_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)

        async with pool.connection() as conn:
            second_vehicle = await conn.execute(
                "INSERT INTO vehicles (account_id, name) VALUES (%s, 'Second Car') RETURNING id", (account_id(conn),)
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
            await conn.execute(
                "UPDATE trips SET exclusion = 'not_my_vehicle' WHERE id = %s", (first_id,)
            )
            await conn.execute(
                "UPDATE trips SET exclusion = 'not_deductible' WHERE id = %s", (manual_id,)
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
            exclusions = await _exclusions(conn, [first_id, manual_id])
        assert rows == [
            (first_id, "business", "First purpose", "rule", second_vehicle_id),
            (manual_id, "personal", "Manual purpose", None, second_vehicle_id),
        ], "vehicle-only updates must preserve category, purpose, and tag ownership"
        assert exclusions == [
            (first_id, "not_my_vehicle"), (manual_id, "not_deductible")
        ], "vehicle-only updates must preserve exclusion"

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
            assert await _exclusions(conn, [first_id, manual_id]) == exclusions
        assert [row[1:4] for row in rows] == [
            ("unclassified", "First purpose", "human"),
            ("unclassified", "Manual purpose", "human"),
        ]

        await handler(
            request, [first_id, manual_id], "keep", "keep", "", False, USER,
            exclusion="",
        )
        async with pool.connection() as conn:
            assert await _exclusions(conn, [first_id, manual_id]) == [
                (first_id, None), (manual_id, None)
            ]

        await handler(
            request, [first_id, manual_id], "keep", "keep", "", False, USER,
            exclusion="not_deductible",
        )
        async with pool.connection() as conn:
            assert await _exclusions(conn, [first_id, manual_id]) == [
                (first_id, "not_deductible"), (manual_id, "not_deductible")
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
        await raw_pool.close()


def test_batch_update_tristates_tag_ownership_and_unrestricted_selection():
    asyncio.run(_update_contract_scenario())


async def _validation_and_rollback_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)

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

        with pytest.raises(HTTPException, match="Unknown exclusion") as exc:
            await handler(
                request, [first_id, second_id], "keep", "keep", "", False, USER,
                exclusion="bogus",
            )
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
        await raw_pool.close()


def test_batch_update_validation_and_atomic_400_paths():
    asyncio.run(_validation_and_rollback_scenario())


async def _single_trip_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)

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
        await raw_pool.close()


def test_batch_update_accepts_a_single_selected_trip():
    asyncio.run(_single_trip_scenario())


async def _batch_delete_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            detected_id = await _insert_trip(
                conn, "A", "detected", "2026-01-02T09:00:00Z", "business",
                "Detected", None, 1,
            )
            manual_id = await _insert_trip(
                conn, "B", "manual", "2026-03-04T09:00:00Z", "personal",
                "Manual", None, 1,
            )
            survivor_id = await _insert_trip(
                conn, "C", "manual", "2026-04-04T09:00:00Z", "personal",
                "Survivor", None, 1,
            )
            await conn.execute(
                "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom, "
                "trip_id) VALUES (%s, %s, 'A', '2026-01-02T09:30:00Z', "
                "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, %s)",
                (account_id(conn), await fixture_device(conn, 'A'), detected_id,),
            )
            await conn.execute(
                "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, "
                "treatment, trip_id) VALUES (%s, 1, '2026-05-04', 'fuel', 17.23, "
                "'business_use_allocated', %s)", (
                                                                                                                                                                                                  account_id(conn),
                                                                                                                                                                                                  manual_id,
                                                                                                                                                                                              ),
            )

        handler = _delete_endpoint()
        request = _request(pool)
        response = await handler(request, [manual_id, detected_id, manual_id], USER)
        assert json.loads(response.body) == {"deleted": 2}
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE id = ANY(%s)",
                ([manual_id, detected_id],),
            )
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute(
                "SELECT kind::text, range_start, range_end "
                "FROM trip_boundary_overrides WHERE kind::text = 'discard'"
            )
            assert len(await cur.fetchall()) == 1
            cur = await conn.execute("SELECT trip_id FROM expenses")
            assert (await cur.fetchone())[0] is None
            cur = await conn.execute("SELECT trip_id FROM points WHERE device = 'A'")
            assert await cur.fetchall() == [(None,)]

            rollback_detected = await _insert_trip(
                conn, "D", "detected", "2026-06-04T09:00:00Z", "business",
                "Rollback detected", None, 1,
            )
            rollback_manual = await _insert_trip(
                conn, "E", "manual", "2026-07-04T09:00:00Z", "personal",
                "Rollback manual", None, 1,
            )
            await conn.execute(
                "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom, "
                "trip_id) VALUES (%s, %s, 'D', '2026-06-04T09:30:00Z', "
                "ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography, %s)",
                (account_id(conn), await fixture_device(conn, 'D'), rollback_detected,),
            )
            await conn.execute(
                "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, "
                "treatment, trip_id) VALUES (%s, 1, '2026-06-04', 'fuel', 19.99, "
                "'business_use_allocated', %s)", (
                                                                                                                                                                                                  account_id(conn),
                                                                                                                                                                                                  rollback_detected,
                                                                                                                                                                                              ),
            )
            await conn.execute(
                "CREATE FUNCTION reject_one_batch_delete() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                f"IF OLD.id = {rollback_manual} THEN RAISE EXCEPTION 'forced bulk delete failure'; END IF; "
                "RETURN OLD; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER reject_one_batch_delete BEFORE DELETE ON trips "
                "FOR EACH ROW EXECUTE FUNCTION reject_one_batch_delete()"
            )
        try:
            with pytest.raises(RaiseException, match="forced bulk delete failure"):
                await handler(request, [rollback_detected, rollback_manual], USER)
        finally:
            async with pool.connection() as conn:
                await conn.execute("DROP TRIGGER reject_one_batch_delete ON trips")
                await conn.execute("DROP FUNCTION reject_one_batch_delete()")
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM trips WHERE id = ANY(%s)",
                ([rollback_detected, rollback_manual],),
            )
            assert (await cur.fetchone())[0] == 2
            cur = await conn.execute(
                "SELECT count(*) FROM trip_boundary_overrides "
                "WHERE kind::text = 'discard'"
            )
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute(
                "SELECT trip_id FROM points WHERE trip_id = %s", (rollback_detected,)
            )
            assert (await cur.fetchone())[0] == rollback_detected
            cur = await conn.execute(
                "SELECT trip_id FROM expenses WHERE trip_id = %s", (rollback_detected,)
            )
            assert (await cur.fetchone())[0] == rollback_detected

        with pytest.raises(HTTPException, match="no longer exist") as exc:
            await handler(request, [999999, survivor_id], USER)
        assert exc.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT purpose FROM trips WHERE id = %s", (survivor_id,))
            assert (await cur.fetchone())[0] == "Survivor"
    finally:
        await raw_pool.close()


def test_batch_delete_is_source_aware_atomic_and_detaches_related_data():
    asyncio.run(_batch_delete_scenario())
