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

from app.db import make_pool
from app.account_context import account_id
from personal_support import fixture_device, personal_request
from app.detector.core import Params
from app.detector.runner import DetectorRunner
from app.main import make_templates
from app.ui import make_router
from conftest import reset_account_db

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
REVIEW_EXCLUSION = _endpoint("/review/{trip_id}/exclusion")
REVIEW_UNDO = _endpoint("/review/{trip_id}/undo")
REVIEW_DELETE = _endpoint("/review/{trip_id}/delete")


class FakeSnapWorker:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


def _request(pool):
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=templates, config=SimpleNamespace(display_tz=TZ),
            detector_runner=DetectorRunner(pool, Params()),
            snap_worker=FakeSnapWorker(),
        )),
        session={"csrf": "test-csrf"},
    ))


async def _insert_trip(
    conn, started_at: datetime, category: str = "unclassified", vehicle_id: int | None = None,
    source: str = "manual", notes: str | None = None,
    exclusion: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, "
        "distance_m, category, vehicle_id, notes, exclusion) VALUES (%s, %s, 'phone', %s, %s, %s, "
        "1000, %s, %s, %s, %s) RETURNING id",
        (
            account_id(conn),
            await fixture_device(conn, 'phone'),
            source,
            started_at,
            started_at + timedelta(minutes=15),
            category,
            vehicle_id,
            notes,
            exclusion,
        ),
    )
    return (await cur.fetchone())[0]


async def _insert_vehicle(conn, name: str) -> int:
    cur = await conn.execute('INSERT INTO vehicles (account_id, name) VALUES (%s, %s) RETURNING id', (
                                                                                                         account_id(conn),
                                                                                                         name,
                                                                                                     ))
    return (await cur.fetchone())[0]


async def _insert_default_vehicle(conn, name: str) -> int:
    await conn.execute("UPDATE vehicles SET is_default = false WHERE is_default")
    cur = await conn.execute(
        'INSERT INTO vehicles (account_id, name, is_default) VALUES (%s, %s, true) RETURNING id', (
                                                                                                      account_id(conn),
                                                                                                      name,
                                                                                                  )
    )
    return (await cur.fetchone())[0]


async def _ordering_and_category_only_eligibility_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, BASE, category="business")
            excluded_id = await _insert_trip(
                conn, BASE + timedelta(minutes=30), exclusion="not_my_vehicle"
            )
            await _insert_trip(conn, BASE + timedelta(hours=1))
            await _insert_trip(conn, BASE + timedelta(hours=2))

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "", "")
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == excluded_id
        assert response.context["remaining"] == 3
    finally:
        await raw_pool.close()


def test_review_page_uses_category_only_eligibility_and_excludes_classified():
    asyncio.run(_ordering_and_category_only_eligibility_scenario())


async def _date_filter_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, BASE)
            in_range_id = await _insert_trip(conn, BASE + timedelta(days=31))
            await _insert_trip(conn, BASE + timedelta(days=62))

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "2026-02-01", "2026-02-28", "", "")
        assert response.context["trip"]["id"] == in_range_id
        assert response.context["remaining"] == 1
        assert response.context["review_count"] == 3
    finally:
        await raw_pool.close()


def test_review_page_respects_date_filters():
    asyncio.run(_date_filter_scenario())


async def _vehicle_filter_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            truck_id = await _insert_vehicle(conn, "Truck")
            sedan_id = await _insert_vehicle(conn, "Sedan")
            # Earlier overall, but the wrong vehicle -- must not win.
            await _insert_trip(conn, BASE, vehicle_id=sedan_id)
            truck_trip_id = await _insert_trip(conn, BASE + timedelta(hours=1), vehicle_id=truck_id)

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", str(truck_id), "")
        assert response.context["trip"]["id"] == truck_trip_id
        assert response.context["remaining"] == 1
    finally:
        await raw_pool.close()


def test_review_page_respects_vehicle_filter():
    asyncio.run(_vehicle_filter_scenario())


async def _unassigned_vehicle_filter_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            truck_id = await _insert_vehicle(conn, "Truck")
            # Earlier overall, but assigned -- must not win under vehicle=none.
            await _insert_trip(conn, BASE, vehicle_id=truck_id)
            unassigned_trip_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "none", "")
        assert response.context["trip"]["id"] == unassigned_trip_id
        assert response.context["remaining"] == 1
    finally:
        await raw_pool.close()


