from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from psycopg import errors

from app.account_context import AccountPool, AccountPrincipal, account_id
from app.account_settings import AccountSettings
from app.account_workers import AccountWorker
from app.db import NUDGE_ADVISORY_LOCK_KEY, make_pool
from app.geocode import GeocodeWorker
from app.nudge import NudgeWorker
from app.snap import SnapWorker
from app.worker import WorkerStatus
from conftest import reset_account_db, seed_tracking_device

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")
WHEN = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)


@asynccontextmanager
async def _accounts():
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        first = await reset_account_db(raw)
        async with raw.connection() as conn:
            await conn.execute("DROP INDEX accounts_singleton_idx")
            await conn.execute("INSERT INTO accounts(id,email,password_hash) VALUES(99,'second@example.test','hash')")
            await conn.execute("INSERT INTO account_settings(account_id) VALUES(99)")
        second = AccountPool(raw, AccountPrincipal(99, True, 1))
        yield raw, first, second
    finally:
        async with raw.connection() as conn:
            # Keep the schema baseline intact; truncate both fixtures before
            # restoring the production singleton guard.
            await conn.execute("TRUNCATE accounts, instance_state CASCADE")
            await conn.execute("CREATE UNIQUE INDEX accounts_singleton_idx ON accounts ((true))")
        await raw.close()


async def _trip(pool, *, detected=False):
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, "same") if detected else None
        cur = await conn.execute(
            "INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,distance_m,start_geom,snap_status,point_count) "
            "VALUES(%s,%s,'same',%s,%s,%s,1000,ST_SetSRID(ST_MakePoint(20,10),4326)::geography,%s,2) RETURNING id",
            (account_id(conn), device, "detected" if detected else "manual", WHEN-timedelta(days=1), WHEN, "pending" if detected else None),
        )
        trip = (await cur.fetchone())[0]
        if detected:
            for index in range(2):
                await conn.execute(
                    "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,trip_id) "
                    "VALUES(%s,%s,'same',%s,ST_SetSRID(ST_MakePoint(20,10),4326)::geography,%s)",
                    (account_id(conn),device,WHEN-timedelta(days=1)+timedelta(seconds=index),trip),
                )
    return trip, device


class _Provider:
    def __init__(self, label, started=None, release=None):
        self.label, self.started, self.release = label, started, release
        self.calls = 0

    async def reverse(self, client, lat, lon):
        self.calls += 1
        if self.started:
            self.started.set()
            await self.release.wait()
        return self.label


async def _private_caches():
    async with _accounts() as (raw, first, second):
        await _trip(first)
        await _trip(second)
        a, b = _Provider("First private address"), _Provider("Second private address")
        await GeocodeWorker(first,None,a,0,0,60).run_once()
        async with second.connection() as conn:
            assert await (await conn.execute("SELECT address FROM geocode_cache WHERE account_id=%s", (account_id(conn),))).fetchall() == []
        await GeocodeWorker(second,None,b,0,0,60).run_once()
        assert a.calls == b.calls == 1
        async with raw.connection() as conn:
            assert await (await conn.execute("SELECT account_id,address FROM geocode_cache ORDER BY account_id")).fetchall() == [(41,"First private address"),(99,"Second private address")]


def test_same_coordinate_cache_is_private_to_each_account():
    asyncio.run(_private_caches())


@pytest.mark.parametrize("device_state", ["disabled", "revoked"])
def test_inactive_device_does_not_starve_later_pending_snap(device_state):
    async def scenario():
        async with _accounts() as (raw, first, second):
            inactive_trip, inactive_device = await _trip(first, detected=True)
            active_trip, _ = await _trip(first, detected=True)
            async with first.connection() as conn:
                await conn.execute(
                    "UPDATE tracking_devices SET enabled=%s,revoked_at=CASE WHEN %s THEN now() END "
                    "WHERE account_id=%s AND id=%s",
                    (device_state != "disabled", device_state == "revoked", account_id(conn), inactive_device),
                )
            calls = []
            class HTTP:
                async def get(self, url):
                    calls.append(url)
                    return httpx.Response(200, json={"code": "NoMatch", "matchings": []})
            worker = SnapWorker(first, HTTP(), "http://osrm", .5, 250, 0, 60, batch_size=1)
            await worker.run_once()
            await worker.run_once()
            assert len(calls) == 1
            async with first.connection() as conn:
                assert await (await conn.execute(
                    "SELECT id,snap_status::text FROM trips WHERE account_id=%s ORDER BY id",
                    (account_id(conn),),
                )).fetchall() == [(inactive_trip, "pending"), (active_trip, "failed")]
    asyncio.run(scenario())


