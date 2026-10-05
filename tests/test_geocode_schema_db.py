"""Durable endpoint intent, discovery and exact accounting contracts."""
import asyncio
import os

import psycopg
import pytest

from app.account_context import account_id
from app.db import make_pool
from conftest import add_test_account, reset_account_db, seed_tracking_device

pytestmark = pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='requires disposable TEST_DATABASE_URL')


def _run(body):
    async def scenario():
        raw = make_pool(os.environ['TEST_DATABASE_URL'])
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            await body(raw, pool)
        finally:
            await raw.close()
    asyncio.run(scenario())


async def _trip(conn, lat=1, lon=2, stream=None):
    return (await (await conn.execute(
        "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,point_count,start_geom,end_geom,tracking_device_id) "
        "VALUES(%s,'phone','manual',now(),now()+interval '1 hour',0,0,"
        "ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,"
        "ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s) RETURNING id",
        (account_id(conn),lon,lat,lon,lat,stream))).fetchone())[0]


async def _queue(conn):
    return await (await conn.execute('SELECT rounded_lat,rounded_lon FROM geocode_retry ORDER BY rounded_lat,rounded_lon')).fetchall()


async def _consistent(raw):
    async with raw.connection() as conn:
        assert (await (await conn.execute('SELECT storage_usage_consistent()')).fetchone())[0]


def test_intents_rounding_generation_and_backoff_preservation():
    async def body(raw,pool):
        async with pool.connection() as conn:
            trip = await _trip(conn, '-1.23455', '-2.34565')
            assert [(str(a),str(b)) for a,b in await _queue(conn)] == [('-1.2346','-2.3457')]
            await conn.execute("UPDATE geocode_retry SET attempted_at=now(),next_attempt_at=now()+interval '1 hour',failure_count=31,failure_reason='parse'")
            before = await (await conn.execute('SELECT attempted_at,next_attempt_at,failure_count,failure_reason FROM geocode_retry')).fetchone()
            await conn.execute("UPDATE trips SET notes='human text',geocode_generation=999 WHERE id=%s",(trip,))
            assert (await (await conn.execute('SELECT geocode_generation FROM trips WHERE id=%s',(trip,))).fetchone())[0] == 1
            await conn.execute('UPDATE trips SET start_geom=ST_SetSRID(ST_MakePoint(3,4),4326)::geography WHERE id=%s',(trip,))
            assert (await (await conn.execute('SELECT geocode_generation FROM trips WHERE id=%s',(trip,))).fetchone())[0] == 2
            assert len(await _queue(conn)) == 2
            assert (await (await conn.execute('SELECT attempted_at,next_attempt_at,failure_count,failure_reason FROM geocode_retry WHERE rounded_lat=-1.2346')).fetchone()) == before
        await _consistent(raw)
    _run(body)


def test_retry_exact_charges_updates_refunds_and_rollback():
    async def body(raw,pool):
        async with pool.connection() as conn:
            before = (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0]
            await conn.execute('INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon) VALUES(%s,1,2)',(account_id(conn),))
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before+128
            await conn.execute("UPDATE geocode_retry SET failure_count=31,failure_reason='transport',attempted_at=now(),next_attempt_at=now()+interval '1 day'")
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before+128+9
        with pytest.raises(RuntimeError):
            async with pool.connection() as conn:
                await conn.execute('DELETE FROM geocode_retry')
                raise RuntimeError('rollback')
        async with pool.connection() as conn:
            assert len(await _queue(conn)) == 1
            await conn.execute('DELETE FROM geocode_retry')
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before
        await _consistent(raw)
    _run(body)


def test_page_is_bounded_and_changes_behind_cursor_survive_restart():
    async def body(raw,pool):
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,point_count,start_geom) "
                "SELECT %s,'phone','manual',now(),now()+interval '1 hour',0,0,"
                "ST_SetSRID(ST_MakePoint(2,i::double precision/100),4326)::geography FROM generate_series(1,501) i",(account_id(conn),))
            first = (await (await conn.execute('SELECT min(id) FROM trips')).fetchone())[0]
            await conn.execute('DELETE FROM geocode_retry')
            page = (await (await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))).fetchone())
            assert page[0:2] == (500,500)
            assert page[2] > first and page[5]
            await conn.execute('UPDATE trips SET start_geom=ST_SetSRID(ST_MakePoint(8,9),4326)::geography WHERE id=%s',(first,))
        # A new connection resumes the persisted cursor and finishes the old generation.
        async with pool.connection() as conn:
            page = (await (await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))).fetchone())
            assert page[0:2] == (1,1) and page[2] == 0 and page[5]
            page = (await (await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))).fetchone())
            assert page[0] == 500 and page[5]
            page = (await (await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))).fetchone())
            assert page[0] == 1 and not page[5]
            assert (await (await conn.execute('SELECT last_unit FROM geocode_discovery')).fetchone())[0] == 'discovery'
            await conn.execute('SELECT geocode_record_coordinate_turn(%s)',(account_id(conn),))
            assert (await (await conn.execute('SELECT last_unit FROM geocode_discovery')).fetchone())[0] == 'coordinate'
        await _consistent(raw)
    _run(body)


