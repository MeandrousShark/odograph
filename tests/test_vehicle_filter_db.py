"""DB-backed tests for the vehicle filter's `none` ("Unassigned") sentinel,
threaded through `_parse_vehicle_id`/`_trip_filter_sql` and shared by
`trips_archive()` and `export_trips()`. `/review`'s own copy of this
behavior is covered by tests/test_review_db.py, right next to its other
vehicle-filter tests. Route handlers are called directly (bypassing
FastAPI's dependency injection), same pattern as
tests/test_missing_trip_index_db.py.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("UTC")
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=TZ)


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} missing")


TRIPS_ARCHIVE = _endpoint("/trips", "GET")
EXPORT_TRIPS = _endpoint("/export", "GET")


def _request(pool):
    config = SimpleNamespace(display_tz=TZ, trips_page_size=25, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    )


async def _insert_trip(conn, started_at: datetime, notes: str, vehicle_id: int | None = None) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, notes, vehicle_id) "
        "VALUES ('FLT', 'manual', %s, %s, 1000, %s, %s) RETURNING id",
        (started_at, started_at + timedelta(minutes=10), notes, vehicle_id),
    )
    return (await cur.fetchone())[0]


async def _scenario(check):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Car') RETURNING id"
            )).fetchone())[0]
            assigned_id = await _insert_trip(conn, T0, "assigned trip", vehicle_id=vehicle_id)
            unassigned_id = await _insert_trip(conn, T0 + timedelta(hours=1), "unassigned trip")
        await check(pool, assigned_id, unassigned_id)
    finally:
        await pool.close()


def test_trip_list_unassigned_filter_returns_only_null_vehicle_trips():
    async def check(pool, assigned_id, unassigned_id):
        response = await TRIPS_ARCHIVE(
            _request(pool), {"sub": "test"}, "", "", "", "none", "", "", "", "",
            manual_open="", q="",
        )
        trip_ids = {
            trip["id"] for month in response.context["months"] for trip in month["trips"]
        }
        assert trip_ids == {unassigned_id}

    asyncio.run(_scenario(check))


def test_export_unassigned_filter_returns_only_null_vehicle_trips():
    async def check(pool, assigned_id, unassigned_id):
        response = await EXPORT_TRIPS(
            _request(pool), {"sub": "test"}, "csv", "", "", "", "none", "",
        )
        rows = list(csv.reader(io.StringIO(response.body.decode("utf-8"))))
        notes_column = rows[0].index("Notes")
        data_rows = rows[1:]
        assert [row[notes_column] for row in data_rows] == ["unassigned trip"]

    asyncio.run(_scenario(check))
