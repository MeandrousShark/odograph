"""DB-backed tests that NaN/Infinity are rejected at every numeric form
handler that used to let them through a plain `<= 0` guard, and that an
out-of-range place lat/lon is rejected rather than silently coerced by the
geography cast (see app/validation.py's parse_finite_number). Route handlers
are invoked directly, same convention tests/test_odometer_db.py uses:
`dependencies=[Depends(require_csrf)]` is a router-level concern the ASGI app
enforces, not something calling the Python function directly exercises, so
these tests skip straight past it.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.db import make_pool, run_migrations
from app.main import make_templates
from app.ui import make_router

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")

TZ = ZoneInfo("America/Los_Angeles")
USER = {"sub": "test"}

NON_FINITE = [float("nan"), float("inf"), float("-inf")]


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} missing")


def _request(pool):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            pool=pool,
            config=SimpleNamespace(display_tz=TZ),
            templates=make_templates(SimpleNamespace(display_tz=TZ, app_version="test")),
        )),
        session={"csrf": "token"},
        headers={},
    )


async def _reset_schema(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await run_migrations(pool)


async def _create_vehicle(conn, name: str) -> int:
    cur = await conn.execute("INSERT INTO vehicles (name) VALUES (%s) RETURNING id", (name,))
    return (await cur.fetchone())[0]


@pytest.mark.parametrize("bad_value", NON_FINITE)
def test_add_odometer_reading_rejects_non_finite_value(bad_value):
    asyncio.run(_odometer_non_finite_scenario(bad_value))


async def _odometer_non_finite_scenario(bad_value):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
        add = _endpoint("/settings/odometer")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await add(
                request, vehicle_id=truck_id, date="2026-01-01", time="00:00",
                value=bad_value, note="", user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM odometer_readings WHERE vehicle_id = %s", (truck_id,)
            )
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


def test_add_odometer_reading_accepts_valid_value():
    asyncio.run(_odometer_valid_scenario())


async def _odometer_valid_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        async with pool.connection() as conn:
            truck_id = await _create_vehicle(conn, "Truck")
        add = _endpoint("/settings/odometer")
        request = _request(pool)
        await add(
            request, vehicle_id=truck_id, date="2026-01-01", time="00:00",
            value=1000.0, note="", user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM odometer_readings WHERE vehicle_id = %s", (truck_id,)
            )
            (count,) = await cur.fetchone()
        assert count == 1
    finally:
        await pool.close()


@pytest.mark.parametrize("bad_value", NON_FINITE)
def test_upsert_rate_rejects_non_finite_rate(bad_value):
    asyncio.run(_rate_non_finite_scenario(bad_value))


async def _rate_non_finite_scenario(bad_value):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        upsert = _endpoint("/settings/rates")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await upsert(
                request, year=2099, rate_per_mi=bad_value, mid_year="", rate_h2_per_mi="",
                h2_start_month=7, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            # 2099 isn't among the seeded years, so a leaked row is unambiguous.
            cur = await conn.execute("SELECT count(*) FROM mileage_rates WHERE year = 2099")
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


@pytest.mark.parametrize("bad_value", NON_FINITE)
def test_upsert_rate_rejects_non_finite_h2_rate(bad_value):
    asyncio.run(_rate_h2_non_finite_scenario(bad_value))


async def _rate_h2_non_finite_scenario(bad_value):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        upsert = _endpoint("/settings/rates")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await upsert(
                request, year=2099, rate_per_mi=0.7, mid_year="1",
                rate_h2_per_mi=repr(bad_value), h2_start_month=7, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM mileage_rates WHERE year = 2099")
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


def test_upsert_rate_accepts_valid_flat_and_midyear_split():
    asyncio.run(_rate_valid_scenario())


async def _rate_valid_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        upsert = _endpoint("/settings/rates")
        request = _request(pool)
        await upsert(
            request, year=2025, rate_per_mi=0.70, mid_year="", rate_h2_per_mi="",
            h2_start_month=7, user=USER,
        )
        await upsert(
            request, year=2022, rate_per_mi=0.585, mid_year="1", rate_h2_per_mi="0.625",
            h2_start_month=7, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT year, rate_per_mi, rate_h2_per_mi, h2_start_month FROM mileage_rates "
                "WHERE year IN (2022, 2025) ORDER BY year"
            )
            rows = await cur.fetchall()
        # rate_per_mi/rate_h2_per_mi are numeric columns, so psycopg returns
        # Decimal; comparing against a Decimal built from a float literal
        # (0.585) rather than a string can fail on the binary float's
        # rounding, so compare via float() instead.
        floated = [(year, float(r1), float(r2) if r2 is not None else None, m) for year, r1, r2, m in rows]
        assert floated == [
            (2022, pytest.approx(0.585), pytest.approx(0.625), 7),
            (2025, pytest.approx(0.70), None, None),
        ]
    finally:
        await pool.close()


@pytest.mark.parametrize("bad_value", NON_FINITE)
def test_create_place_rejects_non_finite_radius(bad_value):
    asyncio.run(_place_create_non_finite_radius_scenario(bad_value))


async def _place_create_non_finite_radius_scenario(bad_value):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await create(
                request, name="Home", kind="home", lat=47.6, lon=-122.3,
                radius_m=bad_value, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places")
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


@pytest.mark.parametrize("bad_value", NON_FINITE)
def test_create_place_rejects_non_finite_lat(bad_value):
    asyncio.run(_place_create_non_finite_lat_scenario(bad_value))


async def _place_create_non_finite_lat_scenario(bad_value):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await create(
                request, name="Home", kind="home", lat=bad_value, lon=-122.3,
                radius_m=150.0, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places")
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


def test_create_place_rejects_out_of_range_lat():
    asyncio.run(_place_create_out_of_range_lat_scenario())


async def _place_create_out_of_range_lat_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await create(
                request, name="Home", kind="home", lat=95.0, lon=-122.3,
                radius_m=150.0, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places")
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


def test_create_place_rejects_out_of_range_lon():
    asyncio.run(_place_create_out_of_range_lon_scenario())


async def _place_create_out_of_range_lon_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        request = _request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await create(
                request, name="Home", kind="home", lat=47.6, lon=185.0,
                radius_m=150.0, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places")
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await pool.close()


def test_create_place_accepts_valid_input():
    asyncio.run(_place_create_valid_scenario())


async def _place_create_valid_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        request = _request(pool)
        await create(
            request, name="Home", kind="home", lat=47.6, lon=-122.3,
            radius_m=150.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM places WHERE name = 'Home'")
            (count,) = await cur.fetchone()
        assert count == 1
    finally:
        await pool.close()


@pytest.mark.parametrize("bad_value", NON_FINITE)
def test_update_place_rejects_non_finite_radius(bad_value):
    asyncio.run(_place_update_non_finite_radius_scenario(bad_value))


async def _place_update_non_finite_radius_scenario(bad_value):
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        update = _endpoint("/places/{place_id}/update")
        request = _request(pool)
        await create(
            request, name="Home", kind="home", lat=47.6, lon=-122.3,
            radius_m=150.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id, radius_m FROM places WHERE name = 'Home'")
            place_id, original_radius = await cur.fetchone()

        with pytest.raises(HTTPException) as exc_info:
            await update(
                request, place_id=place_id, name="Home", kind="home", lat=47.6, lon=-122.3,
                radius_m=bad_value, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT radius_m FROM places WHERE id = %s", (place_id,))
            (radius_m,) = await cur.fetchone()
        assert radius_m == original_radius
    finally:
        await pool.close()


def test_update_place_rejects_out_of_range_lat():
    asyncio.run(_place_update_out_of_range_lat_scenario())


async def _place_update_out_of_range_lat_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        update = _endpoint("/places/{place_id}/update")
        request = _request(pool)
        await create(
            request, name="Home", kind="home", lat=47.6, lon=-122.3,
            radius_m=150.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, ST_Y(geom::geometry) FROM places WHERE name = 'Home'"
            )
            place_id, original_lat = await cur.fetchone()

        with pytest.raises(HTTPException) as exc_info:
            await update(
                request, place_id=place_id, name="Home", kind="home", lat=95.0, lon=-122.3,
                radius_m=150.0, user=USER,
            )
        assert exc_info.value.status_code == 400
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT ST_Y(geom::geometry) FROM places WHERE id = %s", (place_id,)
            )
            (lat,) = await cur.fetchone()
        assert lat == pytest.approx(original_lat)
    finally:
        await pool.close()


def test_update_place_accepts_valid_input():
    asyncio.run(_place_update_valid_scenario())


async def _place_update_valid_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await _reset_schema(pool)
        create = _endpoint("/places")
        update = _endpoint("/places/{place_id}/update")
        request = _request(pool)
        await create(
            request, name="Home", kind="home", lat=47.6, lon=-122.3,
            radius_m=150.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM places WHERE name = 'Home'")
            (place_id,) = await cur.fetchone()

        await update(
            request, place_id=place_id, name="Home", kind="work", lat=47.7, lon=-122.4,
            radius_m=200.0, user=USER,
        )
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT kind::text, radius_m, ST_Y(geom::geometry), ST_X(geom::geometry) "
                "FROM places WHERE id = %s", (place_id,)
            )
            row = await cur.fetchone()
        assert row[0] == "work"
        assert row[1] == pytest.approx(200.0)
        assert row[2] == pytest.approx(47.7)
        assert row[3] == pytest.approx(-122.4)
    finally:
        await pool.close()