def test_review_page_respects_unassigned_vehicle_filter():
    asyncio.run(_unassigned_vehicle_filter_scenario())


async def _tag_and_advance_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            vehicle_id = await _insert_vehicle(conn, "Truck")
            first_id = await _insert_trip(conn, BASE)
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_TAG(
            request, first_id, "business", "  Client meeting  ", "", "", "", {"sub": "test"},
            notes="Gate code", vehicle_id=str(vehicle_id), q="",
            exclusion="not_deductible",
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == second_id
        assert response.context["remaining"] == 1

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose, notes, vehicle_id, exclusion::text, "
                "tag_source::text FROM trips WHERE id = %s", (first_id,)
            )
            assert await cur.fetchone() == (
                "business", "Client meeting", "Gate code", vehicle_id,
                "not_deductible", "human",
            )

        # Tagging the last remaining trip exhausts the pass -- "done", not
        # "empty" (a fresh /review load would still find nothing today, but
        # that's a coincidence of this scenario, not what "done" means).
        response = await REVIEW_TAG(
            request, second_id, "personal", "", "", "", "", {"sub": "test"}, q="",
        )
        assert response.context["state"] == "done"
        assert response.context["remaining"] == 0
    finally:
        await raw_pool.close()


def test_review_tag_atomically_saves_visible_fields_and_advances():
    asyncio.run(_tag_and_advance_scenario())


async def _independent_exclusion_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, BASE)
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_EXCLUSION(
            request, first_id, "not_deductible", "  Saved purpose  ", "", "", "",
            {"sub": "test"}, "Saved notes", "", "",
        )
        assert response.status_code == 204
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT exclusion::text, purpose, notes FROM trips WHERE id = %s",
                (first_id,),
            )).fetchone()
        assert row == ("not_deductible", "Saved purpose", "Saved notes")

        page = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "", "")
        assert page.context["trip"]["id"] == first_id
        assert page.context["remaining"] == 2

        response = await REVIEW_EXCLUSION(
            request, first_id, "", "Saved purpose", "", "", "",
            {"sub": "test"}, "Saved notes", "", "",
        )
        assert response.status_code == 204
        async with pool.connection() as conn:
            assert await (await conn.execute(
                "SELECT exclusion FROM trips WHERE id = %s", (first_id,)
            )).fetchone() == (None,)
            assert await (await conn.execute(
                "SELECT category::text FROM trips WHERE id = %s", (second_id,)
            )).fetchone() == ("unclassified",)
    finally:
        await raw_pool.close()


def test_review_exclusion_sets_and_clears_without_advancing():
    asyncio.run(_independent_exclusion_scenario())


async def _invalid_category_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)

        request = _request(pool)
        for category in ("unclassified", "bogus"):
            with pytest.raises(HTTPException) as exc_info:
                await REVIEW_TAG(
                    request, trip_id, category, "", "", "", "", {"sub": "test"}, q="",
                )
            assert exc_info.value.status_code == 400

        with pytest.raises(HTTPException) as exc_info:
            await REVIEW_EXCLUSION(
                request, trip_id, "bogus", "", "", "", "", {"sub": "test"}, "", "", "",
            )
        assert exc_info.value.status_code == 400

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT category::text FROM trips WHERE id = %s", (trip_id,))
            assert (await cur.fetchone())[0] == "unclassified"
    finally:
        await raw_pool.close()


def test_review_rejects_invalid_category_and_exclusion():
    asyncio.run(_invalid_category_scenario())


async def _skip_saves_purpose_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, BASE)
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1))
        response = await REVIEW_SKIP(
            _request(pool), first_id, "  Deliver records  ", "", "", "", {"sub": "test"}, q="",
        )
        assert response.context["trip"]["id"] == second_id
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose FROM trips WHERE id = %s", (first_id,)
            )
            assert await cur.fetchone() == ("unclassified", "Deliver records")
    finally:
        await raw_pool.close()


def test_review_skip_saves_current_purpose_before_advancing():
    asyncio.run(_skip_saves_purpose_scenario())


async def _action_saves_all_review_fields_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            vehicle_id = await _insert_default_vehicle(conn, "Default")
            first_id = await _insert_trip(conn, BASE)
            await _insert_trip(conn, BASE + timedelta(hours=1))
        request = _request(pool)
        await REVIEW_SKIP(
            request, first_id, "Site visit", "", "", "", {"sub": "test"},
            notes="Gate code", vehicle_id=str(vehicle_id), q="",
            exclusion="not_my_vehicle",
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose, notes, vehicle_id, exclusion::text "
                "FROM trips WHERE id = %s",
                (first_id,),
            )
            assert await cur.fetchone() == (
                "unclassified", "Site visit", "Gate code", vehicle_id, "not_my_vehicle",
            )
    finally:
        await raw_pool.close()


