"""DB-backed tests for the archive list response's history directives.

`GET /trips/list` returns fragments, so the address bar must never end up
pointing at it: the canonical `/trips` URL travels back on an `HX-Push-Url`
or `HX-Replace-Url` header instead. Which of the two is used depends on the
browser's current URL, carried by `HX-Current-URL`, so a filter that
re-resolves to the state already on screen replaces rather than stacking a
duplicate history entry. Route handlers are called directly (bypassing
FastAPI's dependency injection), same pattern as tests/test_trip_search_db.py.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.account_context import account_id
from personal_support import personal_request
from app.main import make_templates
from app.ui import make_router
from app.ui.trips import _archive_date_preset_ranges
from conftest import reset_account_db

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
TRIPS_ARCHIVE_LIST = _endpoint("/trips/list", "GET")
BATCH_UPDATE = _endpoint("/trips/batch_update", "POST")


def _request(pool, headers: dict | None = None, page_size: int = 25):
    config = SimpleNamespace(display_tz=TZ, trips_page_size=page_size, app_version="test")
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        headers=headers or {},
    ))


async def _insert_trip(conn, started_at, category="business"):
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, category)"
        " VALUES (%s, 'ARCHHIST', 'manual', %s, %s, 1000, %s) RETURNING id",
        (account_id(conn), started_at, started_at + timedelta(minutes=10), category,),
    )
    return (await cur.fetchone())[0]


async def _list(pool, current_url=None, page_size=25, **filters):
    headers = {} if current_url is None else {"HX-Current-URL": current_url}
    return await TRIPS_ARCHIVE_LIST(
        _request(pool, headers, page_size), {"sub": "test"},
        category=filters.get("category", ""),
        from_=filters.get("from_", ""), to=filters.get("to", ""),
        vehicle=filters.get("vehicle", ""), q=filters.get("q", ""),
        exclusion=filters.get("exclusion", ""),
        date_preset=filters.get("date_preset", ""),
        loaded_depth=filters.get("loaded_depth", ""),
    )


def _scenario(coro) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            async with pool.connection() as conn:
                await _insert_trip(conn, T0)
            await coro(pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


def _directive(response) -> tuple[str, str]:
    push = response.headers.get("HX-Push-Url")
    replace = response.headers.get("HX-Replace-Url")
    assert push is None or replace is None, "a response must carry only one history directive"
    assert push is not None or replace is not None, "expected a history directive"
    if push is not None:
        return "HX-Push-Url", push
    return "HX-Replace-Url", replace


async def _push_scenario(pool):
    # No page context at all (a caller without the header).
    header, url = _directive(await _list(pool, category="business"))
    assert (header, url) == ("HX-Push-Url", "/trips?category=business")

    # A current URL describing a different filter state.
    header, url = _directive(await _list(
        pool, current_url="https://odograph.example/trips?category=personal",
        category="business",
    ))
    assert (header, url) == ("HX-Push-Url", "/trips?category=business")

    # Back to the unfiltered archive.
    header, url = _directive(await _list(
        pool, current_url="https://odograph.example/trips?category=business",
    ))
    assert (header, url) == ("HX-Push-Url", "/trips")


def test_archive_list_pushes_the_canonical_url_for_a_new_filter_state():
    _scenario(_push_scenario)


async def _replace_scenario(pool):
    header, url = _directive(await _list(
        pool, current_url="https://odograph.example/trips?category=business",
        category="business",
    ))
    assert (header, url) == ("HX-Replace-Url", "/trips?category=business")

    header, url = _directive(await _list(pool, current_url="https://odograph.example/trips"))
    assert (header, url) == ("HX-Replace-Url", "/trips")

    # A preset re-resolving to the concrete dates already in the address bar
    # is the same state, not a new one, so it must not stack a duplicate
    # history entry.
    from_, to = _archive_date_preset_ranges(TZ)["this_month"]
    header, url = _directive(await _list(
        pool, current_url=f"https://odograph.example/trips?from={from_}&to={to}",
        date_preset="this_month",
    ))
    assert header == "HX-Replace-Url"
    assert url == f"/trips?from={from_}&to={to}"


def test_archive_list_replaces_when_the_canonical_url_is_already_current():
    _scenario(_replace_scenario)


async def _never_fragment_scenario(pool):
    for current_url in (None, "https://odograph.example/trips", "https://odograph.example/report"):
        for filters in ({}, {"category": "business"}, {"q": "zephyr", "vehicle": "none"}):
            _, url = _directive(await _list(pool, current_url=current_url, **filters))
            assert url.split("?")[0] == "/trips", url
            assert "/trips/list" not in url


def test_archive_list_history_url_is_never_the_fragment_endpoint():
    _scenario(_never_fragment_scenario)


async def _history_restore_document_scenario(pool):
    # htmx refetches this URL on a history cache miss and extracts the marked
    # results root from it, so it has to stay a full document even when the
    # request announces itself as an htmx history restore.
    response = await TRIPS_ARCHIVE(
        _request(pool, {
            "HX-Request": "true",
            "HX-History-Restore-Request": "true",
            "HX-Current-URL": "https://odograph.example/trips?category=business",
        }),
        {"sub": "test"}, category="business", from_="", to="", vehicle="",
        manual_date="", manual_start="", manual_notes="", bridge_trip="",
        manual_open="", notice="", q="", exclusion="", date_preset="",
    )
    body = response.body.decode()

    assert body.lstrip().startswith("<!doctype html>")
    assert body.count("hx-history-elt") == 1
    assert '<div id="trip-archive-results" class="trip-archive-results" hx-history-elt>' in body
    assert "HX-Push-Url" not in response.headers
    assert "HX-Replace-Url" not in response.headers


def test_archive_page_stays_a_full_document_for_a_history_restore_request():
    _scenario(_history_restore_document_scenario)


async def _batch_refresh_scenario(pool):
    # `_scenario` seeds one unrelated trip; this scenario builds its own two
    # months on top of it, in a category the filter below excludes.
    async with pool.connection() as conn:
        july = [
            await _insert_trip(conn, T0 + timedelta(hours=offset), category="business")
            for offset in range(4)
        ]
        august = [
            await _insert_trip(
                conn, T0 + timedelta(days=31, hours=offset), category="business"
            )
            for offset in range(3)
        ]

    # The client has loaded a second page in each month, then moves the
    # newest July trip out of the filtered set.
    depth = json.dumps({"2026-07": 2, "2026-08": 2})
    await BATCH_UPDATE(
        _request(pool), trip_ids=[july[3]], category="personal", vehicle_id="keep",
        purpose="", set_purpose=False, user={"sub": "test"}, exclusion="keep",
    )

    current_url = "https://odograph.example/trips?category=business"
    response = await _list(
        pool, current_url=current_url, page_size=1,
        category="business", loaded_depth=depth,
    )
    months = {(m["year"], m["month_num"]): m for m in response.context["months"]}

    # Each month keeps the depth the client had rendered instead of
    # collapsing back to its first page, and the updated trip is gone from
    # the filtered rows without shifting the ones that remain.
    assert [trip["id"] for trip in months[(2026, 7)]["trips"]] == [july[2], july[1]]
    assert [trip["id"] for trip in months[(2026, 8)]["trips"]] == [august[2], august[1]]
    assert months[(2026, 7)]["loaded_depth"] == 2
    assert months[(2026, 8)]["loaded_depth"] == 2
    assert months[(2026, 7)]["has_more"] is True
    assert months[(2026, 8)]["has_more"] is True
    assert "offset=2" in months[(2026, 7)]["next_url"]
    assert "offset=2" in months[(2026, 8)]["next_url"]

    # Same filters as the address bar already holds, so the refresh replaces
    # rather than stacking a duplicate history entry, and the transient depth
    # never reaches the canonical URL or the export links.
    header, url = _directive(response)
    assert (header, url) == ("HX-Replace-Url", "/trips?category=business")
    state = response.context["archive_state"]
    assert "loaded_depth" not in url
    assert "loaded_depth" not in state["url"]
    assert "loaded_depth" not in state["export_csv"]
    assert "loaded_depth" not in state["export_xlsx"]

    # Without the depth metadata the same request returns one row per month,
    # which is what the retained depth is protecting the refresh from.
    collapsed = await _list(
        pool, current_url=current_url, page_size=1, category="business",
    )
    assert all(len(month["trips"]) == 1 for month in collapsed.context["months"])


def test_archive_list_retains_loaded_depth_and_replaces_history_after_a_batch_write():
    _scenario(_batch_refresh_scenario)
