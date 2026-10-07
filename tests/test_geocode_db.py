"""DB-backed tests for reverse geocoding: `GeocodeWorker`'s discovery
query and `TRIP_COLUMNS`' address subselects.

Like tests/test_snap_db.py, this needs a real Postgres+PostGIS and is
skipped unless TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from psycopg.rows import dict_row

from app.db import make_pool
from app.geocode import GeoapifyProvider, GeocodeWorker
from app.storage import storage_status
from app.ui import TRIP_COLUMNS
from conftest import reset_account_db, seed_tracking_device
from app.account_context import account_id

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

DEVICE = "TESTDEV"
T0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)


def test_geocode_batch_counts_retryable_failures_and_completed_cache_rows():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
                await _insert_trip(conn, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=1),
                                   47.2, -122.2)

            class Provider:
                failed = True

                async def reverse(self, client, lat, lon):
                    if lat == 47.1 and self.failed:
                        raise httpx.ConnectError("private provider URL")
                    return None

            provider = Provider()
            worker = GeocodeWorker(pool, None, provider, 0)
            mixed = await worker.run_once()
            assert (mixed.attempted, mixed.completed, mixed.retriable_failures,
                    mixed.failure_type) == (2, 1, 1, "ConnectError")
            provider.failed = False
            assert (await worker.run_once()).attempted == 0
            async with pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT failure_count,failure_reason::text,next_attempt_at-attempted_at "
                    "FROM geocode_retry WHERE account_id=%s", (account_id(conn),)
                )).fetchone()
                assert row == (1, 'transport', timedelta(seconds=60))
                await conn.execute("UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s",
                                   (account_id(conn),))
            success = await worker.run_once()
            empty = await worker.run_once()
            assert (success.attempted, success.completed, success.retriable_failures) == (1, 1, 0)
            assert (empty.attempted, empty.completed, empty.retriable_failures) == (0, 0, 0)
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT count(*) FROM geocode_cache WHERE account_id=%s", (account_id(conn),)
                )).fetchone())[0] == 2
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_source_disappearing_before_provider_call_is_not_attempted():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)

            class Provider:
                async def reverse(self, client, lat, lon):
                    pytest.fail("missing source reached provider")

            outcome = await GeocodeWorker(pool, None, Provider(), 0)._geocode_one(47.1, -122.1)
            assert (outcome.attempted, outcome.completed, outcome.retriable_failures) == (0, 0, 0)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_capacity_pause_retains_retry_and_resumes_without_repeat_calls():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                owner = account_id(conn)
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE storage_grants SET enhancement_limit_bytes=128 WHERE account_id=%s",
                    (owner,),
                )

            class Provider:
                calls = 0

                async def reverse(self, client, lat, lon):
                    self.calls += 1
                    return "A"

            provider = Provider()
            worker = GeocodeWorker(pool, None, provider, 0)
            first = await worker._geocode_one(47.1, -122.1)
            assert first.attempted == 1 and first.completed == 0
            assert provider.calls == 1
            async with pool.connection() as conn:
                retry = await (await conn.execute(
                    "SELECT capacity_paused,capacity_needed_bytes FROM geocode_retry "
                    "WHERE account_id=%s AND rounded_lat=47.1 AND rounded_lon=-122.1",
                    (owner,),
                )).fetchone()
                assert retry == (True, 129)
                await conn.execute(
                    "UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s", (owner,),
                )
            pause = None
            for _ in range(3):
                pause = await worker.run_turn()
                assert provider.calls == 1
                if pause.deferred_until is not None:
                    break
            assert pause.deferred_until is not None

            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE storage_grants SET enhancement_limit_bytes=129 WHERE account_id=%s",
                    (owner,),
                )
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s", (owner,),
                )
            completed = None
            for _ in range(3):
                completed = await worker.run_turn()
                if completed.batch.completed:
                    break
            assert completed.batch.completed == 1
            assert provider.calls == 2
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT address FROM geocode_cache WHERE account_id=%s AND lat=47.1 AND lon=-122.1",
                    (owner,),
                )).fetchone()) == ("A",)
                assert (await (await conn.execute(
                    "SELECT count(*) FROM geocode_retry WHERE account_id=%s", (owner,),
                )).fetchone())[0] == 0
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_preflight_pause_is_visible_and_resumes_without_provider_probe():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                owner = account_id(conn)
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
                usage_before = await (await conn.execute(
                    "SELECT actual_bytes,reserved_bytes,enhancement_bytes "
                    "FROM account_usage WHERE account_id=%s", (owner,),
                )).fetchone()
            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE storage_grants SET enhancement_limit_bytes=1 WHERE account_id=%s",
                    (owner,),
                )

            class Provider:
                calls = 0

                async def reverse(self, client, lat, lon):
                    self.calls += 1
                    return None

            provider = Provider()
            worker = GeocodeWorker(pool, None, provider, 0)
            paused = await worker._geocode_one(47.1, -122.1)
            assert paused.attempted == 0 and provider.calls == 0
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT capacity_paused,capacity_needed_bytes FROM geocode_retry "
                    "WHERE account_id=%s AND rounded_lat=47.1 AND rounded_lon=-122.1",
                    (owner,),
                )).fetchone()) == (True, 128)
                assert (await storage_status(conn))["enhancement_paused"]
                assert await (await conn.execute(
                    "SELECT actual_bytes,reserved_bytes,enhancement_bytes "
                    "FROM account_usage WHERE account_id=%s", (owner,),
                )).fetchone() == usage_before

            async with raw.connection() as conn:
                await conn.execute(
                    "UPDATE storage_grants SET enhancement_limit_bytes=256 WHERE account_id=%s",
                    (owner,),
                )
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s", (owner,),
                )
            for _ in range(4):
                turn = await worker.run_turn()
                if turn.batch.completed:
                    break
            assert turn.batch.completed == 1 and provider.calls == 1
            await worker._capacity_paused()
            async with pool.connection() as conn:
                assert not (await storage_status(conn))["enhancement_paused"]
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_oversized_utf8_provider_address_is_retried_without_caching_a_miss():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                owner = account_id(conn)
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)

            class Provider:
                address = "é" * 2049

                async def reverse(self, client, lat, lon):
                    return self.address

            provider = Provider()
            worker = GeocodeWorker(pool, None, provider, 0)
            failed = await worker._geocode_one(47.1, -122.1)
            assert failed.retriable_failures == 1 and failed.completed == 0
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT count(*) FROM geocode_cache WHERE account_id=%s", (owner,),
                )).fetchone())[0] == 0
                assert (await (await conn.execute(
                    "SELECT failure_reason::text FROM geocode_retry WHERE account_id=%s",
                    (owner,),
                )).fetchone()) == ("parse",)
                await conn.execute(
                    "UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s", (owner,),
                )

            provider.address = "é" * 2048
            assert (await worker._geocode_one(47.1, -122.1)).completed == 1
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT address FROM geocode_cache WHERE account_id=%s", (owner,),
                )).fetchone()) == (provider.address,)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_capacity_race_can_pause_when_discovery_turn_byte_cannot_grow():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                owner = account_id(conn)
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            worker = GeocodeWorker(pool, None, None, 0)
            await worker._discover()
            async with raw.connection() as conn:
                used = (await (await conn.execute(
                    "SELECT actual_bytes+reserved_bytes FROM account_usage WHERE account_id=%s",
                    (owner,),
                )).fetchone())[0]
                await conn.execute(
                    "UPDATE storage_grants SET account_limit_bytes=%s,raw_limit_bytes=1,"
                    "enhancement_limit_bytes=129 WHERE account_id=%s",
                    (used, owner),
                )

            class Provider:
                calls = 0

                async def reverse(self, client, lat, lon):
                    self.calls += 1
                    return "A"

            provider = Provider()
            worker.provider = provider
            result = await worker._geocode_one(47.1, -122.1)
            assert result.attempted == 1 and result.completed == 0
            assert provider.calls == 1
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT capacity_paused,capacity_needed_bytes FROM geocode_retry "
                    "WHERE account_id=%s AND rounded_lat=47.1 AND rounded_lon=-122.1",
                    (owner,),
                )).fetchone()) == (True, 129)
                assert (await (await conn.execute(
                    "SELECT count(*) FROM geocode_cache WHERE account_id=%s", (owner,),
                )).fetchone())[0] == 0
        finally:
            await raw.close()
    asyncio.run(scenario())


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
    trip_id = (await cur.fetchone())[0]
    # Detector-owned fixtures retain the fixes funding their core output.
    for recorded_at, point_lat, point_lon in (
        (started_at, lat, lon),
        (ended_at, end_lat if end_lat is not None else lat,
         end_lon if end_lon is not None else lon),
    ):
        await conn.execute(
            "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, geom, trip_id) "
            "VALUES (%s, 1, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
            (account_id(conn), DEVICE, recorded_at, point_lon, point_lat, trip_id),
        )
    return trip_id


class _FakeHTTP(httpx.AsyncClient):
    """Mock transport exercises the same streaming API as serving requests."""

    def __init__(self, address: str | None):
        self.address = address
        self.calls = 0
        super().__init__(transport=httpx.MockTransport(self._response))

    def _response(self, request):
        self.calls += 1
        features = (
            [{"properties": {"formatted": self.address}}] if self.address else []
        )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})


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
        await fake_http.aclose()
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


def test_geocode_turn_failure_does_not_defer_other_coordinates_after_restart():
    from app.provider_pacing import ProviderPacer
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                for offset in range(3):
                    await _insert_trip(conn, T0 + timedelta(hours=offset),
                        T0 + timedelta(hours=offset, minutes=1), 47.1 + offset / 10, -122.1)
            pacer = ProviderPacer(0)
            class Provider:
                calls = []
                async def reverse(self, client, lat, lon):
                    self.calls.append(lat)
                    if lat == 47.1:
                        raise ValueError("malformed provider response")
                    return None
            provider = Provider()
            outcomes = []
            for _ in range(5):
                # Production reconstructs workers every turn; durable progress
                # must survive that, including discovery's alternating priority.
                worker = GeocodeWorker(pool, None, provider, 0, pacer=pacer)
                ticket = await pacer.wait_ready()
                try:
                    outcomes.append(await worker.run_turn(ticket))
                finally:
                    ticket.close()
            assert provider.calls == [47.1, 47.2, 47.3]
            assert sum(o.batch.completed for o in outcomes) == 2
            assert sum(o.batch.retriable_failures for o in outcomes) == 1
            assert outcomes[-1].deferred_until > asyncio.get_running_loop().time()
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM geocode_cache")).fetchone())[0] == 2
                assert (await (await conn.execute(
                    "SELECT failure_count,failure_reason::text FROM geocode_retry"
                )).fetchone()) == (1, 'parse')
        finally:
            await raw.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['endpoint', 'device_cycle', 'note', 'unrelated_endpoint'])
def test_geocode_generation_revalidation_preserves_only_current_results(mutation):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            class Provider:
                async def reverse(self, client, lat, lon):
                    async with pool.connection() as conn:
                        if mutation == 'endpoint':
                            await conn.execute(
                                "UPDATE trips SET start_geom=ST_SetSRID(ST_MakePoint(-122.2,47.2),4326)::geography "
                                "WHERE account_id=%s AND id=%s", (account_id(conn), trip),
                            )
                        elif mutation == 'device_cycle':
                            await conn.execute("UPDATE tracking_devices SET enabled=false WHERE account_id=%s",
                                               (account_id(conn),))
                            await conn.execute("UPDATE tracking_devices SET enabled=true WHERE account_id=%s",
                                               (account_id(conn),))
                        elif mutation == 'unrelated_endpoint':
                            await _insert_trip(conn, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=1),
                                               47.2, -122.2)
                        else:
                            await conn.execute("UPDATE trips SET notes='human edit' WHERE account_id=%s AND id=%s",
                                               (account_id(conn), trip))
                    return 'Current address'
            outcome = await GeocodeWorker(pool, None, Provider(), 0, batch_size=1).run_once()
            assert outcome.attempted == 1
            async with pool.connection() as conn:
                cache = await (await conn.execute("SELECT address FROM geocode_cache")).fetchall()
                assert cache == ([('Current address',)] if mutation in ('note', 'unrelated_endpoint') else [])
                if mutation in ('endpoint', 'device_cycle'):
                    assert (await (await conn.execute(
                        "SELECT failure_reason::text FROM geocode_retry WHERE rounded_lat=47.1"
                    )).fetchone()) == ('source_changed',)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_cancellation_keeps_coordinate_and_restart_caches_genuine_null():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            started = asyncio.Event()
            class Provider:
                async def reverse(self, client, lat, lon):
                    started.set()
                    await asyncio.Future()
            task = asyncio.create_task(GeocodeWorker(pool, None, Provider(), 0).run_once())
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT attempted_at,failure_count FROM geocode_retry"
                )).fetchone()) == (None, 0)
            class Miss:
                calls = 0
                async def reverse(self, client, lat, lon):
                    self.calls += 1
                    return None
            provider = Miss()
            worker = GeocodeWorker(pool, None, provider, 0)
            assert (await worker.run_once()).completed == 1
            assert (await worker.run_once()).attempted == 0
            assert provider.calls == 1
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT address FROM geocode_cache")).fetchone()) == (None,)
                assert (await (await conn.execute("SELECT count(*) FROM geocode_retry")).fetchone()) == (0,)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_retry_is_capped_and_orphan_removed_without_http():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            class Failure:
                async def reverse(self, client, lat, lon):
                    raise httpx.ConnectError('unavailable')
            worker = GeocodeWorker(pool, None, Failure(), 0)
            for attempt, delay in enumerate([60, 120, 240, 480, 960, 1920, 3600, 3600], 1):
                assert (await worker.run_once()).retriable_failures == 1
                async with pool.connection() as conn:
                    assert (await (await conn.execute(
                        "SELECT failure_count,next_attempt_at-attempted_at FROM geocode_retry"
                    )).fetchone()) == (attempt, timedelta(seconds=delay))
                    await conn.execute("UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s",
                                       (account_id(conn),))
            async with pool.connection() as conn:
                await conn.execute("UPDATE geocode_retry SET failure_count=31 WHERE account_id=%s",
                                   (account_id(conn),))
            assert (await worker.run_once()).retriable_failures == 1
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT failure_count FROM geocode_retry")).fetchone()) == (31,)
                await conn.execute("DELETE FROM trips WHERE account_id=%s AND id=%s", (account_id(conn), trip))
                await conn.execute("UPDATE geocode_retry SET next_attempt_at=now() WHERE account_id=%s",
                                   (account_id(conn),))
            class Unused:
                async def reverse(self, client, lat, lon):
                    pytest.fail('orphan reached HTTP')
            cleaned = await GeocodeWorker(pool, None, Unused(), 0).run_once()
            assert (cleaned.attempted, cleaned.completed) == (0, 1)
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM geocode_retry")).fetchone()) == (0,)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_discovery_pages_alternate_with_due_coordinates_and_resume_after_restart():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,point_count,"
                    "start_geom,end_geom) SELECT %s,'manual','manual',%s,%s,100,0,"
                    "ST_SetSRID(ST_MakePoint(-122,47+i::float/10000),4326)::geography,NULL "
                    "FROM generate_series(1,1001) i", (account_id(conn), T0, T0 + timedelta(minutes=1)),
                )
                # Simulate pre-existing history without immediate endpoint intents.
                await conn.execute("DELETE FROM geocode_retry WHERE account_id=%s", (account_id(conn),))
            class Provider:
                calls = 0
                async def reverse(self, client, lat, lon):
                    self.calls += 1
                    return 'Address'
            provider = Provider()
            cursors = []
            for turn in range(5):
                outcome = await GeocodeWorker(pool, None, provider, 0).run_turn()
                assert outcome.ready
                async with pool.connection() as conn:
                    cursors.append((await (await conn.execute(
                        "SELECT cursor_trip_id,last_unit::text FROM geocode_discovery"
                    )).fetchone()))
            assert [kind for _, kind in cursors] == ['discovery', 'coordinate', 'discovery', 'coordinate', 'discovery']
            assert provider.calls == 2
            assert cursors[0][0] > 0 and cursors[2][0] > cursors[0][0]
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT generation=scanned_generation FROM geocode_discovery"
                )).fetchone()) == (True,)
                assert (await (await conn.execute("SELECT count(*) FROM geocode_retry")).fetchone()) == (999,)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_representative_and_discovery_use_bounded_indexed_plans():
    import re
    from decimal import Decimal
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,point_count,"
                    "start_geom) SELECT %s,'manual','manual',%s,%s,100,0,"
                    "ST_SetSRID(ST_MakePoint(20,10+i::float/10000),4326)::geography "
                    "FROM generate_series(1,10000) i", (account_id(conn), T0, T0 + timedelta(minutes=1)),
                )
                owner = account_id(conn)
                assert await GeocodeWorker(pool, None, None, 0)._source(conn, Decimal('11'), Decimal('20'))
            plans = []
            async with raw.connection() as conn:
                await conn.execute('ANALYZE trips; ANALYZE geocode_retry')
                cur = await conn.execute(
                    "SELECT pg_get_functiondef('public.geocode_representative_source(bigint,numeric,numeric)'::regprocedure)"
                )
                definition = (await cur.fetchone())[0]
                inner = definition.split('RETURN QUERY', 1)[1].split(';', 1)[0]
                inner = re.sub(r'\b(owner_id|lat|lon)\b', lambda match: '%(' + match[0] + ')s', inner)
                # Explain the actual installed body under its definer identity,
                # whose migration_writer policy permits the indexed expressions.
                await conn.execute('SET LOCAL ROLE odograph_migrate')
                cur = await conn.execute('EXPLAIN (ANALYZE, FORMAT JSON) ' + inner,
                                         {'owner_id': owner, 'lat': Decimal('11'), 'lon': Decimal('20')})
                plans.append((await cur.fetchone())[0][0]['Plan'])
            async with pool.connection() as conn:
                cur = await conn.execute(
                    'EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM trips '
                    'WHERE account_id=%s AND id>%s ORDER BY id LIMIT 500', (account_id(conn), 5000),
                )
                plans.append((await cur.fetchone())[0][0]['Plan'])
                cur = await conn.execute(
                    'EXPLAIN (ANALYZE, FORMAT JSON) SELECT rounded_lat,rounded_lon FROM geocode_retry '
                    'WHERE account_id=%s AND next_attempt_at<=now() ORDER BY next_attempt_at,'
                    'attempted_at NULLS FIRST,rounded_lat,rounded_lon LIMIT 1', (account_id(conn),),
                )
                plans.append((await cur.fetchone())[0][0]['Plan'])
            def nodes(plan):
                yield plan
                for child in plan.get('Plans', []):
                    yield from nodes(child)
            for plan in plans:
                assert plan['Node Type'] == 'Limit'
                assert not any(n['Node Type'] == 'Seq Scan' and n.get('Relation Name') in ('trips', 'geocode_retry')
                               for n in nodes(plan)), plan
            source_indexes = {n.get('Index Name') for n in nodes(plans[0])}
            assert {'trips_geocode_start_idx', 'trips_geocode_end_idx'} <= source_indexes
            discovery_indexes = {n.get('Index Name') for n in nodes(plans[1])}
            assert discovery_indexes & {'trips_geocode_discovery_idx', 'trips_account_id_id_key', 'trips_pkey'}, discovery_indexes
            assert all(n.get('Actual Rows', 0) <= 500 and n.get('Rows Removed by Filter', 0) == 0
                       for n in nodes(plans[1]) if n.get('Relation Name') == 'trips')
            assert any(n.get('Index Name') == 'geocode_retry_due_idx' for n in nodes(plans[2]))
            assert [p['Actual Rows'] for p in plans] == [1, 500, 1]
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_concurrent_failures_advance_retry_only_once():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            both_started = asyncio.Event()
            class Provider:
                calls = 0
                async def reverse(self, client, lat, lon):
                    self.calls += 1
                    if self.calls == 2:
                        both_started.set()
                    await asyncio.wait_for(both_started.wait(), 5)
                    raise httpx.ConnectError('unavailable')
            provider = Provider()
            outcomes = await asyncio.gather(*[
                GeocodeWorker(pool, None, provider, 0)._geocode_one(47.1, -122.1) for _ in range(2)
            ])
            assert provider.calls == 2
            assert sum(o.attempted for o in outcomes) == 2
            assert sum(o.retriable_failures for o in outcomes) == 1
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT failure_count,next_attempt_at-attempted_at FROM geocode_retry"
                )).fetchone()) == (1, timedelta(seconds=60))
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_cache_and_queue_completion_roll_back_together():
    from psycopg.errors import CheckViolation
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            async with raw.connection() as conn:
                await conn.execute("CREATE FUNCTION test_geocode_rollback() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$BEGIN RAISE EXCEPTION 'completion rejected' USING ERRCODE='23514'; END$$; "
                    "CREATE TRIGGER test_geocode_rollback BEFORE DELETE ON geocode_retry "
                    "FOR EACH ROW EXECUTE FUNCTION test_geocode_rollback()")
            class Provider:
                async def reverse(self, client, lat, lon):
                    return 'Address'
            try:
                with pytest.raises(CheckViolation, match='completion rejected'):
                    await GeocodeWorker(pool, None, Provider(), 0)._geocode_one(47.1, -122.1)
                async with pool.connection() as conn:
                    assert (await (await conn.execute("SELECT count(*) FROM geocode_cache")).fetchone()) == (0,)
                    assert (await (await conn.execute(
                        "SELECT attempted_at,failure_count FROM geocode_retry"
                    )).fetchone()) == (None, 0)
            finally:
                async with raw.connection() as conn:
                    await conn.execute("DROP TRIGGER test_geocode_rollback ON geocode_retry; "
                                       "DROP FUNCTION test_geocode_rollback()")
            assert (await GeocodeWorker(pool, None, Provider(), 0).run_once()).completed == 1
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_endpoint_eligibility_behind_cursor_survives_round_and_restart():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await conn.execute("UPDATE tracking_devices SET enabled=false WHERE account_id=%s", (account_id(conn),))
                await conn.execute(
                    "INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,"
                    "distance_m,point_count,start_geom) SELECT %s,1,%s,'manual',%s,%s,100,0,"
                    "ST_SetSRID(ST_MakePoint(-122,47+i::float/10000),4326)::geography "
                    "FROM generate_series(1,501) i", (account_id(conn), DEVICE, T0, T0 + timedelta(minutes=1)),
                )
            class Unused:
                async def reverse(self, client, lat, lon):
                    pytest.fail('discovery consumed HTTP')
            assert (await GeocodeWorker(pool, None, Unused(), 0).run_turn()).ready
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM geocode_retry")).fetchone()) == (0,)
                await conn.execute("UPDATE tracking_devices SET enabled=true WHERE account_id=%s", (account_id(conn),))
            # Finish the old round, then start the changed generation's round.
            await GeocodeWorker(pool, None, Unused(), 0)._discover()
            await GeocodeWorker(pool, None, Unused(), 0)._discover()
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM geocode_retry")).fetchone()) == (501,)
                assert (await (await conn.execute(
                    "SELECT generation>scanned_generation,cursor_trip_id>0 FROM geocode_discovery"
                )).fetchone()) == (True, True)
            await GeocodeWorker(pool, None, Unused(), 0)._discover()
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT generation=scanned_generation,cursor_trip_id FROM geocode_discovery"
                )).fetchone()) == (True, 0)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_source_disappearing_during_http_completes_orphan_without_null_cache():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                trip = await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            class Provider:
                async def reverse(self, client, lat, lon):
                    async with pool.connection() as conn:
                        await conn.execute('DELETE FROM trips WHERE account_id=%s AND id=%s', (account_id(conn), trip))
                    return None
            result = await GeocodeWorker(pool, None, Provider(), 0).run_once()
            assert (result.attempted, result.completed) == (1, 1)
            async with pool.connection() as conn:
                assert (await (await conn.execute('SELECT count(*) FROM geocode_cache')).fetchone()) == (0,)
                assert (await (await conn.execute('SELECT count(*) FROM geocode_retry')).fetchone()) == (0,)
        finally:
            await raw.close()
    asyncio.run(scenario())



def test_geocode_malformed_nominatim_label_persists_parse_retry():
    from app.geocode import NominatimProvider
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"display_name": True})
            )) as client:
                worker = GeocodeWorker(pool, client, NominatimProvider("http://nominatim", "", "test"), 0)
                result = await worker.run_once()
                assert (result.attempted, result.completed, result.retriable_failures) == (1, 0, 1)
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT failure_reason::text,next_attempt_at-attempted_at FROM geocode_retry"
                )).fetchone()) == ('parse', timedelta(seconds=60))
                assert (await (await conn.execute('SELECT count(*) FROM geocode_cache')).fetchone()) == (0,)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_geocode_new_coordinate_is_due_while_old_coordinate_remains_deferred():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await seed_tracking_device(conn, DEVICE, device_id=1)
                await _insert_trip(conn, T0, T0 + timedelta(minutes=1), 47.1, -122.1)
            class Provider:
                calls = []
                async def reverse(self, client, lat, lon):
                    self.calls.append(lat)
                    if lat == 47.1:
                        raise httpx.ConnectError('unavailable')
                    return 'Later address'
            provider = Provider()
            assert (await GeocodeWorker(pool, None, provider, 0).run_once()).retriable_failures == 1
            deferred = await GeocodeWorker(pool, None, provider, 0).run_turn()
            assert deferred.deferred_until > asyncio.get_running_loop().time()
            async with pool.connection() as conn:
                old_retry = (await (await conn.execute(
                    'SELECT next_attempt_at FROM geocode_retry WHERE rounded_lat=47.1'
                )).fetchone())[0]
                await _insert_trip(conn, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=1), 47.2, -122.2)
                due_rows = await (await conn.execute(
                    'SELECT rounded_lat,next_attempt_at<=now() FROM geocode_retry ORDER BY rounded_lat'
                )).fetchall()
                assert [(float(lat), due) for lat, due in due_rows] == [(47.1, False), (47.2, True)]
            # A refreshed continuation/restart reads the durable due queue.
            # Discovery gets one alternating turn before the new coordinate.
            results = [await GeocodeWorker(pool, None, provider, 0).run_turn() for _ in range(2)]
            assert sum(r.batch.completed for r in results) == 1
            assert provider.calls == [47.1, 47.2]
            async with pool.connection() as conn:
                assert (await (await conn.execute(
                    'SELECT failure_count,next_attempt_at FROM geocode_retry'
                )).fetchone()) == (1, old_retry)
        finally:
            await raw.close()
    asyncio.run(scenario())