def test_review_skip_ignores_draft_category_and_saves_non_category_fields():
    asyncio.run(_action_saves_all_review_fields_scenario())


async def _undo_tag_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            vehicle_id = await _insert_vehicle(conn, "Truck")
            first_id = await _insert_trip(conn, BASE, vehicle_id=vehicle_id)
            second_id = await _insert_trip(
                conn, BASE + timedelta(hours=1), vehicle_id=vehicle_id
            )

        request = _request(pool)
        from_str, to_str, vehicle_str = "2026-01-01", "2026-01-31", str(vehicle_id)
        response = await REVIEW_TAG(
            request, first_id, "business", "Client meeting",
            from_str, to_str, vehicle_str, {"sub": "test"}, vehicle_id=vehicle_str, q="",
            exclusion="not_deductible",
        )
        assert response.context["trip"]["id"] == second_id

        # Undo reverses the tag through the same clearing path the list
        # view's tag route uses: category back to 'unclassified',
        # tag_source stays 'human' (never NULL) so the auto-tagger can't
        # re-tag a trip a human just touched, and the human-entered purpose
        # is untouched. Filters round-trip exactly like the other
        # /review/{id}/* actions.
        response = await REVIEW_UNDO(
            request, first_id, "tag", from_str, to_str, vehicle_str, {"sub": "test"}, q="",
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == first_id
        assert response.context["filter_from"] == from_str
        assert response.context["filter_to"] == to_str
        assert response.context["filter_vehicle"] == vehicle_str

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose, exclusion::text, tag_source::text "
                "FROM trips WHERE id = %s",
                (first_id,),
            )
            assert await cur.fetchone() == (
                "unclassified", "Client meeting", "not_deductible", "human",
            )
    finally:
        await raw_pool.close()


def test_review_undo_reverses_a_tag_and_represents_the_trip():
    asyncio.run(_undo_tag_scenario())


async def _undo_skip_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, BASE)
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_SKIP(
            request, first_id, "Deliver records", "", "", "", {"sub": "test"}, q="",
            exclusion="not_my_vehicle",
        )
        assert response.context["trip"]["id"] == second_id

        # A skip saves every visible field; undoing it reverses no write, only
        # re-presents the trip. The saved values stay exactly as the user set
        # them, and tag_source is untouched (still NULL: this trip was never
        # human-tagged).
        response = await REVIEW_UNDO(
            request, first_id, "skip", "", "", "", {"sub": "test"}, q="",
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == first_id

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text, purpose, exclusion::text, tag_source::text "
                "FROM trips WHERE id = %s",
                (first_id,),
            )
            assert await cur.fetchone() == (
                "unclassified", "Deliver records", "not_my_vehicle", None,
            )
    finally:
        await raw_pool.close()


def test_review_undo_reverses_a_skip_by_re_presenting_with_no_write():
    asyncio.run(_undo_skip_scenario())


async def _undo_rejects_unknown_kind_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)

        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await REVIEW_UNDO(request, trip_id, "exclusion", "", "", "", {"sub": "test"}, "")
        assert exc_info.value.status_code == 400

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT category::text FROM trips WHERE id = %s", (trip_id,))
            assert (await cur.fetchone())[0] == "unclassified"
    finally:
        await raw_pool.close()


def test_review_undo_rejects_kind_outside_tag_or_skip():
    asyncio.run(_undo_rejects_unknown_kind_scenario())


async def _undo_vanished_trip_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            trip_id = await _insert_trip(conn, BASE)
            await conn.execute("DELETE FROM trips WHERE id = %s", (trip_id,))

        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await REVIEW_UNDO(request, trip_id, "skip", "", "", "", {"sub": "test"}, "")
        assert exc_info.value.status_code == 404
    finally:
        await raw_pool.close()


def test_review_undo_404s_when_the_trip_no_longer_exists():
    asyncio.run(_undo_vanished_trip_scenario())


