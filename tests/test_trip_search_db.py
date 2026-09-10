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
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db import make_pool
from app.main import make_templates
from app.rates import deduction, load_rates
from app.ui import make_router
import app.ui.trips as trips_ui
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
TRIPS_ARCHIVE_LIST = _endpoint("/trips/list", "GET")
EXPORT_TRIPS = _endpoint("/export", "GET")
MONTH_PAGE = _endpoint("/trips/month/{year}/{month}", "GET")


def _request(pool, page_size=25):
    config = SimpleNamespace(display_tz=TZ, trips_page_size=page_size, app_version="test")
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool, templates=make_templates(config), config=config,
        )),
        session={"csrf": "test-csrf"},
        # The list response reads HX-Current-URL to decide whether its
        # canonical URL is a new history entry; no header means no page
        # context, which is what a direct route call is.
        headers={},
    )


async def _insert_trip(
    conn, started_at, *, notes=None, purpose=None, category="unclassified",
    vehicle_id=None, start_place_id=None, end_place_id=None,
    start_lat=40.0, start_lon=-74.0, end_lat=40.1, end_lon=-74.1,
    exclusion=None, start_label=None, end_label=None,
) -> int:
    # A label requires its own endpoint's geom to be NULL
    # (migrations/025_manual_trip_labels.sql), so a caller passing
    # start_label/end_label must also pass that endpoint's lat/lon as None:
    # ST_MakePoint is strict, so a NULL argument makes the whole geography
    # NULL rather than a point at (0, 0).
    cur = await conn.execute(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, notes, purpose, "
        " category, vehicle_id, start_place_id, end_place_id, start_geom, end_geom, exclusion, "
        " start_label, end_label) "
        "VALUES ('FLT', 'manual', %s, %s, 1000, %s, %s, %s, %s, %s, %s, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s) RETURNING id",
        (
            started_at, started_at + timedelta(minutes=10), notes, purpose, category,
            vehicle_id, start_place_id, end_place_id, start_lon, start_lat, end_lon, end_lat,
            exclusion, start_label, end_label,
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


async def _archive(
    pool, q="", category="", from_="", to="", vehicle="", page_size=25,
    exclusion="",
):
    return await TRIPS_ARCHIVE(
        _request(pool, page_size=page_size), {"sub": "test"},
        category=category, from_=from_, to=to, vehicle=vehicle,
        manual_date="", manual_start="", manual_notes="", bridge_trip="", manual_open="",
        q=q, exclusion=exclusion,
    )


async def _archive_list(
    pool, q="", category="", from_="", to="", vehicle="", page_size=25,
    exclusion="", loaded_depth="",
):
    return await TRIPS_ARCHIVE_LIST(
        _request(pool, page_size=page_size), {"sub": "test"},
        category=category, from_=from_, to=to, vehicle=vehicle, q=q,
        exclusion=exclusion, loaded_depth=loaded_depth,
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
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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


# --- Custom endpoint labels (trips.start_label/end_label) also match,
# independently of each other and of the saved-place/address fields above ---

async def _start_label_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(
                conn, T0, start_lat=None, start_lon=None, start_label="Zephyr Cabin"
            )
            await _insert_trip(conn, T0 + timedelta(hours=1))
        response = await _archive(pool, q="ZEPHYR")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_start_label_case_insensitively_and_on_substring():
    asyncio.run(_start_label_scenario())


async def _end_label_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(
                conn, T0, end_lat=None, end_lon=None, end_label="Zephyr Trailhead"
            )
            await _insert_trip(conn, T0 + timedelta(hours=1))
        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_matches_end_label_case_insensitively_and_on_substring():
    asyncio.run(_end_label_scenario())


# --- B4.2: combines with other filters, narrows rather than replaces
# (acceptance criterion 2) ---

async def _category_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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


# --- A label match composes with every other filter the same way notes/
# purpose do above: an additional OR branch inside the search term's own
# parenthesized group, not a new top-level AND. Each scenario below proves
# that, unfiltered, both label-matching trips come back, but the other
# filter still excludes the one that fails it -- the regression a misplaced
# parenthesis around the OR group would let slip through. ---

async def _label_category_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            business_id = await _insert_trip(
                conn, T0, start_lat=None, start_lon=None, start_label="Zephyr Cabin",
                category="business",
            )
            personal_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), start_lat=None, start_lon=None,
                start_label="Zephyr Lodge", category="personal",
            )

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {business_id, personal_id}

        response = await _archive(pool, q="zephyr", category="business")
        assert _trip_ids(response) == {business_id}
    finally:
        await pool.close()


def test_label_search_combines_with_category_filter_and_excludes_wrong_category():
    asyncio.run(_label_category_combination_scenario())


async def _label_date_range_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            july_id = await _insert_trip(
                conn, T0, start_lat=None, start_lon=None, start_label="Zephyr Cabin"
            )
            august_id = await _insert_trip(
                conn, T0 + timedelta(days=31), start_lat=None, start_lon=None,
                start_label="Zephyr Lodge",
            )

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {july_id, august_id}

        response = await _archive(pool, q="zephyr", from_="2026-07-01", to="2026-07-31")
        assert _trip_ids(response) == {july_id}
    finally:
        await pool.close()


def test_label_search_combines_with_date_range_filter_and_excludes_outside_range():
    asyncio.run(_label_date_range_combination_scenario())


async def _label_vehicle_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            truck_id = await _insert_vehicle(conn, "Truck")
            sedan_id = await _insert_vehicle(conn, "Sedan")
            truck_trip_id = await _insert_trip(
                conn, T0, start_lat=None, start_lon=None, start_label="Zephyr Cabin",
                vehicle_id=truck_id,
            )
            sedan_trip_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), start_lat=None, start_lon=None,
                start_label="Zephyr Lodge", vehicle_id=sedan_id,
            )

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {truck_trip_id, sedan_trip_id}

        response = await _archive(pool, q="zephyr", vehicle=str(truck_id))
        assert _trip_ids(response) == {truck_trip_id}
    finally:
        await pool.close()


