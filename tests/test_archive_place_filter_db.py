"""The archive `place` filter against stored trips: the listing, month pager,
summaries, review, select-all-matching and both exports touch only trips
starting or ending at the place, and another account's place matches none
of them."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from app.account_context import AccountPool, AccountPrincipal, account_id
from app.db import make_pool
from app.rates import load_rates
import app.ui.trips as trips_ui
from conftest import add_test_account, reset_account_db
from prepared_report_support import report_response
from test_api_exports_db import OTHER, _bearer, application
from test_review_db import _request as _review_request
from test_trip_tag_rollup_db import _request as _tag_request
from test_trip_search_db import TZ, _endpoint, _insert_place, _insert_trip, _request

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TRIPS_ARCHIVE = _endpoint("/trips", "GET")
TRIPS_ARCHIVE_LIST = _endpoint("/trips/list", "GET")
TRIP_SELECTION = _endpoint("/trips/selection", "GET")
MONTH_PAGE = _endpoint("/trips/month/{year}/{month}", "GET")
REVIEW_PAGE = _endpoint("/review", "GET")
REVIEW_SKIP = _endpoint("/review/{trip_id}/skip", "POST")
REVIEW_TAG = _endpoint("/review/{trip_id}/tag", "POST")
REVIEW_UNDO = _endpoint("/review/{trip_id}/undo", "POST")
REVIEW_DELETE = _endpoint("/review/{trip_id}/delete", "POST")
TAG = _endpoint("/trips/{trip_id}/tag", "POST")
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=TZ)


async def _seed(conn, marker: str = "") -> SimpleNamespace:
    """One trip per relation to the place `home`: leaving it, arriving at it,
    a loop from and to it, and one that never touches it."""
    home = await _insert_place(conn, f"Home{marker}", 40.0, -74.0)
    work = await _insert_place(conn, f"Work{marker}", 40.1, -74.1)
    leave = await _insert_trip(conn, T0, notes=f"leave{marker}", start_place_id=home, end_place_id=work)
    arrive = await _insert_trip(conn, T0 + timedelta(hours=1), notes=f"arrive{marker}",
                                start_place_id=work, end_place_id=home)
    loop = await _insert_trip(conn, T0 + timedelta(hours=2), notes=f"loop{marker}",
                              start_place_id=home, end_place_id=home)
    other = await _insert_trip(conn, T0 + timedelta(hours=3), notes=f"other{marker}",
                               start_place_id=work, end_place_id=None)
    return SimpleNamespace(home=home, work=work, leave=leave, arrive=arrive, loop=loop, other=other,
                           touching={leave, arrive, loop})


@asynccontextmanager
async def _accounts():
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            mine = await _seed(conn)
        foreign = await add_test_account(raw, OTHER)
        async with foreign.connection() as conn:
            theirs = await _seed(conn, " (theirs)")
        yield pool, mine, theirs
    finally:
        await raw.close()


async def _archive(pool, place, **filters):
    return await TRIPS_ARCHIVE(
        _request(pool, page_size=filters.pop("page_size", 25)), {"sub": "test"},
        category=filters.get("category", ""), from_="", to="", vehicle="",
        manual_date="", manual_start="", manual_notes="", bridge_trip="", manual_open="",
        q="", exclusion="", place=place, date_preset="",
    )


def _ids(response) -> set[int]:
    return {trip["id"] for month in response.context["months"] for trip in month["trips"]}


async def _selection(pool, place):
    response = await TRIP_SELECTION(
        _request(pool), {"sub": "test"}, category="", from_="", to="", vehicle="",
        q="", exclusion="", place=place, date_preset="",
    )
    return json.loads(response.body)


def _csv_notes(body: bytes) -> list[str]:
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    column = rows[0].index("Notes")
    return sorted(row[column] for row in rows[1:])


async def _listing_scenario():
    async with _accounts() as (pool, mine, theirs):
        response = await _archive(pool, str(mine.home))
        assert _ids(response) == mine.touching
        month = response.context["months"][0]
        # The loop starts and ends at home and is still one trip.
        assert month["trip_count"] == 3
        assert response.context["filter_place"] == str(mine.home)
        assert response.context["filter_place_name"] == "Home"
        state = response.context["archive_state"]
        assert state["place"] == str(mine.home) and state["place_name"] == "Home"
        assert state["url"] == f"/trips?place={mine.home}"
        assert state["place_clear_url"] == "/trips"
        assert f"place={mine.home}" in response.context["export_url"]("csv")
        assert f"place={mine.home}" in state["export_xlsx"]
        assert response.context["review_url"] == f"/review?place={mine.home}"
        assert response.context["archive_filters_active"]

        body = response.body.decode()
        assert f'<input type="hidden" name="place" value="{mine.home}">' in body
        assert "<span data-archive-place-name>Home</span>" in body

        assert _ids(await _archive(pool, str(mine.work))) == {mine.leave, mine.arrive, mine.other}

        # Another account's place is a well-formed id that matches nothing
        # here, and its name never reaches this account.
        foreign = await _archive(pool, str(theirs.home))
        assert _ids(foreign) == set()
        assert foreign.context["filter_place_name"] == ""
        assert "(theirs)" not in foreign.body.decode()

        # Junk is no filter, the way a junk vehicle is, and is not carried on.
        junk = await _archive(pool, "abc")
        assert _ids(junk) == mine.touching | {mine.other}
        assert junk.context["filter_place"] == ""
        assert "place=" not in junk.context["archive_state"]["url"]
        assert _ids(await _archive(pool, "")) == mine.touching | {mine.other}
        # Past bigint, an id is still just one that matches nothing.
        assert _ids(await _archive(pool, str(2**70))) == set()
        assert (await _selection(pool, str(2**70)))["trip_ids"] == []


def test_place_filter_lists_trips_touching_the_place_once_each():
    asyncio.run(_listing_scenario())


async def _paging_scenario():
    async with _accounts() as (pool, mine, _theirs):
        response = await _archive(pool, str(mine.home), page_size=1)
        month = response.context["months"][0]
        assert month["has_more"]
        assert month["next_url"] == f"/trips/month/2026/7?offset=1&place={mine.home}"

        page = await MONTH_PAGE(
            _request(pool, page_size=1), year=2026, month=7, offset=1, category="",
            from_="", to="", vehicle="", user={"sub": "test"}, q="", exclusion="",
            place=str(mine.home),
        )
        assert [trip["id"] for trip in page.context["trips"]] == [mine.arrive]
        assert page.context["next_url"] == f"/trips/month/2026/7?offset=2&place={mine.home}"

        listed = await TRIPS_ARCHIVE_LIST(
            _request(pool), {"sub": "test"}, category="", from_="", to="", vehicle="",
            q="", exclusion="", place=str(mine.home), date_preset="", loaded_depth="",
        )
        assert _ids(listed) == mine.touching
        assert listed.headers["HX-Push-Url"] == f"/trips?place={mine.home}"

        async with pool.connection() as conn:
            rates = await load_rates(conn)
            summary = await trips_ui._fetch_month_summary(
                conn, TZ, 2026, 7, rates, "", None, None, None, "", "", mine.home,
            )
        assert summary["trip_count"] == 3


def test_place_filter_survives_paging_the_list_response_and_month_summary():
    asyncio.run(_paging_scenario())


async def _selection_and_export_scenario():
    async with _accounts() as (pool, mine, theirs):
        selected = await _selection(pool, str(mine.home))
        assert set(selected["trip_ids"]) == mine.touching and selected["count"] == 3
        assert (await _selection(pool, str(theirs.home)))["trip_ids"] == []

        exported = await report_response(
            pool, "/export?" + urlencode({"format": "csv", "place": mine.home}),
            SimpleNamespace(display_tz=TZ),
        )
        assert exported.status_code == 200
        assert _csv_notes(exported.body) == ["arrive", "leave", "loop"]

        foreign = await report_response(
            pool, "/export?" + urlencode({"format": "csv", "place": theirs.home}),
            SimpleNamespace(display_tz=TZ),
        )
        assert foreign.status_code == 200
        assert _csv_notes(foreign.body) == []
        assert b"(theirs)" not in foreign.body


def test_select_all_matching_and_web_export_touch_only_the_place_trips():
    asyncio.run(_selection_and_export_scenario())


async def _review_scenario():
    async with _accounts() as (pool, mine, _theirs):
        request = _review_request(pool)
        page = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "", "", str(mine.home))
        assert page.context["trip"]["id"] == mine.leave
        assert page.context["remaining"] == 3
        assert page.context["review_url"] == f"/review?place={mine.home}"
        assert f'<input type="hidden" name="place" value="{mine.home}">' in page.body.decode()

        skipped = await REVIEW_SKIP(
            request, mine.leave, "", "", "", "", {"sub": "test"}, q="", place=str(mine.home),
        )
        assert skipped.context["trip"]["id"] == mine.arrive
        assert skipped.context["remaining"] == 2
        assert skipped.context["filter_place"] == str(mine.home)


def test_review_keeps_the_place_filter_through_the_pass():
    asyncio.run(_review_scenario())


async def _api_export_scenario(tmp_path):
    async with application(tmp_path) as state:
        pool = AccountPool(state.app.state.runtime_pool, AccountPrincipal(state.owner, True, 1))
        async with pool.connection() as conn:
            mine = await _seed(conn)
        foreign = AccountPool(state.app.state.runtime_pool, AccountPrincipal(OTHER, True, 1))
        async with foreign.connection() as conn:
            theirs = await _seed(conn, " (theirs)")

        async def export(place):
            return await state.client.get(
                "/api/v1/exports/trips", params={"format": "csv", "place": place},
                headers=_bearer(state),
            )

        response = await export(str(mine.home))
        assert response.status_code == 200
        assert _csv_notes(response.content) == ["arrive", "leave", "loop"]
        web = await state.client.get("/export", params={"format": "csv", "place": str(mine.home)})
        assert web.status_code == 200 and web.content == response.content

        response = await export(str(theirs.home))
        assert response.status_code == 200
        assert _csv_notes(response.content) == []
        assert b"(theirs)" not in response.content

        for bad in ("abc", "0", "-1", str(2**63)):
            response = await export(bad)
            assert response.status_code == 422, bad
            assert response.json()["error"]["fields"] == {"place": "This value is not valid."}


def test_api_export_takes_the_place_filter_and_refuses_a_malformed_one(tmp_path):
    asyncio.run(_api_export_scenario(tmp_path))


async def _api_listing_unchanged_scenario(tmp_path):
    async with application(tmp_path) as state:
        pool = AccountPool(state.app.state.runtime_pool, AccountPrincipal(state.owner, True, 1))
        async with pool.connection() as conn:
            mine = await _seed(conn)
            total = (await (await conn.execute(
                "SELECT count(*) FROM trips WHERE account_id = %s", (account_id(conn),))).fetchone())[0]
        # The API v1 listing does not take `place`; it lists every trip.
        response = await state.client.get(
            "/api/v1/trips", params={"place": str(mine.home), "limit": 100}, headers=_bearer(state),
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) == total


def test_api_trip_listing_ignores_place(tmp_path):
    asyncio.run(_api_listing_unchanged_scenario(tmp_path))


async def _review_writes_scenario():
    async with _accounts() as (pool, mine, _theirs):
        async with pool.connection() as conn:
            # Unclassified and between the place's first two trips, so a
            # review pass that lost the place would show it next.
            between = await _insert_trip(conn, T0 + timedelta(minutes=30), notes="between",
                                         start_place_id=mine.work, end_place_id=None)
        request = _review_request(pool)
        place = str(mine.home)

        tagged = await REVIEW_TAG(
            request, mine.leave, "business", "", "", "", "", {"sub": "test"}, q="", place=place,
        )
        assert tagged.context["trip"]["id"] == mine.arrive
        assert tagged.context["remaining"] == 2
        assert tagged.context["filter_place"] == place

        undone = await REVIEW_UNDO(
            request, mine.leave, "tag", "", "", "", {"sub": "test"}, q="", place=place,
        )
        assert undone.context["trip"]["id"] == mine.leave
        assert undone.context["remaining"] == 3
        assert undone.context["filter_place"] == place

        deleted = await REVIEW_DELETE(
            request, mine.leave, "", "", "", {"sub": "test"}, q="", place=place,
        )
        assert deleted.context["trip"]["id"] == mine.arrive
        assert deleted.context["remaining"] == 2
        assert deleted.context["filter_place"] == place
        assert f'<input type="hidden" name="place" value="{place}">' in deleted.body.decode()

        # Without the place the same pass reaches the trip in between.
        unfiltered = await REVIEW_PAGE(request, {"sub": "test"}, "", "", "", "", "")
        assert unfiltered.context["trip"]["id"] == between


def test_review_tag_undo_and_delete_keep_the_place_filter():
    asyncio.run(_review_writes_scenario())


async def _inline_tag_summary_scenario():
    async with _accounts() as (pool, mine, _theirs):
        current = f"https://odograph.example/trips?from=2026-07-01&to=2026-07-31&place={mine.home}"
        response = await TAG(_tag_request(pool, current_url=current), mine.leave, "business",
                             {"sub": "test"})
        assert response.status_code == 200
        # The month has four trips; only three touch the place.
        assert response.context["month"]["trip_count"] == 3
        assert 'id="month-summary-2026-7"' in response.body.decode()

        unfiltered = "https://odograph.example/trips?from=2026-07-01&to=2026-07-31"
        response = await TAG(_tag_request(pool, current_url=unfiltered), mine.arrive, "business",
                             {"sub": "test"})
        assert response.context["month"]["trip_count"] == 4


def test_inline_tag_month_summary_keeps_the_place_filter():
    asyncio.run(_inline_tag_summary_scenario())