async def _skip_cursor_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            # Same started_at: the row-value comparison's stable tiebreak
            # must fall back to id.
            tied_lower_id = await _insert_trip(conn, BASE)
            tied_higher_id = await _insert_trip(conn, BASE)
            third_id = await _insert_trip(conn, BASE + timedelta(hours=1))

        request = _request(pool)
        response = await REVIEW_CARD(request, {"sub": "test"}, tied_lower_id, "", "", "", "")
        assert response.context["trip"]["id"] == tied_higher_id
        assert response.context["remaining"] == 2

        # Skip again past the tie, landing on the strictly-later trip.
        response = await REVIEW_CARD(request, {"sub": "test"}, tied_higher_id, "", "", "", "")
        assert response.context["trip"]["id"] == third_id
        assert response.context["remaining"] == 1

        # Exhausting the cursor is "done", not "empty".
        response = await REVIEW_CARD(request, {"sub": "test"}, third_id, "", "", "", "")
        assert response.context["state"] == "done"
        assert response.context["remaining"] == 0

        # A vanished `after` trip (deleted mid-pass) falls back to no
        # cursor -- a fresh pass from the oldest surviving trip -- rather
        # than 404ing.
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM trips WHERE id = %s", (tied_lower_id,))
        response = await REVIEW_CARD(request, {"sub": "test"}, tied_lower_id, "", "", "", "")
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == tied_higher_id
        assert response.context["remaining"] == 2
    finally:
        await raw_pool.close()


def test_review_card_skip_cursor_ties_break_on_id_and_vanished_after_resets_pass():
    asyncio.run(_skip_cursor_scenario())


async def _empty_state_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, BASE, category="business")

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "", "")
        assert response.context["state"] == "empty"
        assert response.context["trip"] is None
        assert response.context["remaining"] == 0
    finally:
        await raw_pool.close()


def test_review_page_shows_empty_state_when_nothing_matches():
    asyncio.run(_empty_state_scenario())


async def _delete_and_advance_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
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
            request, manual_id, from_str, to_str, vehicle_str, {"sub": "test"}, q="",
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
            request, detected_id, from_str, to_str, vehicle_str, {"sub": "test"}, q="",
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
            request, last_id, from_str, to_str, vehicle_str, {"sub": "test"}, q="",
        )
        assert response.context["state"] == "done"
        assert response.context["trip"] is None
        assert response.context["remaining"] == 0
        assert response.context["review_url"] == (
            f"/review?from={from_str}&to={to_str}&vehicle={vehicle_str}"
        )
    finally:
        await raw_pool.close()


def test_review_delete_manual_and_detected_advances_with_cursor_and_filters_to_done():
    asyncio.run(_delete_and_advance_scenario())


async def _review_search_filter_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            matching_id = await _insert_trip(conn, BASE, notes="Zephyr pickup")
            await _insert_trip(conn, BASE + timedelta(hours=1), notes="Ordinary errand")

        request = _request(pool)
        response = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "", "zephyr")
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == matching_id
        assert response.context["remaining"] == 1
    finally:
        await raw_pool.close()


def test_review_page_respects_search_term():
    asyncio.run(_review_search_filter_scenario())


async def _undo_within_search_filtered_pass_scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, BASE, notes="Zephyr pickup")
            second_id = await _insert_trip(conn, BASE + timedelta(hours=1), notes="Zephyr dropoff")
            # Doesn't match the search term, so a search-filtered pass must
            # skip it entirely -- proving undo lands back on the exact trip,
            # not merely "the next unclassified trip" (which this would be,
            # unfiltered).
            await _insert_trip(conn, BASE + timedelta(minutes=30), notes="Unrelated")

        request = _request(pool)
        # Carries the trip's own notes back on the tag request (as a real
        # form submission would) so the write doesn't clobber the text the
        # search term needs to still match after undo clears the category.
        response = await REVIEW_TAG(
            request, first_id, "business", "", "", "", "", {"sub": "test"},
            notes="Zephyr pickup", q="zephyr",
        )
        assert response.context["trip"]["id"] == second_id
        assert response.context["remaining"] == 1

        response = await REVIEW_UNDO(
            request, first_id, "tag", "", "", "", {"sub": "test"}, q="zephyr",
        )
        assert response.context["state"] == "card"
        assert response.context["trip"]["id"] == first_id
        assert response.context["remaining"] == 2

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT category::text FROM trips WHERE id = %s", (first_id,)
            )
            assert (await cur.fetchone())[0] == "unclassified"
    finally:
        await raw_pool.close()


def test_review_undo_lands_on_exact_trip_within_a_search_filtered_pass():
    asyncio.run(_undo_within_search_filtered_pass_scenario())