def test_label_search_combines_with_vehicle_filter_and_excludes_wrong_vehicle():
    asyncio.run(_label_vehicle_combination_scenario())


async def _label_exclusion_combination_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            excluded_id = await _insert_trip(
                conn, T0, start_lat=None, start_lon=None, start_label="Zephyr Cabin",
                exclusion="not_my_vehicle",
            )
            normal_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), start_lat=None, start_lon=None,
                start_label="Zephyr Lodge",
            )

        response = await _archive(pool, q="zephyr")
        assert _trip_ids(response) == {excluded_id, normal_id}

        response = await _archive(pool, q="zephyr", exclusion="not_my_vehicle")
        assert _trip_ids(response) == {excluded_id}

        # EXCLUSION_FILTER_NONE's "normal trips only" branch also still
        # excludes the label-matching excluded trip.
        response = await _archive(pool, q="zephyr", exclusion="none")
        assert _trip_ids(response) == {normal_id}
    finally:
        await pool.close()


def test_label_search_combines_with_exclusion_filter_and_excludes_wrong_exclusion():
    asyncio.run(_label_exclusion_combination_scenario())


# --- B4.2: the term round-trips through every link the page builds
# (acceptance criterion 3) ---

async def _link_round_trip_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            await _insert_trip(conn, T0, notes="Zephyr run", category="business")

        response = await _archive(pool, q="zephyr")
        ctx = response.context
        assert "q=zephyr" in ctx["archive_state"]["url"]
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
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
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
        await reset_db(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(conn, T0, notes=r"C:\Users\driver\route")
            await _insert_trip(conn, T0 + timedelta(hours=1), notes="Unrelated route notes")

        response = await _archive(pool, q=r"Users\driver")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_search_term_matches_a_literal_backslash():
    asyncio.run(_backslash_wildcard_scenario())


# --- The label OR-branches use the same `_escape_ilike_term`/`ESCAPE '\'`
# treatment as every other search target above, so wildcard characters in a
# label are matched literally rather than as ILIKE metacharacters. ---

async def _label_percent_wildcard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            target_id = await _insert_trip(
                conn, T0, start_lat=None, start_lon=None, start_label="50% Off Storage"
            )
            await _insert_trip(
                conn, T0 + timedelta(hours=1), start_lat=None, start_lon=None,
                start_label="No percent sign here",
            )

        # Unescaped, "%" is the ILIKE wildcard and would match everything.
        response = await _archive(pool, q="%")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_label_search_matches_a_literal_percent_sign():
    asyncio.run(_label_percent_wildcard_scenario())


async def _label_underscore_wildcard_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            # Unescaped, "_" matches any single character, so an unescaped
            # search for "a1_b2" would also match "a1Xb2".
            target_id = await _insert_trip(
                conn, T0, end_lat=None, end_lon=None, end_label="Bay a1_b2"
            )
            await _insert_trip(
                conn, T0 + timedelta(hours=1), end_lat=None, end_lon=None,
                end_label="Bay a1Xb2",
            )

        response = await _archive(pool, q="a1_b2")
        assert _trip_ids(response) == {target_id}
    finally:
        await pool.close()


def test_label_search_matches_a_literal_underscore():
    asyncio.run(_label_underscore_wildcard_scenario())


# --- B4.2: empty/whitespace-only term is no search (acceptance criteria
# 5 and 9: the response contract, including exact URLs, is unchanged) ---

async def _empty_and_whitespace_term_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
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

        # No active search: the archive URL and export/review links carry no
        # `q` param at all, matching the exact URLs built before this filter
        # existed.
        assert "q=" not in unfiltered.context["archive_state"]["url"]
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
        await reset_db(pool)
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


async def _month_aggregate_exclusion_scenario():
    # _MONTH_AGGREGATE_COLUMNS_SQL is shared, by name, between this route's
    # month build and the inline-classify rollup's single-month recompute
    # (see app/ui/trips.py); neither call site has its own dedicated
    # DB-backed check that a Not My Vehicle trip drops out of the month's
    # total distance, or that any excluded trip drops out of its business
    # distance, so a regression in that shared SQL would slip past every
    # other test in the suite.
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            counted_id = await _insert_trip(
                conn, T0, category="business", notes="Counted",
            )
            not_my_vehicle_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), category="business",
                notes="Not mine", exclusion="not_my_vehicle",
            )
            not_deductible_id = await _insert_trip(
                conn, T0 + timedelta(hours=2), category="business",
                notes="Someone else drove", exclusion="not_deductible",
            )

        response = await _archive(pool)
        months_by_key = {(m["year"], m["month_num"]): m for m in response.context["months"]}
        july = months_by_key[(2026, 7)]

        assert july["trip_count"] == 3
        # total_m excludes only Not My Vehicle trips (not this device's own
        # miles), so the not_deductible trip still counts toward it.
        assert july["total_m"] == pytest.approx(2000.0)
        # business_m excludes every excluded trip regardless of category,
        # so only the one plain business trip counts.
        assert july["business_m"] == pytest.approx(1000.0)
        assert _trip_ids(response) == {counted_id, not_my_vehicle_id, not_deductible_id}
    finally:
        await pool.close()


