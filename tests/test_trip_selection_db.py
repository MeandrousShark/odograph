"""DB-backed coverage for the complete archive selection snapshot."""
from __future__ import annotations

import asyncio
import json
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

TZ = ZoneInfo("America/Los_Angeles")


def _endpoint(path: str, method: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} missing")


TRIP_SELECTION = _endpoint("/trips/selection", "GET")
BATCH_UPDATE = _endpoint("/trips/batch_update", "POST")


def _request(pool, page_size=2):
    config = SimpleNamespace(display_tz=TZ, trips_page_size=page_size, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    )


async def _insert_trip(
    conn,
    started_at: datetime,
    *,
    category: str = "unclassified",
    vehicle_id: int | None = None,
    notes: str | None = None,
    exclusion: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category, "
        "vehicle_id, notes, exclusion) VALUES ('SEL', 'manual', %s, %s, 1000, %s, %s, %s, %s) "
        "RETURNING id",
        (
            started_at, started_at + timedelta(minutes=10), category, vehicle_id,
            notes, exclusion,
        ),
    )
    return (await cur.fetchone())[0]


async def _selection_response(pool, **filters):
    return await TRIP_SELECTION(
        _request(pool), {"sub": "test"},
        category=filters.get("category", ""),
        from_=filters.get("from_", ""),
        to=filters.get("to", ""),
        vehicle=filters.get("vehicle", ""),
        q=filters.get("q", ""),
        exclusion=filters.get("exclusion", ""),
        date_preset=filters.get("date_preset", ""),
    )


async def _selection(pool, **filters):
    response = await _selection_response(pool, **filters)
    return json.loads(response.body)


async def _filter_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        jan = datetime(2026, 1, 15, 12, tzinfo=TZ)
        async with pool.connection() as conn:
            vehicle_a = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Selection A') RETURNING id"
            )).fetchone())[0]
            vehicle_b = (await (await conn.execute(
                "INSERT INTO vehicles (name) VALUES ('Selection B') RETURNING id"
            )).fetchone())[0]
            literal_id = await _insert_trip(
                conn, jan, category="business", vehicle_id=vehicle_a,
                notes="Literal 100%_ marker",
            )
            personal_id = await _insert_trip(
                conn, jan + timedelta(days=1), category="personal", vehicle_id=vehicle_b,
            )
            unassigned_id = await _insert_trip(
                conn, jan + timedelta(days=2), category="unclassified",
            )
            excluded_id = await _insert_trip(
                conn, jan + timedelta(days=3), category="business", vehicle_id=vehicle_a,
                exclusion="not_my_vehicle",
            )
            month_ids = [
                await _insert_trip(
                    conn, datetime(2026, month, 5, 12, tzinfo=TZ),
                    category="business", vehicle_id=vehicle_a,
                )
                for month in range(2, 5)
            ]

        selection_response = await _selection_response(pool)
        assert selection_response.headers["cache-control"] == "no-store"
        all_ids = json.loads(selection_response.body)
        assert all_ids["count"] == 7
        assert all_ids["trip_ids"] == [
            month_ids[2], month_ids[1], month_ids[0], excluded_id,
            unassigned_id, personal_id, literal_id,
        ]

        assert await _selection(pool, q="100%_") == {
            "trip_ids": [literal_id], "count": 1,
        }
        assert await _selection(pool, category="business") == {
            "trip_ids": [month_ids[2], month_ids[1], month_ids[0], excluded_id, literal_id],
            "count": 5,
        }
        assert await _selection(pool, from_="2026-01-16", to="2026-01-16") == {
            "trip_ids": [personal_id], "count": 1,
        }
        assert await _selection(pool, vehicle="none") == {
            "trip_ids": [unassigned_id], "count": 1,
        }
        assert await _selection(pool, vehicle=str(vehicle_b)) == {
            "trip_ids": [personal_id], "count": 1,
        }
        assert await _selection(pool, exclusion="not_my_vehicle") == {
            "trip_ids": [excluded_id], "count": 1,
        }
        assert await _selection(pool, exclusion="none") == {
            "trip_ids": [month_ids[2], month_ids[1], month_ids[0], unassigned_id, personal_id, literal_id],
            "count": 6,
        }
        assert await _selection(
            pool, category="business", from_="2026-02-01", to="2026-04-30",
            vehicle=str(vehicle_a), exclusion="none",
        ) == {
            "trip_ids": [month_ids[2], month_ids[1], month_ids[0]], "count": 3,
        }
        assert await _selection(pool, q="does not exist") == {
            "trip_ids": [], "count": 0,
        }

        # Archive date and vehicle parsing is intentionally forgiving. A bad
        # value means that filter is open, just as it does for GET /trips.
        assert (await _selection(
            pool, from_="not-a-date", to="also-not-a-date", vehicle="not-a-vehicle",
        ))["count"] == 7
    finally:
        await pool.close()