@pytest.mark.parametrize("device_state", ["disabled", "revoked"])
def test_inactive_endpoint_does_not_fill_the_geocode_batch(device_state):
    async def scenario():
        async with _accounts() as (raw, first, second):
            first_trip, _ = await _trip(first, detected=True)
            await _trip(first, detected=True)
            async with first.connection() as conn:
                await conn.execute(
                    "UPDATE trips SET start_geom=ST_SetSRID(ST_MakePoint(20,11),4326)::geography "
                    "WHERE account_id=%s AND id=%s", (account_id(conn), first_trip),
                )
            # Observe the actual LIMIT choice rather than assuming a hash/UNION
            # ordering, then invalidate that source before the next sweep.
            selected = []
            class Probe(GeocodeWorker):
                async def _geocode_one(self, lat, lon):
                    selected.append((lat, lon))
            await Probe(first, None, _Provider("unused"), 0, 0, 60, batch_size=1).run_once()
            assert len(selected) == 1
            async with first.connection() as conn:
                await conn.execute(
                    "UPDATE tracking_devices d SET enabled=%s,revoked_at=CASE WHEN %s THEN now() END "
                    "FROM trips t WHERE d.account_id=%s AND t.account_id=d.account_id "
                    "AND t.tracking_device_id=d.id AND ST_Y(t.start_geom::geometry)=%s",
                    (device_state != "disabled", device_state == "revoked", account_id(conn), selected[0][0]),
                )
            provider = _Provider("active source")
            worker = GeocodeWorker(first, None, provider, 0, 0, 60, batch_size=1)
            await worker.run_once()
            await worker.run_once()
            assert provider.calls == 1
            async with first.connection() as conn:
                coords = await (await conn.execute(
                    "SELECT lat,lon FROM geocode_cache WHERE account_id=%s", (account_id(conn),),
                )).fetchall()
                assert len(coords) == 1
                assert tuple(map(float, coords[0])) != selected[0]
    asyncio.run(scenario())


@pytest.mark.parametrize("imported", [False, True])
def test_geocode_accepts_manual_and_imported_endpoints_without_a_device(imported):
    async def scenario():
        async with _accounts() as (raw, first, second):
            trip, _ = await _trip(first)
            if imported:
                async with first.connection() as conn:
                    await conn.execute(
                        "UPDATE trips SET source='detected',imported=true WHERE account_id=%s AND id=%s",
                        (account_id(conn), trip),
                    )
            provider = _Provider("inert endpoint")
            await GeocodeWorker(first, None, provider, 0, 0, 60, batch_size=1).run_once()
            assert provider.calls == 1
    asyncio.run(scenario())


async def _delayed_result(worker_kind, mutation):
    async with _accounts() as (raw, first, second):
        trip, device = await _trip(first, detected=True)
        started, release = asyncio.Event(), asyncio.Event()
        if worker_kind == "geocode":
            worker = GeocodeWorker(first,None,_Provider("stale",started,release),0,0,60)
            task = asyncio.create_task(worker.run_once())
        else:
            class DelayedHTTP:
                async def get(self, url):
                    started.set()
                    await release.wait()
                    return httpx.Response(200,json={"code":"NoMatch","matchings":[]})
            task = asyncio.create_task(SnapWorker(first,DelayedHTTP(),"http://osrm",0.5,250,0,60).run_once())
        await asyncio.wait_for(started.wait(),5)
        async with raw.connection() as conn:
            if mutation == "disable_account":
                await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=41")
            else:
                # A revoke/re-enable cycle is stale even when currently enabled.
                await conn.execute("UPDATE tracking_devices SET generation=generation+1 WHERE id=%s", (device,))
        release.set()
        if mutation == "disable_account":
            with pytest.raises(errors.InsufficientPrivilege):
                await task
        else:
            await task
        async with raw.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM geocode_cache")).fetchone() == (0,)
            assert await (await conn.execute("SELECT snap_status::text FROM trips WHERE id=%s", (trip,))).fetchone() == ("pending",)