def test_place_and_device_eligibility_changes_generate_rediscovery():
    async def body(raw,pool):
        async with pool.connection() as conn:
            stream = await seed_tracking_device(conn)
            trip = await _trip(conn,stream=stream)
            place = (await (await conn.execute("INSERT INTO places(account_id,name,geom,kind) VALUES(%s,'Home',ST_SetSRID(ST_MakePoint(2,1),4326)::geography,'home') RETURNING id",(account_id(conn),))).fetchone())[0]
            await conn.execute('UPDATE trips SET start_place_id=%s,end_place_id=%s WHERE id=%s',(place,place,trip))
            await conn.execute('DELETE FROM geocode_retry')
            await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))
            assert not await _queue(conn)
            await conn.execute('DELETE FROM places WHERE id=%s',(place,))
            assert len(await _queue(conn)) == 1
            await conn.execute('DELETE FROM geocode_retry')
            await conn.execute('UPDATE tracking_devices SET geocode_generation=999 WHERE id=%s',(stream,))
            assert (await (await conn.execute('SELECT geocode_generation FROM tracking_devices WHERE id=%s',(stream,))).fetchone())[0] == 1
            await conn.execute('UPDATE tracking_devices SET enabled=false WHERE id=%s',(stream,))
            assert (await (await conn.execute('SELECT geocode_generation FROM tracking_devices WHERE id=%s',(stream,))).fetchone())[0] == 2
            await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))
            assert not await _queue(conn)
        async with pool.connection() as conn:
            await conn.execute('UPDATE tracking_devices SET enabled=true WHERE id=%s',(stream,))
            assert (await (await conn.execute('SELECT geocode_generation FROM tracking_devices WHERE id=%s',(stream,))).fetchone())[0] == 3
            assert (await (await conn.execute('SELECT generation>scanned_generation FROM geocode_discovery')).fetchone())[0]
            await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))
            assert len(await _queue(conn)) == 1
            await conn.execute('UPDATE tracking_devices SET revoked_at=now() WHERE id=%s',(stream,))
            await conn.execute('DELETE FROM geocode_retry')
            await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))
            assert not await _queue(conn)
        await _consistent(raw)
    _run(body)


def test_cache_deletion_reopens_discovery_and_intents_skip_cached_coordinates():
    async def body(raw,pool):
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES(%s,1,2,NULL)",(account_id(conn),))
            await _trip(conn)
            assert not await _queue(conn)
            await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))
            await conn.execute('DELETE FROM geocode_cache')
            assert (await (await conn.execute('SELECT generation>scanned_generation FROM geocode_discovery')).fetchone())[0]
            page = (await (await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))).fetchone())
            assert page[1] == 1 and not page[5]
        await _consistent(raw)
    _run(body)


def test_retry_isolation_and_protected_helper_authority():
    async def body(raw,pool):
        other = await add_test_account(raw, 91002)
        async with other.connection() as conn:
            await _trip(conn,lat=3,lon=4)
        async with pool.connection() as conn:
            assert not await _queue(conn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with pool.connection() as conn:
                await conn.execute('SELECT geocode_discover_page(91002)')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with pool.connection() as conn:
                await conn.execute('UPDATE geocode_discovery SET generation=generation+1')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with pool.connection() as conn:
                await conn.execute('INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon) VALUES(91002,5,6)')
        await _consistent(raw)
    _run(body)


@pytest.mark.parametrize('reason,reason_bytes',[(None,0),('http',4),('transport',9),('parse',5),('source_changed',14)])
def test_enum_text_charges_include_reason_and_alternating_unit(reason,reason_bytes):
    # Charging helpers have migration-only authority; inspect exact totals through the runtime ledger.
    async def runtime_body(raw,pool):
        async with pool.connection() as conn:
            before = (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0]
            await conn.execute('INSERT INTO geocode_retry(account_id,rounded_lat,rounded_lon,failure_reason) VALUES(%s,1,2,%s)',(account_id(conn),reason))
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before+128+reason_bytes
            await conn.execute('SELECT * FROM geocode_discover_page(%s)',(account_id(conn),))
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before+127+reason_bytes
            await conn.execute('SELECT geocode_record_coordinate_turn(%s)',(account_id(conn),))
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before+128+reason_bytes
            await conn.execute('DELETE FROM geocode_retry')
            assert (await (await conn.execute('SELECT actual_bytes FROM account_usage')).fetchone())[0] == before
        await _consistent(raw)
    _run(runtime_body)


def test_bulk_intents_advance_once_per_statement_and_bound_queue_by_coordinate():
    async def body(raw,pool):
        async with pool.connection() as conn:
            before = (await (await conn.execute('SELECT generation FROM geocode_discovery')).fetchone())[0]
            await conn.execute("INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,start_geom) "
                "SELECT %s,'phone','manual',now(),now()+interval '1 hour',0,"
                "ST_SetSRID(ST_MakePoint(2,(i%%100)::double precision/100),4326)::geography FROM generate_series(1,50000)i",(account_id(conn),))
            assert (await (await conn.execute('SELECT generation FROM geocode_discovery')).fetchone())[0] == before+1
            assert len(await _queue(conn)) == 100
            await conn.execute('UPDATE trips SET start_geom=ST_SetSRID(ST_MakePoint(3,4),4326)::geography')
            assert (await (await conn.execute('SELECT generation FROM geocode_discovery')).fetchone())[0] == before+2
            assert len(await _queue(conn)) == 101
        await _consistent(raw)
    _run(body)
