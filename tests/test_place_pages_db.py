"""GET /places and GET /places/{place_id} against a real database: row
security, the counting rule, year scoping, the report summary and the tag
rules that name a place. Route handlers are called directly, same pattern as
tests/test_carried_rate_routes_db.py."""
from __future__ import annotations

import asyncio
import functools
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.account_context import account_id
from app.db import make_pool
from app.main import make_templates
from app.ui import make_router
from conftest import add_test_account, reset_account_db
from personal_support import personal_request

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}
MILE = 1609.344
OTHER = 99


@functools.cache
def _routes():
    return tuple(make_router().routes)


def _endpoint(path: str, method: str = "GET"):
    for route in _routes():
        if getattr(route, "path", None) == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} missing")


def _request(pool):
    config = SimpleNamespace(
        display_tz=TZ, trips_page_size=25, app_version="test", missing_trip_gap_m=1000.0,
    )
    return personal_request(SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        headers={},
        url=SimpleNamespace(path="/places"),
    ))


def _scenario(coro) -> None:
    async def run():
        raw_pool = make_pool(TEST_DB)
        await raw_pool.open(wait=True)
        try:
            pool = await reset_account_db(raw_pool)
            await coro(raw_pool, pool)
        finally:
            await raw_pool.close()

    asyncio.run(run())


async def _place(conn, name: str, kind: str) -> int:
    cur = await conn.execute(
        "INSERT INTO places (account_id, name, kind, geom, radius_m) "
        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(-122.25, 37.5), 4326)::geography, 150) RETURNING id",
        (account_id(conn), name, kind),
    )
    return (await cur.fetchone())[0]


async def _trip(conn, started_at, start, end, miles, category="business", exclusion=None) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, "
        " category, exclusion, start_place_id, end_place_id)"
        " VALUES (%s, 'PLACE', 'manual', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (account_id(conn), started_at, started_at + timedelta(minutes=30), miles * MILE,
         category, exclusion, start, end),
    )
    return (await cur.fetchone())[0]


async def _rate(conn, year: int) -> None:
    await conn.execute("DELETE FROM mileage_rates WHERE account_id = %s", (account_id(conn),))
    await conn.execute(
        "INSERT INTO mileage_rates (account_id, year, rate_per_mi) VALUES (%s, %s, 0.7000)",
        (account_id(conn), year),
    )


async def _fixture(conn, year: int) -> dict:
    ids = {
        "home": await _place(conn, "Home", "home"),
        "office": await _place(conn, "Office", "work"),
        "cafe": await _place(conn, "Cafe", "other"),
    }
    home, office, cafe = ids["home"], ids["office"], ids["cafe"]
    at = lambda month, day, hour=9: datetime(year, month, day, hour, tzinfo=TZ)  # noqa: E731
    ids["trips"] = [
        await _trip(conn, at(2, 1), home, office, 10),                       # arrival
        await _trip(conn, at(2, 1, 17), office, home, 10, "personal"),        # departure
        await _trip(conn, at(3, 5), office, office, 2, "unclassified"),       # one trip, both
        await _trip(conn, at(4, 2), cafe, office, 3, "personal", "not_deductible"),
        await _trip(conn, at(5, 9), office, cafe, 40, "business", "not_my_vehicle"),
        await _trip(conn, at(6, 1), home, cafe, 7),                           # not this place
    ]
    # Outside the selected year in local time, though inside it in UTC.
    ids["outside"] = await _trip(conn, datetime(year - 1, 12, 31, 23, 30, tzinfo=TZ), home, office, 5)
    return ids


def _ids_in(body: str) -> set[int]:
    return {int(m) for m in re.findall(r'<article id="trip-(\d+)"', body)}


