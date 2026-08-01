"""DB-backed tests for the `/review` triage flow: oldest-first
ordering, from/to/vehicle filters (shared with trips_archive() via
`_trip_filter_sql` so they can't drift), the Skip cursor's row-value
comparison and its vanished-trip fallback, tag-and-advance's single round
trip, and the empty/done state split.

Route handlers are called directly (bypassing FastAPI's dependency
injection, same pattern as tests/test_ui_merge_db.py) with a real
Jinja2Templates instance so `TemplateResponse.context` can be asserted on
without needing a running app or HTTP client.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import make_pool, run_migrations
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.main import make_templates
from app.ui import make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = timezone.utc
BASE = datetime(2026, 1, 1, 9, tzinfo=TZ)


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} missing")


REVIEW_PAGE = _endpoint("/review")
REVIEW_CARD = _endpoint("/review/card")
REVIEW_TAG = _endpoint("/review/{trip_id}/tag")
REVIEW_SKIP = _endpoint("/review/{trip_id}/skip")
REVIEW_DELETE = _endpoint("/review/{trip_id}/delete")


class FakeSnapWorker:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


def _request(pool):
    templates = make_templates(SimpleNamespace(display_tz=TZ))
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=templates, config=SimpleNamespace(display_tz=TZ),
            detector_runner=DetectorRunner(pool, Params()),
            snap_worker=FakeSnapWorker(),
        )),
        session={"csrf": "test-csrf"},
    )


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_trip(
    conn, started_at: datetime, category: str = "unclassified", vehicle_id: int | None = None,
    source: str = "manual",
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, category, vehicle_id) "
        "VALUES ('phone', %s, %s, %s, 1000, %s, %s) RETURNING id",
        (source, started_at, started_at + timedelta(minutes=15), category, vehicle_id),
    )
    return (await cur.fetchone())[0]


async def _insert_vehicle(conn, name: str) -> int:
    cur = await conn.execute("INSERT INTO vehicles (name) VALUES (%s) RETURNING id", (name,))
    return (await cur.fetchone())[0]


async def _ordering_and_classified_exclusion_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, BASE, category="business")
            oldest_id = await _insert_trip(conn, BASE + timedelta(hours=1))
            await _insert_trip(conn, BASE + timedelta(hours=2))

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "")
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == oldest_id
        assert response.context["remaining"] == 2
    finally:
        await pool.close()


def test_review_page_picks_oldest_unclassified_and_excludes_classified():
    asyncio.run(_ordering_and_classified_exclusion_scenario())


async def _date_filter_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, BASE)
            in_range_id = await _insert_trip(conn, BASE + timedelta(days=31))
            await _insert_trip(conn, BASE + timedelta(days=62))

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "2026-02-01", "2026-02-28", "")
        assert response.context["trip"]["id"] == in_range_id
        assert response.context["remaining"] == 1
    finally:
        await pool.close()


def test_review_page_respects_date_filters():
    asyncio.run(_date_filter_scenario())


async def _vehicle_filter_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _insert_vehicle(conn, "Truck")
            sedan_id = await _insert_vehicle(conn, "Sedan")
            # Earlier overall, but the wrong vehicle -- must not win.
            await _insert_trip(conn, BASE, vehicle_id=sedan_id)
            truck_trip_id = await _insert_trip(conn, BASE + timedelta(hours=1), vehicle_id=truck_id)

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", str(truck_id))
        assert response.context["trip"]["id"] == truck_trip_id
        assert response.context["remaining"] == 1
    finally:
        await pool.close()


def test_review_page_respects_vehicle_filter():
    asyncio.run(_vehicle_filter_scenario())


async def _unassigned_vehicle_filter_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _insert_vehicle(conn, "Truck")
            # Earlier overall, but assigned -- must not win under vehicle=none.
            await _insert_trip(conn, BASE, vehicle_id=truck_id)
            unassigned_trip_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "none")
        assert response.context["trip"]["id"] == unassigned_trip_id
        assert response.context["remaining"] == 1
    finally:
        await pool.close()


def test_review_page_respects_unassigned_vehicle_filter():
    asyncio.run(_unassigned_vehicle_filter_scenario())


async def _tag_and_advance_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, BASE)
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_TAG(
            request, first_id, "business", "  Client meeting  ", "", "", "", {"sub": "test"}
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == second_id
        assert response.context["remaining"] == 1

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose, tag_source::text FROM trips WHERE id = %s", (first_id,)
            )
            assert await cur.fetchone() == ("business", "Client meeting", "human")

        # Tagging the last remaining trip exhausts the pass -- "done", not
        # "empty" (a fresh /review load would still find nothing today, but
        # that's a coincidence of this scenario, not what "done" means).
        response = await REVIEW_TAG(
            request, second_id, "personal", "", "", "", "", {"sub": "test"}
        )
        assert response.context["state"] == "done"
        assert response.context["remaining"] == 0
    finally:
        await pool.close()


def test_review_tag_advances_to_next_card_and_sets_human_tag_source():
    asyncio.run(_tag_and_advance_scenario())


async def _invalid_category_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)

        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await REVIEW_TAG(
                request, trip_id, "unclassified", "", "", "", "", {"sub": "test"}
            )
        assert exc_info.value.status_code == 400

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT category::text FROM trips WHERE id = %s", (trip_id,))
            assert (await cur.fetchone())[0] == "unclassified"
    finally:
        await pool.close()


def test_review_tag_rejects_category_outside_business_or_personal():
    asyncio.run(_invalid_category_scenario())


async def _skip_saves_purpose_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, BASE)
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1))
        response = await REVIEW_SKIP(
            _request(pool), first_id, "  Deliver records  ", "", "", "", {"sub": "test"}
        )
        assert response.context["trip"]["id"] == second_id
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose FROM trips WHERE id = %s", (first_id,)
            )
            assert await cur.fetchone() == ("unclassified", "Deliver records")
    finally:
        await pool.close()


def test_review_skip_saves_current_purpose_before_advancing():
    asyncio.run(_skip_saves_purpose_scenario())


async def _skip_cursor_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            # Same started_at: the row-value comparison's stable tiebreak
            # must fall back to id.
            tied_lower_id = await _insert_trip(conn, BASE)
            tied_higher_id = await _insert_trip(conn, BASE)
            third_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_CARD(request, {"sub": "test"}, tied_lower_id, "", "", "")
        assert response.context["trip"]["id"] == tied_higher_id
        assert response.context["remaining"] == 2

        # Skip again past the tie, landing on the strictly-later trip.
        response = await REVIEW_CARD(request, {"sub": "test"}, tied_higher_id, "", "", "")
        assert response.context["trip"]["id"] == third_id
        assert response.context["remaining"] == 1

        # Exhausting the cursor is "done", not "empty".
        response = await REVIEW_CARD(request, {"sub": "test"}, third_id, "", "", "")
        assert response.context["state"] == "done"
        assert response.context["remaining"] == 0

        # A vanished `after` trip (deleted mid-pass) falls back to no
        # cursor -- a fresh pass from the oldest surviving trip -- rather
        # than 404ing.
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM trips WHERE id = %s", (tied_lower_id,))
        response = await REVIEW_CARD(request, {"sub": "test"}, tied_lower_id, "", "", "")
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == tied_higher_id
        assert response.context["remaining"] == 2
    finally:
        await pool.close()


def test_review_card_skip_cursor_ties_break_on_id_and_vanished_after_resets_pass():
    asyncio.run(_skip_cursor_scenario())


async def _empty_state_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, BASE, category="business")

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "")
        assert response.context["state"] == "empty"
        assert response.context["trip"] is None
        assert response.context["remaining"] == 0
    finally:
        await pool.close()


def test_review_page_shows_empty_state_when_nothing_matches():
    asyncio.run(_empty_state_scenario())


async def _delete_and_advance_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            included_vehicle = await _insert_vehicle(conn, "Included")
            excluded_vehicle = await _insert_vehicle(conn, "Excluded")
            manual_id = await _insert_trip(conn, BASE, vehicle_id=included_vehicle)
            detected_id = await _insert_trip(
                conn, BASE, vehicle_id=included_vehicle, source="detected"
            )
            await _insert_trip(
                conn, BASE + timedelta(minutes=30), vehicle_id=excluded_vehicle
            )
            last_id = await _insert_trip(
                conn, BASE + timedelta(hours=1), vehicle_id=included_vehicle
            )
            await _insert_trip(
                conn, BASE + timedelta(days=40), vehicle_id=included_vehicle
            )

        request = _request(pool)
        from_str, to_str, vehicle_str = "2026-01-01", "2026-01-31", str(included_vehicle)

        response = await REVIEW_DELETE(
            request, manual_id, from_str, to_str, vehicle_str, {"sub": "test"}
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == detected_id
        assert response.context["remaining"] == 2
        assert response.context["filter_from"] == from_str
        assert response.context["filter_to"] == to_str
        assert response.context["filter_vehicle"] == vehicle_str
        assert request.app.state.snap_worker.pokes == 0

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM trips WHERE id = %s", (manual_id,))
            assert await cur.fetchone() is None

        response = await REVIEW_DELETE(
            request, detected_id, from_str, to_str, vehicle_str, {"sub": "test"}
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == last_id
        assert response.context["remaining"] == 1
        assert request.app.state.snap_worker.pokes == 0

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT range_start, range_end FROM trip_boundary_overrides "
                "WHERE kind::text = 'discard' AND device = 'phone'"
            )
            assert await cur.fetchone() == (BASE, BASE + timedelta(minutes=15))

        response = await REVIEW_DELETE(
            request, last_id, from_str, to_str, vehicle_str, {"sub": "test"}
        )
        assert response.context["state"] == "done"
        assert response.context["trip"] is None
        assert response.context["remaining"] == 0
        assert response.context["review_url"] == (
            f"/review?from={from_str}&to={to_str}&vehicle={vehicle_str}"
        )
    finally:
        await pool.close()


def test_review_delete_manual_and_detected_advances_with_cursor_and_filters_to_done():
    asyncio.run(_delete_and_advance_scenario())