def test_month_aggregate_excludes_not_my_vehicle_from_total_and_any_exclusion_from_business():
    asyncio.run(_month_aggregate_exclusion_scenario())


async def _archive_list_parity_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            vehicle_id = await _insert_vehicle(conn, "Archive car")
            july_id = await _insert_trip(
                conn, T0, notes="Filtered list", category="business",
                vehicle_id=vehicle_id, exclusion="not_deductible",
            )
            personal_id = await _insert_trip(
                conn, T0 + timedelta(hours=1), notes="Other category", category="personal",
                vehicle_id=vehicle_id,
            )
            unassigned_id = await _insert_trip(
                conn, T0 + timedelta(hours=2), notes="Unassigned", category="business",
            )
            await _insert_trip(
                conn, T0 + timedelta(days=31), notes="Outside date", category="business",
                vehicle_id=vehicle_id, exclusion="not_deductible",
            )

        def assert_parity(full, partial, expected_ids):
            full_month = full.context["months"]
            partial_month = partial.context["months"]
            assert [m["label"] for m in partial_month] == [m["label"] for m in full_month]
            assert {
                trip["id"] for month in partial_month for trip in month["trips"]
            } == expected_ids
            assert {
                trip["id"] for month in full_month for trip in month["trips"]
            } == expected_ids
            assert [m["trip_count"] for m in partial_month] == [
                m["trip_count"] for m in full_month
            ]
            assert [m["next_url"] for m in partial_month] == [
                m["next_url"] for m in full_month
            ]
            assert partial.context["ytd_year"] == full.context["ytd_year"]
            assert partial.context["ytd_deduction"] == full.context["ytd_deduction"]

        full = await _archive(
            pool, from_="2026-07-01", to="2026-07-31", vehicle=str(vehicle_id),
            exclusion="not_deductible",
        )
        partial = await _archive_list(
            pool, from_="2026-07-01", to="2026-07-31", vehicle=str(vehicle_id),
            exclusion="not_deductible",
        )
        assert_parity(full, partial, {july_id})

        full = await _archive(pool, vehicle="none")
        partial = await _archive_list(pool, vehicle="none")
        assert_parity(full, partial, {unassigned_id})

        full = await _archive(pool, from_="2026-07-01", to="2026-07-31")
        partial = await _archive_list(pool, from_="2026-07-01", to="2026-07-31")
        assert_parity(full, partial, {july_id, personal_id, unassigned_id})

        # A filtered YTD figure remains the current-year whole-archive value,
        # even when the visible month set is narrowed to one trip.
        unfiltered = await _archive(pool)
        assert partial.context["ytd_year"] == unfiltered.context["ytd_year"]
        assert partial.context["ytd_deduction"] == unfiltered.context["ytd_deduction"]

        assert "from=2026-07-01" in partial.context["export_url"]("csv")
        assert "to=2026-07-31" in partial.context["export_url"]("csv")
        assert 'id="trip-archive-results"' in partial.body.decode()
        assert 'id="trip-archive-export-links"' in partial.body.decode()
        assert 'id="trip-archive-ytd" hx-swap-oob="outerHTML"' in partial.body.decode()
    finally:
        await pool.close()