def test_trip_selection_returns_complete_filtered_snapshot():
    asyncio.run(_filter_scenario())


async def _preset_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        now = datetime.now(TZ)
        current_start = datetime(now.year, now.month, 1, 12, tzinfo=TZ)
        if now.month == 1:
            previous_start = datetime(now.year - 1, 12, 1, 12, tzinfo=TZ)
        else:
            previous_start = datetime(now.year, now.month - 1, 1, 12, tzinfo=TZ)
        async with pool.connection() as conn:
            current_id = await _insert_trip(conn, current_start + timedelta(days=1))
            previous_id = await _insert_trip(conn, previous_start + timedelta(days=1))
            older_id = await _insert_trip(conn, current_start - timedelta(days=400))

        assert await _selection(pool, date_preset="this_month") == {
            "trip_ids": [current_id], "count": 1,
        }
        assert await _selection(pool, date_preset="last_month") == {
            "trip_ids": [previous_id], "count": 1,
        }
        year_ids = {current_id}
        if previous_start.year == current_start.year:
            year_ids.add(previous_id)
        year_result = await _selection(pool, date_preset="this_year")
        assert set(year_result["trip_ids"]) == year_ids
        assert year_result["count"] == len(year_ids)
        all_result = await _selection(pool, date_preset="all")
        assert set(all_result["trip_ids"]) == {current_id, previous_id, older_id}
        custom_result = await _selection(
            pool,
            date_preset="custom",
            from_=current_start.date().isoformat(),
            to=(current_start + timedelta(days=2)).date().isoformat(),
        )
        assert custom_result == {"trip_ids": [current_id], "count": 1}
    finally:
        await pool.close()


def test_trip_selection_resolves_archive_date_presets():
    asyncio.run(_preset_scenario())


async def _snapshot_mutation_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        start = datetime(2026, 6, 1, 12, tzinfo=TZ)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, start, category="business")
            second_id = await _insert_trip(
                conn, start + timedelta(days=1), category="business",
            )
            new_id = None

        selected = await _selection(pool, category="business")
        assert set(selected["trip_ids"]) == {first_id, second_id}

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE trips SET category = 'personal' WHERE id = %s", (first_id,)
            )
            new_id = await _insert_trip(
                conn, start + timedelta(days=2), category="business",
            )

        response = await BATCH_UPDATE(
            _request(pool), selected["trip_ids"], "unclassified", "keep", "", False,
            {"sub": "test"},
        )
        assert json.loads(response.body) == {"updated": 2}
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, category::text FROM trips WHERE id = ANY(%s) ORDER BY id",
                ([first_id, second_id, new_id],),
            )
            assert await cur.fetchall() == [
                (first_id, "unclassified"),
                (second_id, "unclassified"),
                (new_id, "business"),
            ]
    finally:
        await pool.close()


def test_snapshot_ids_remain_explicit_when_filters_and_rows_change():
    asyncio.run(_snapshot_mutation_scenario())