@pytest.mark.parametrize("worker_kind", ["geocode","snap"])
@pytest.mark.parametrize("mutation", ["disable_account","replace_generation"])
def test_delayed_provider_result_cannot_commit_after_owner_or_device_invalidation(worker_kind, mutation):
    asyncio.run(_delayed_result(worker_kind, mutation))


async def _notification_destinations():
    async with _accounts() as (raw, first, second):
        calls=[]
        def post(request):
            calls.append(str(request.url))
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(post)) as client:
            workers=[]
            for pool,topic in ((first,"first"),(second,"second")):
                await _trip(pool)
                async with pool.connection() as conn:
                    await conn.execute("UPDATE account_settings SET ntfy_topic=%s WHERE account_id=%s", (topic,account_id(conn)))
                workers.append(NudgeWorker(pool,client,"https://ntfy.example.test",topic,"","","","",ZoneInfo("UTC"),18))
            for worker in workers:
                await worker.run_once(WHEN)
                await worker.run_once(WHEN)
        assert calls == ["https://ntfy.example.test/first","https://ntfy.example.test/second"]
        async with raw.connection() as conn:
            assert await (await conn.execute("SELECT account_id,trip_count FROM nudge_delivery_windows ORDER BY account_id")).fetchall() == [(41,1),(99,1)]


def test_same_delivery_window_uses_each_accounts_destination_and_deduplicates_independently():
    asyncio.run(_notification_destinations())


async def _stale_preferences():
    async with _accounts() as (raw, first, second):
        await _trip(first)
        async with first.connection() as conn:
            await conn.execute("UPDATE account_settings SET ntfy_topic='old' WHERE account_id=%s", (account_id(conn),))
        calls=[]
        def post(request):
            calls.append(request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(post)) as client:
            worker=NudgeWorker(first,client,"https://ntfy.example.test","old","","","","",ZoneInfo("UTC"),18)
            async with raw.connection() as locker:
                await locker.execute("SELECT pg_advisory_xact_lock(%s)", (NUDGE_ADVISORY_LOCK_KEY,))
                task=asyncio.create_task(worker.run_once(WHEN))
                async def wait_for_lock():
                    while True:
                        async with raw.connection() as probe:
                            row=await (await probe.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'SELECT pg_advisory_xact_lock%%'")).fetchone()
                        if row[0]:
                            return
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_lock(),5)
                async with raw.connection() as changer:
                    await changer.execute("UPDATE account_settings SET ntfy_topic='new' WHERE account_id=41")
            await task
        assert calls == []
        async with raw.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM nudge_delivery_windows")).fetchone() == (0,)


def test_queued_notification_rechecks_changed_preferences_after_waiting_for_lock():
    asyncio.run(_stale_preferences())


async def _account_failure_independence():
    async with _accounts() as (raw, first, second):
        fail=True
        class Job:
            def __init__(self,pool):
                self.pool=pool
                self.status=WorkerStatus("child")
            async def run_once(self):
                if fail and self.pool.principal.account_id == 41:
                    raise RuntimeError("synthetic account failure")
                async with self.pool.connection() as conn:
                    await conn.execute("INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES(%s,10,20,'completed') ON CONFLICT DO NOTHING", (account_id(conn),))
        worker=AccountWorker(SimpleNamespace(control=raw,runtime=raw),AccountSettings(ZoneInfo("UTC")),lambda pool,config:Job(pool),label="test",debounce_s=0,sweep_s=60)
        await worker._run_guarded()
        assert worker.status.last_failure_at is not None
        assert worker.status.last_success_at is None
        async with raw.connection() as conn:
            assert await (await conn.execute("SELECT account_id FROM geocode_cache")).fetchall() == [(99,)]
        fail=False
        await worker._run_guarded()
        assert worker.status.last_success_at is not None
        async with raw.connection() as conn:
            assert await (await conn.execute("SELECT account_id FROM geocode_cache ORDER BY account_id")).fetchall() == [(41,),(99,)]


def test_one_account_failure_does_not_block_another_or_claim_complete_success():
    asyncio.run(_account_failure_independence())