def test_partial_archive_matches_full_page_rows_summaries_and_exports():
    asyncio.run(_archive_list_parity_scenario())


async def _archive_list_empty_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        response = await _archive_list(pool, q="does-not-exist")
        assert response.context["months"] == []
        body = response.body.decode()
        assert "No trips match the current filters." in body
        assert 'id="trip-archive-ytd" hx-swap-oob="outerHTML"' in body
    finally:
        await pool.close()


def test_partial_archive_distinguishes_empty_filtered_result():
    asyncio.run(_archive_list_empty_scenario())


async def _archive_list_no_page_only_lookup_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    old_recent_purposes = trips_ui._fetch_recent_purposes

    async def fail(*args, **kwargs):
        raise AssertionError("page-only lookup ran during archive list refresh")

    trips_ui._fetch_recent_purposes = fail
    try:
        await reset_db(pool)
        response = await _archive_list(pool)
        assert response.context["months"] == []
    finally:
        trips_ui._fetch_recent_purposes = old_recent_purposes
        await pool.close()


def test_partial_archive_skips_full_page_recent_purpose_lookup():
    asyncio.run(_archive_list_no_page_only_lookup_scenario())


async def _archive_list_depth_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            july_ids = [
                await _insert_trip(conn, T0 + timedelta(hours=offset), notes=f"July {offset}")
                for offset in range(3)
            ]
            august_ids = [
                await _insert_trip(
                    conn, T0 + timedelta(days=31, hours=offset), notes=f"August {offset}"
                )
                for offset in range(3)
            ]

        response = await _archive_list(
            pool, page_size=1,
            loaded_depth=json.dumps({"2026-07": 2, "2026-08": 1}),
        )
        months = {(m["year"], m["month_num"]): m for m in response.context["months"]}
        july = months[(2026, 7)]
        august = months[(2026, 8)]
        assert [trip["id"] for trip in july["trips"]] == [july_ids[2], july_ids[1]]
        assert [trip["id"] for trip in august["trips"]] == [august_ids[2]]
        assert july["loaded_depth"] == 2 and august["loaded_depth"] == 1
        assert july["has_more"] is True and august["has_more"] is True
        assert "offset=2" in july["next_url"]
        assert "offset=1" in august["next_url"]

        july_page = await _month_page(pool, 2026, 7, offset=2, page_size=1)
        august_page = await _month_page(pool, 2026, 8, offset=1, page_size=1)
        assert [trip["id"] for trip in july_page.context["trips"]] == [july_ids[0]]
        assert [trip["id"] for trip in august_page.context["trips"]] == [august_ids[1]]
        assert {
            trip["id"] for trip in july["trips"] + july_page.context["trips"]
        } == set(july_ids)
        assert {
            trip["id"] for trip in august["trips"] + august_page.context["trips"]
        } == {august_ids[2], august_ids[1]}
        assert not (
            {trip["id"] for trip in july["trips"]}
            & {trip["id"] for trip in july_page.context["trips"]}
        )
        assert not (
            {trip["id"] for trip in august["trips"]}
            & {trip["id"] for trip in august_page.context["trips"]}
        )
    finally:
        await pool.close()


def test_partial_archive_keeps_validated_loaded_month_depth_and_next_offset():
    asyncio.run(_archive_list_depth_scenario())