def test_place_page_counts_each_trip_once_for_the_selected_year():
    async def run(raw_pool, pool):
        year = 2025
        async with pool.connection() as conn:
            await _rate(conn, year)
            ids = await _fixture(conn, year)
        page = _endpoint("/places/{place_id}")
        response = await page(_request(pool), ids["office"], year, USER)
        assert response.status_code == 200
        totals = response.context["totals"]
        assert totals["trip_count"] == 4
        assert totals["arrivals"] == 3
        assert totals["departures"] == 2
        assert round(totals["total_m"] / MILE, 6) == 25.0
        assert round(totals["unclassified_m"] / MILE, 6) == 2.0

        report = response.context["report"]
        assert report.trip_count == 4
        assert round(report.business_m / MILE, 6) == 10.0
        assert round(report.personal_m / MILE, 6) == 10.0
        assert round(report.nondeductible_m / MILE, 6) == 3.0
        assert report.caveats.unclassified_trips == 1
        assert report.total_deduction == pytest.approx(7.0)

        body = response.body.decode()
        # Every trip touching the place is listed, not_my_vehicle included and marked.
        assert _ids_in(body) == set(ids["trips"][:5])
        assert "trip-archive-row-not-my-vehicle" in body
        assert f'href="/trips?from={year}-01-01&amp;to={year}-12-31&amp;place={ids["office"]}"' in body

        earlier = await page(_request(pool), ids["office"], year - 1, USER)
        assert earlier.context["totals"]["trip_count"] == 1
        assert _ids_in(earlier.body.decode()) == {ids["outside"]}

    _scenario(run)


def test_place_page_shows_rules_naming_the_place_or_its_kind_only():
    async def run(raw_pool, pool):
        async with pool.connection() as conn:
            ids = await _fixture(conn, 2025)
            await conn.execute("DELETE FROM tag_rules WHERE account_id = %s", (account_id(conn),))
            rules = []
            for a_place, a_kind, b_place, b_kind, category in (
                (ids["office"], None, None, None, "business"),       # place, wildcard other side
                (None, "home", None, "work", "personal"),            # kind on side B
                (ids["home"], None, ids["cafe"], None, "personal"),  # other places only
                (None, "home", None, None, "personal"),              # wildcard is not a match
                (ids["cafe"], None, ids["office"], None, "business"),  # place on side B
            ):
                cur = await conn.execute(
                    "INSERT INTO tag_rules (account_id, a_place, a_kind, b_place, b_kind, category) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (account_id(conn), a_place, a_kind, b_place, b_kind, category),
                )
                rules.append((await cur.fetchone())[0])
        response = await _endpoint("/places/{place_id}")(_request(pool), ids["office"], 2025, USER)
        shown = response.context["rules"]
        assert [rule["id"] for rule in shown] == [rules[0], rules[1], rules[4]]
        assert [(rule["a_desc"], rule["b_desc"]) for rule in shown] == [
            ("Office", "Any place"), ("Any home", "Any work"), ("Cafe", "Office"),
        ]

    _scenario(run)


def test_another_accounts_place_is_not_found_and_not_listed():
    async def run(raw_pool, pool):
        other = await add_test_account(raw_pool, OTHER)
        async with other.connection() as conn:
            theirs = await _place(conn, "Their office", "work")
            await _trip(conn, datetime(2025, 2, 1, 9, tzinfo=TZ), theirs, theirs, 4)
        async with pool.connection() as conn:
            ours = await _place(conn, "Home", "home")

        with pytest.raises(HTTPException) as refused:
            await _endpoint("/places/{place_id}")(_request(pool), theirs, 2025, USER)
        assert refused.value.status_code == 404

        index = await _endpoint("/places")(_request(pool), USER)
        assert [place["id"] for place in index.context["places"]] == [ours]
        assert "Their office" not in index.body.decode()

        # The owner still sees it.
        assert (await _endpoint("/places/{place_id}")(_request(other), theirs, 2025, USER)).status_code == 200

    _scenario(run)


def test_places_index_counts_this_year_and_the_last_visit():
    async def run(raw_pool, pool):
        year = datetime.now(TZ).year
        async with pool.connection() as conn:
            ids = await _fixture(conn, year)
            unused = await _place(conn, "Unused", "other")
        index = await _endpoint("/places")(_request(pool), USER)
        rows = {place["id"]: place for place in index.context["places"]}
        assert [place["name"] for place in index.context["places"]] == ["Cafe", "Home", "Office", "Unused"]
        office = rows[ids["office"]]
        assert office["trip_count"] == 4
        assert round(office["total_m"] / MILE, 6) == 25.0
        # The not_my_vehicle trip in May is later but left out.
        assert office["last_visit"] == datetime(year, 4, 2, 9, 30, tzinfo=TZ)
        assert rows[ids["home"]]["trip_count"] == 3
        # A departure is a visit too: Home was last left on June 1.
        assert rows[ids["home"]]["last_visit"] == datetime(year, 6, 1, 9, tzinfo=TZ)
        assert rows[unused]["trip_count"] == 0 and rows[unused]["last_visit"] is None
        body = index.body.decode()
        assert f'<a href="/places/{ids["office"]}">Office</a>' in body

    _scenario(run)
