"""DB-backed tests for reverse geocoding: `GeocodeWorker`'s discovery
query and `TRIP_COLUMNS`' address subselects.

Like tests/test_snap_db.py, this needs a real Postgres+PostGIS and is
skipped unless TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.rows import dict_row

from app.db import make_pool
from app.geocode import GeoapifyProvider, GeocodeWorker
from app.ui import TRIP_COLUMNS
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "TESTDEV"
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)


async def _insert_trip(
    conn, started_at, ended_at, lat, lon, end_lat=None, end_lon=None,
    start_place_id=None, end_place_id=None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO trips (account_id, tracking_device_id, device, source, started_at, ended_at, distance_m, "
        " point_count, detector_version, "
        " start_geom, end_geom, start_place_id, end_place_id) "
        "VALUES (%s, %s, %s, 'detected', %s, %s, 1000, 2, 2, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
        " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s) RETURNING id",
        (
            account_id(conn), 1, DEVICE, started_at, ended_at, lon, lat,
            end_lon if end_lon is not None else lon,
            end_lat if end_lat is not None else lat,
            start_place_id, end_place_id,
        ),
    )
    return (await cur.fetchone())[0]


class _FakeHTTP:
    """Stands in for httpx.AsyncClient in GeocodeWorker tests -- no real
    network call, just enough of the interface GeoapifyProvider.reverse() uses.
    """

    def __init__(self, address: str | None):
        self.address = address
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        features = (
            [{"properties": {"formatted": self.address}}] if self.address else []
        )
        return _FakeResponse({"type": "FeatureCollection", "features": features})


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


async def _scenario():
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await seed_tracking_device(conn, DEVICE, device_id=1)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO places (account_id, name, kind, geom, radius_m) "
                "VALUES (%s, 'Home', 'home', "
                "ST_SetSRID(ST_MakePoint(-122.1000, 47.1000), 4326)::geography, 150)", (account_id(conn),)
            )
            place_id_row = await conn.execute("SELECT id FROM places WHERE name = 'Home'")
            place_id = (await place_id_row.fetchone())[0]

            # 1: unnamed, un-cached endpoint -> should be found by discovery.
            trip_needs_geocode = await _insert_trip(
                conn, T0, T0 + timedelta(minutes=10), 47.6031, -122.3301
            )
            # 2: named place at both ends -> excluded from discovery.
            await _insert_trip(
                conn, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=10),
                47.1000, -122.1000, start_place_id=place_id, end_place_id=place_id,
            )
            # 3: already-cached coordinate -> excluded from discovery.
            trip_already_cached = await _insert_trip(
                conn, T0 + timedelta(hours=2), T0 + timedelta(hours=2, minutes=10),
                47.5000, -122.5000,
            )
            await conn.execute(
                "INSERT INTO geocode_cache (account_id, lat, lon, address) VALUES (%s, %s, %s, %s)",
                (account_id(conn), 47.5000, -122.5000, "Cached Ave"),
            )
            # 4: a cached NULL (a previous miss) -> also excluded, not retried.
            trip_cached_miss = await _insert_trip(
                conn, T0 + timedelta(hours=3), T0 + timedelta(hours=3, minutes=10),
                47.9000, -122.9000,
            )
            await conn.execute(
                "INSERT INTO geocode_cache (account_id, lat, lon, address) VALUES (%s, %s, %s, NULL)",
                (account_id(conn), 47.9000, -122.9000),
            )

        provider = GeoapifyProvider(api_key="fake-key", omit_country="United States of America")
        worker = GeocodeWorker(pool, None, provider, 0.0)
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT lat, lon FROM (
                    SELECT DISTINCT ROUND(ST_Y(start_geom::geometry)::numeric, 4) AS lat,
                                    ROUND(ST_X(start_geom::geometry)::numeric, 4) AS lon
                    FROM trips WHERE start_place_id IS NULL AND start_geom IS NOT NULL
                    UNION
                    SELECT DISTINCT ROUND(ST_Y(end_geom::geometry)::numeric, 4),
                                    ROUND(ST_X(end_geom::geometry)::numeric, 4)
                    FROM trips WHERE end_place_id IS NULL AND end_geom IS NOT NULL
                ) endpoints
                EXCEPT
                SELECT lat, lon FROM geocode_cache
                """
            )
            pending = {(float(r[0]), float(r[1])) for r in await cur.fetchall()}

        assert (47.6031, -122.3301) in pending, "unnamed, un-cached endpoint must be discovered"
        assert (47.5000, -122.5000) not in pending, "already-cached hit must not be re-queried"
        assert (47.9000, -122.9000) not in pending, "a cached miss (NULL) must not be re-queried"
        assert (47.1000, -122.1000) not in pending, "a named place's coordinate is never geocoded"

        # run_once() end-to-end: writes a real cache row for the pending coordinate.
        fake_http = _FakeHTTP("123 Real St")
        worker.http = fake_http
        await worker.run_once()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT address FROM geocode_cache WHERE lat = 47.6031 AND lon = -122.3301"
            )
            row = await cur.fetchone()
        assert row is not None and row[0] == "123 Real St"

        # TRIP_COLUMNS resolves start_address once the cache row exists.
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (trip_needs_geocode,))
            trip = await cur.fetchone()
        assert trip["start_address"] == "123 Real St"

        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (trip_already_cached,))
            trip2 = await cur.fetchone()
        assert trip2["start_address"] == "Cached Ave"

        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {TRIP_COLUMNS} FROM trips WHERE id = %s", (trip_cached_miss,))
            trip3 = await cur.fetchone()
        assert trip3["start_address"] is None
    finally:
        await raw_pool.close()


def test_geocode_discovery_and_trip_columns():
    asyncio.run(_scenario())
