"""DB-backed tests for the free-text trip search (`q`), threaded through
`_trip_filter_sql` and shared by the trip list, `/export`, and the month
pager. `/review`'s own copy of this behavior (including undo across a
search-filtered pass) is covered by tests/test_review_db.py, right next to
its other filter tests. Route handlers are called directly (bypassing
FastAPI's dependency injection), same pattern as
tests/test_vehicle_filter_db.py.
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

from app.db import make_pool, run_migrations
from app.main import make_templates
from app.rates import deduction, load_rates
from app.ui import make_router

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
MONTH_PAGE = _endpoint("/trips/month/{year}/{month}", "GET")


def _request(pool, page_size=25):
    config = SimpleNamespace(display_tz=TZ, trips_page_size=page_size, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
    )


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _insert_trip(
    conn, started_at, *, notes=None, purpose=None, category="unclassified",
    vehicle_id=None, start_place_id=None, end_place_id=None,
    start_lat=40.0, start_lon=-74.0, end_lat=40.1, end_lon=-74.1,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, notes, purpose, "
        " category, vehicle_id, start_place_id, end_place_id, start_geom, end_geom) "
        "VALUES ('FLT', 'manual', %s, %s, 1000, %s, %s, %s, %s, %s, %s, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) RETURNING id",
        (
            started_at, started_at + timedelta(minutes=10), notes, purpose, category,
            vehicle_id, start_place_id, end_place_id, start_lon, start_lat, end_lon, end_lat,
        ),
    )
    return (await cur.fetchone())[0]


async def _insert_place(conn, name: str, lat: float, lon: float) -> int:
    cur = await conn.execute(
        "INSERT INTO places (name, geom) VALUES "
        "(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) RETURNING id",
        (name, lon, lat),
    )
    return (await cur.fetchone())[0]


async def _insert_geocode(conn, lat: float, lon: float, address: str) -> None:
    await conn.execute(
        "INSERT INTO geocode_cache (lat, lon, address) VALUES (%s, %s, %s)",
        (round(lat, 4), round(lon, 4), address),
    )


async def _insert_vehicle(conn, name: str) -> int:
    cur = await conn.execute("INSERT INTO vehicles (name) VALUES (%s) RETURNING id", (name,))
    return (await cur.fetchone())[0]


def _trip_ids(response) -> set[int]:
    return {
        trip["id"] for month in response.context["months"] for trip in month["trips"]
    }


async def _archive(pool, q="", category="", from_="", to="", vehicle="", page_size=25):
    return await TRIPS_ARCHIVE(
        _request(pool, page_size=page_size), {"sub": "test"},
        category=category, from_=from_, to=to, vehicle=vehicle,
        manual_date="", manual_start="", manual_notes="", bridge_trip="", manual_open="",
        q=q,
    )


async def _export(pool, q="", category="", from_="", to="", vehicle="", fmt="csv"):
    return await EXPORT_TRIPS(
        _request(pool), {"sub": "test"}, format=fmt, category=category,
        from_=from_, to=to, vehicle=vehicle, q=q,
    )


async def _month_page(pool, year, month, offset=0, q="", category="", from_="", to="", vehicle="", page_size=25):
    return await MONTH_PAGE(
        _request(pool, page_size=page_size), year=year, month=month, offset=offset,
        category=category, from_=from_, to=to, vehicle=vehicle, user={"sub": "test"}, q=q,
    )


# --- B4.1: each searchable field individually (acceptance criteria 1, 8) ---

async def _notes_field_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(conn, T0, notes="Picked up client Zephyr")
            await _insert_trip(conn, T0 + timedelta(hours=1), notes="Ordinary errand")
        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_notes_case_insensitively_and_on_substring():
    asyncio.run(_notes_field_scenario())


async def _purpose_field_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(conn, T0, purpose="Zephyr conference")
            await _insert_trip(conn, T0 + timedelta(hours=1), purpose="Weekly standup")
        response = await _archive(pool, q="ZEPHYR")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_purpose_case_insensitively_and_on_substring():
    asyncio.run(_purpose_field_scenario())


async def _start_place_name_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            place_id = await _insert_place(conn, "Zephyr Depot", 40.0, -74.0)
            target_id = await _insert_trip(conn, T0, start_place_id=place_id)
            await _insert_trip(conn, T0 + timedelta(hours=1))
        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_start_place_name():
    asyncio.run(_start_place_name_scenario())


async def _end_place_name_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            place_id = await _insert_place(conn, "Zephyr Warehouse", 40.1, -74.1)
            target_id = await _insert_trip(conn, T0, end_place_id=place_id)
            await _insert_trip(conn, T0 + timedelta(hours=1))
        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_end_place_name():
    asyncio.run(_end_place_name_scenario())


async def _start_address_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_geocode(conn, 40.0, -74.0, "1 Zephyr Ave")
            target_id = await _insert_trip(conn, T0, start_lat=40.0, start_lon=-74.0)
            await _insert_trip(conn, T0 + timedelta(hours=1), start_lat=41.0, start_lon=-75.0)
        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_start_cached_address():
    asyncio.run(_start_address_scenario())


async def _end_address_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_geocode(conn, 40.1, -74.1, "1 Zephyr Ave")
            target_id = await _insert_trip(conn, T0, end_lat=40.1, end_lon=-74.1)
            await _insert_trip(conn, T0 + timedelta(hours=1), end_lat=41.1, end_lon=-75.1)
        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_end_cached_address():
    asyncio.run(_end_address_scenario())


# --- B4.2: combines with other filters, narrows rather than replaces
# (acceptance criterion 2) ---

async def _category_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            business_id = await _insert_trip(conn, T0, notes="Zephyr run", category="business")
            personal_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), notes="Zephyr errand", category="personal"
            )
            await _insert_trip(conn, T0 + timedelta(hours=2), notes="Unrelated", category="business")

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {business_id, personal_id}

        response = await _archive(pool, q="zephyr", category="business")
        assert _trip_ids(response) == {business_id}
    finally:
        await pool.close()


def test_search_combines_with_category_filter_and_narrows():
    asyncio.run(_category_combination_scenario())


async def _date_range_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            july_id = await _insert_trip(conn, T0, notes="Zephyr run")
            august_id = await _insert_trip(conn, T0 + timedelta(days=31), notes="Zephyr errand")
            await _insert_trip(conn, T0, notes="Unrelated")

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {july_id, august_id}

        response = await _archive(pool, q="zephyr", from_="2026-07-01", to="2026-07-31")
        assert _trip_ids(response) == {july_id}
    finally:
        await pool.close()


def test_search_combines_with_date_range_filter_and_narrows():
    asyncio.run(_date_range_combination_scenario())


async def _vehicle_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _insert_vehicle(conn, "Truck")
            sedan_id = await _insert_vehicle(conn, "Sedan")
            truck_trip_id = await _insert_trip(conn, T0, notes="Zephyr run", vehicle_id=truck_id)
            sedan_trip_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), notes="Zephyr errand", vehicle_id=sedan_id
            )

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {truck_trip_id, sedan_trip_id}

        response = await _archive(pool, q="zephyr", vehicle=str(truck_id))
        assert _trip_ids(response) == {truck_trip_id}
    finally:
        await pool.close()


def test_search_combines_with_vehicle_filter_and_narrows():
    asyncio.run(_vehicle_combination_scenario())


# --- B4.2: the term round-trips through every link the page builds
# (acceptance criterion 3) ---

async def _link_round_trip_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, T0, notes="Zephyr run", category="business")

        response = await _archive(pool, q="zephyr")
        ctx = response.context
        assert "q=zephyr" in ctx["filter_url"]("business")
        assert "q=zephyr" in ctx["export_url"]("csv")
        assert "q=zephyr" in ctx["review_url"]
        assert ctx["months"], "expected at least one month bucket"
        assert "q=zephyr" in ctx["months"][0]["next_url"]
    finally:
        await pool.close()


def test_search_term_survives_filter_export_review_and_month_pagination_links():
    asyncio.run(_link_round_trip_scenario())


async def _export_parity_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, T0, notes="Zephyr run")
            await _insert_trip(conn, T0 + timedelta(hours=1), notes="Unrelated")

        archive_response = await _archive(pool, q="zephyr")
        archive_notes = {
            trip["notes"]
            for month in archive_response.context["months"] for trip in month["trips"]
        }

        export_response = await _export(pool, q="zephyr")
        rows = list(csv.reader(io.StringIO(export_response.body.decode("utf-8"))))
        notes_column = rows[0].index("Notes")
        export_notes = {row[notes_column] for row in rows[1:]}

        assert archive_notes == export_notes == {"Zephyr run"}
    finally:
        await pool.close()


def test_export_taken_with_search_active_matches_what_is_on_screen():
    asyncio.run(_export_parity_scenario())


# --- B4.2: literal wildcard characters (acceptance criterion 6) ---

async def _percent_wildcard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(conn, T0, notes="50% off parking")
            await _insert_trip(conn, T0 + timedelta(hours=1), notes="No percent sign here")

        # Unescaped, "%" is the ILIKE wildcard and would match everything.
        response = await _archive(pool, q="%")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_term_matches_a_literal_percent_sign():
    asyncio.run(_percent_wildcard_scenario())


async def _underscore_wildcard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            # Unescaped, "_" matches any single character, so an unescaped
            # search for "a1_b2" would also match "a1Xb2".
            target_id = await _insert_trip(conn, T0, notes="Ref a1_b2 confirmed")
            await _insert_trip(conn, T0 + timedelta(hours=1), notes="Ref a1Xb2 confirmed")

        response = await _archive(pool, q="a1_b2")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_term_matches_a_literal_underscore():
    asyncio.run(_underscore_wildcard_scenario())


async def _backslash_wildcard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(conn, T0, notes=r"C:\Users\driver\route")
            await _insert_trip(conn, T0 + timedelta(hours=1), notes="Unrelated route notes")

        response = await _archive(pool, q=r"Users\driver")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_term_matches_a_literal_backslash():
    asyncio.run(_backslash_wildcard_scenario())


# --- B4.2: empty/whitespace-only term is no search (acceptance criteria
# 5 and 9: the response contract, including exact URLs, is unchanged) ---

async def _empty_and_whitespace_term_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            first_id = await _insert_trip(conn, T0, notes="Alpha")
            second_id = await _insert_trip(conn, T0 + timedelta(hours=1), notes="Beta")

        unfiltered = await _archive(pool)
        empty = await _archive(pool, q="")
        whitespace = await _archive(pool, q="   ")

        expected = {first_id, second_id}
        assert _trip_ids(unfiltered) == expected
        assert _trip_ids(empty) == expected
        assert _trip_ids(whitespace) == expected

        # No active search: filter/export/review links carry no `q` param
        # at all, matching the exact URLs built before this filter existed.
        assert "q=" not in unfiltered.context["filter_url"]("business")
        assert "q=" not in unfiltered.context["export_url"]("csv")
        assert "q=" not in unfiltered.context["review_url"]
        assert "q=" not in whitespace.context["review_url"]
    finally:
        await pool.close()


def test_empty_or_whitespace_only_term_is_treated_as_no_search():
    asyncio.run(_empty_and_whitespace_term_scenario())


# --- B4.2: month grouping, totals, deduction, and pagination are computed
# over the filtered set (acceptance criterion 4) ---

async def _month_and_pagination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            july_a = await _insert_trip(conn, T0, notes="Zephyr run", category="business")
            july_b = await _insert_trip(
                conn, T0 + timedelta(hours=1), notes="Zephyr errand", category="business"
            )
            await _insert_trip(conn, T0 + timedelta(hours=2), notes="Unrelated", category="business")
            august_id = await _insert_trip(
                conn, T0 + timedelta(days=31), notes="Zephyr trip", category="business"
            )
            rates = await load_rates(conn)

        # page_size=1 forces has_more for the two-match July bucket.
        response = await _archive(pool, q="zephyr", page_size=1)
        months_by_key = {(m["year"], m["month_num"]): m for m in response.context["months"]}

        july = months_by_key[(2026, 7)]
        assert july["trip_count"] == 2
        assert len(july["trips"]) == 1
        assert july["has_more"] is True
        # Two matching business trips at 1000m each -- not the third,
        # unmatched business trip that month.
        assert july["total_m"] == pytest.approx(2000.0)
        assert july["business_m"] == pytest.approx(2000.0)
        assert july["business_deduction"] == pytest.approx(
            deduction(2000.0, 2026, rates, 7)
        )

        first_page_ids = {t["id"] for t in july["trips"]}
        page_two = await _month_page(pool, 2026, 7, offset=1, q="zephyr", page_size=1)
        second_page_ids = {t["id"] for t in page_two.context["trips"]}
        assert first_page_ids | second_page_ids == {july_a, july_b}
        assert page_two.context["has_more"] is False

        august = months_by_key[(2026, 8)]
        assert august["trip_count"] == 1
        assert {t["id"] for t in august["trips"]} == {august_id}
    finally:
        await pool.close()


def test_search_narrows_month_grouping_totals_deduction_and_pagination():
    asyncio.run(_month_and_pagination_scenario())
