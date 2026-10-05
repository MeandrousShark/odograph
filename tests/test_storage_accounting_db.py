"""Logical stored-value charging, rollback, authority, and reconciliation."""
import asyncio
import os

import psycopg
import pytest

from app.account_context import account_id
from app.db import make_pool
from conftest import add_test_account, reset_account_db, seed_tracking_device

TEST_DB = os.environ.get('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not TEST_DB, reason='requires disposable TEST_DATABASE_URL')


async def _usage(conn):
    return await (await conn.execute(
        'SELECT actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes FROM account_usage WHERE account_id=%s',
        (account_id(conn),))).fetchone()


async def _assert_consistent(raw):
    async with raw.connection() as conn:
        assert (await (await conn.execute('SELECT public.storage_usage_consistent()')).fetchone())[0]


def _run(body):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            await body(raw, pool)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_raw_canonical_utf8_old_new_refunds_and_rollback():
    async def body(raw, pool):
        async with pool.connection() as conn:
            before = await _usage(conn)
            result = await (await conn.execute(
                "INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{\"z\": 1, \"a\": \"é🚀\"}'::jsonb) "
                'RETURNING id,128+octet_length(convert_to(payload::text,\'UTF8\'))', (account_id(conn),))).fetchone()
            row, charge = result
            after = await _usage(conn)
            assert after == (before[0]+charge, before[1], before[2]+charge, before[3])
        async with pool.connection() as conn:
            await conn.execute("UPDATE raw_messages SET payload='{\"n\": null}'::jsonb WHERE id=%s", (row,))
            current_charge = (await (await conn.execute(
                "SELECT 128+octet_length(convert_to(payload::text,'UTF8')) FROM raw_messages WHERE id=%s", (row,))).fetchone())[0]
            assert await _usage(conn) == (before[0]+current_charge,before[1],before[2]+current_charge,before[3])
        with pytest.raises(RuntimeError):
            async with pool.connection() as conn:
                await conn.execute('DELETE FROM raw_messages WHERE id=%s', (row,))
                raise RuntimeError('deliberate rollback')
        async with pool.connection() as conn:
            assert (await _usage(conn))[2] == before[2]+current_charge
            await conn.execute('DELETE FROM raw_messages WHERE id=%s', (row,))
            assert await _usage(conn) == before
        await _assert_consistent(raw)
    _run(body)


def test_device_metadata_point_text_geometry_and_permanent_label_reserve():
    async def body(raw, pool):
        async with pool.connection() as conn:
            before = await _usage(conn)
            stream = await seed_tracking_device(conn, '🚀')
            # Device and detector rows plus protected envelope metadata.
            assert (await _usage(conn))[0] == before[0]+128+4+128+128
            point = (await (await conn.execute(
                "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,trigger) "
                "VALUES(%s,%s,'🚀',now(),ST_SetSRID(ST_MakePoint(1,2),4326)::geography,'é') RETURNING id",
                (account_id(conn),stream))).fetchone())[0]
            usage = await _usage(conn)
            assert usage[0] == before[0]+128+4+128+128+256+25+4+2
            assert usage[1] == 1024+8
            await conn.execute("UPDATE tracking_devices SET label=%s WHERE id=%s", ('🚀'*100,stream))
            assert (await _usage(conn))[1] == 1024+800
            await conn.execute("UPDATE tracking_devices SET label='x' WHERE id=%s", (stream,))
            assert (await _usage(conn))[1] == 1024+800
            await conn.execute('DELETE FROM points WHERE id=%s', (point,))
            assert (await _usage(conn))[1] == 0
            await conn.execute('DELETE FROM detector_state WHERE tracking_device_id=%s', (stream,))
            await conn.execute('DELETE FROM tracking_devices WHERE id=%s', (stream,))
            assert await _usage(conn) == before
        await _assert_consistent(raw)
    _run(body)


def test_manual_imported_geometry_human_text_and_optional_subsets():
    async def body(raw,pool):
        async with pool.connection() as conn:
            before=await _usage(conn)
            for source,imported in [('manual',False),('detected',True)]:
                await conn.execute(
                    'INSERT INTO trips(account_id,device,source,imported,started_at,ended_at,distance_m,notes,purpose,path,path_snapped) '
                    'VALUES(%s,%s,%s,%s,now(),now(),1,%s,%s,ST_GeomFromText(%s,4326),ST_GeomFromText(%s,4326))',
                    (account_id(conn),'é',source,imported,'🚀','a','LINESTRING(1 2,3 4)','MULTILINESTRING((1 2,3 4))'))
            route_bytes=(await (await conn.execute('SELECT octet_length(ST_AsEWKB(path_snapped,\'NDR\')) FROM trips LIMIT 1')).fetchone())[0]
            after=await _usage(conn)
            assert after[0]-before[0] == 2*(512+2+4+1+45+route_bytes)
            assert after[1] == before[1]
            assert after[3]-before[3] == 2*route_bytes
            await conn.execute("INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES(%s,1,2,'é🚀')",(account_id(conn),))
            assert (await _usage(conn))[3] == after[3]+128+6
            await conn.execute('UPDATE trips SET path_snapped=NULL WHERE id=-1')
            assert (await _usage(conn))[3] == after[3]+134
            await conn.execute('DELETE FROM trips')
            await conn.execute('DELETE FROM geocode_cache')
            assert await _usage(conn)==before
        await _assert_consistent(raw)
    _run(body)


def test_control_avatar_uses_stored_owner_without_runtime_context():
    async def body(raw,pool):
        async with pool.connection() as conn:
            before=await _usage(conn)
        async with pool.control_pool.connection() as conn:
            assert (await (await conn.execute("SELECT current_setting('app.account_id',true)")).fetchone())[0] in (None,'')
            await conn.execute("UPDATE accounts SET avatar_bytes=%s,avatar_mime='image/png',avatar_updated_at=now() WHERE id=%s",(b'abc',pool.principal.account_id))
        async with pool.connection() as conn:
            assert (await _usage(conn))[0]==before[0]+3+9
        async with pool.control_pool.connection() as conn:
            await conn.execute('UPDATE accounts SET avatar_bytes=NULL,avatar_mime=NULL,avatar_updated_at=NULL WHERE id=%s',(pool.principal.account_id,))
        async with pool.connection() as conn:
            assert await _usage(conn)==before
        await _assert_consistent(raw)
    _run(body)


@pytest.mark.parametrize('table',['account_usage','device_storage_envelopes'])
def test_runtime_cannot_edit_protected_state_or_reconcile(table):
    async def body(raw,pool):
        async with pool.connection() as conn:
            await seed_tracking_device(conn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with pool.connection() as conn:
                await conn.execute(f'DELETE FROM {table}')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with pool.connection() as conn:
                await conn.execute('SELECT public.reconcile_storage_usage()')
        other=await add_test_account(raw,42)
        async with other.connection() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM account_usage')).fetchone())[0]==1
            assert (await (await conn.execute('SELECT count(*) FROM device_storage_envelopes')).fetchone())[0]==0
        await _assert_consistent(raw)
    _run(body)


def test_reconciliation_repairs_drift_without_reducing_historical_highwater():
    async def body(raw,pool):
        async with pool.connection() as conn:
            stream=await seed_tracking_device(conn,'🚀'*100)
            await conn.execute("UPDATE tracking_devices SET label='x' WHERE id=%s",(stream,))
        async with raw.connection() as conn:
            await conn.execute('UPDATE account_usage SET actual_bytes=actual_bytes+7 WHERE account_id=41')
            assert not (await (await conn.execute('SELECT public.storage_usage_consistent()')).fetchone())[0]
            await conn.execute('SELECT public.reconcile_storage_usage()')
            assert (await (await conn.execute('SELECT public.storage_usage_consistent()')).fetchone())[0]
            assert (await (await conn.execute('SELECT label_bytes FROM device_storage_envelopes WHERE tracking_device_id=%s',(stream,))).fetchone())[0]==400
        await _assert_consistent(raw)
    _run(body)


def test_concurrent_same_account_updates_serialize_before_personal_row_locks():
    async def body(raw,pool):
        async with pool.connection() as conn:
            rows=await (await conn.execute(
                "INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}'),(%s,'{}') RETURNING id",
                (account_id(conn),account_id(conn)))).fetchall()
        locked=asyncio.Event()
        waiting=asyncio.Event()
        async def first():
            async with pool.connection() as conn:
                await conn.execute("SET LOCAL lock_timeout='3s'")
                await conn.execute("UPDATE raw_messages SET payload='{\"a\":1}' WHERE id=%s",(rows[0][0],))
                locked.set()
                await waiting.wait()
                await asyncio.sleep(.1)
                await conn.execute("UPDATE raw_messages SET payload='{\"b\":2}' WHERE id=%s",(rows[1][0],))
        async def second():
            await locked.wait()
            async with pool.connection() as conn:
                await conn.execute("SET LOCAL lock_timeout='3s'")
                waiting.set()
                await conn.execute("UPDATE raw_messages SET payload='{\"c\":3}' WHERE id=%s",(rows[1][0],))
                await conn.execute("UPDATE raw_messages SET payload='{\"d\":4}' WHERE id=%s",(rows[0][0],))
        await asyncio.wait_for(asyncio.gather(first(),second()),5)
        await _assert_consistent(raw)
    _run(body)


def test_forward_backfill_keeps_legacy_copied_labels_without_truncation(monkeypatch,tmp_path):
    import shutil
    import app.db as db_module
    from app.db import MIGRATIONS_DIR, run_migrations
    from conftest import drop_and_recreate_schema, full_schema_reset

    old=tmp_path/'old'
    old.mkdir()
    for path in MIGRATIONS_DIR.glob('*.sql'):
        if int(path.name.split('_',1)[0])<40:
            shutil.copy(path,old/path.name)

    async def scenario():
        raw=make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            await drop_and_recreate_schema(raw)
            monkeypatch.setattr(db_module,'MIGRATIONS_DIR',old)
            await run_migrations(raw)
            async with raw.connection() as conn:
                await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) VALUES(41,'legacy@example.invalid','unused',true)")
                await conn.execute("INSERT INTO tracking_devices(id,account_id,label) VALUES(1,41,'new')")
                await conn.execute(
                    "INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom) "
                    "SELECT 41,1,'d',now()+i*interval '1 second',ST_SetSRID(ST_MakePoint(i,i),4326)::geography FROM generate_series(1,2)i")
                await conn.execute(
                    'INSERT INTO stays(account_id,tracking_device_id,device,started_at,ended_at,centroid,point_count) '
                    'VALUES(41,1,%s,now(),now(),ST_SetSRID(ST_MakePoint(1,1),4326)::geography,1)',('🚀'*1000,))
                await conn.execute(
                    "INSERT INTO trips(account_id,tracking_device_id,device,started_at,ended_at,distance_m,notes,path) "
                    "VALUES(41,1,'old',now(),now(),1,'é',ST_GeomFromText('LINESTRING(1 1,2 2)',4326))")
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(41,'{\"é\":1}')")
                raw_charge=(await (await conn.execute("SELECT 128+octet_length(convert_to(payload::text,'UTF8')) FROM raw_messages")).fetchone())[0]
            monkeypatch.setattr(db_module,'MIGRATIONS_DIR',MIGRATIONS_DIR)
            await run_migrations(raw)
            async with raw.connection() as conn:
                expected_actual=128+(128+3)+128+2*(256+25+1)+2+raw_charge
                usage=await (await conn.execute('SELECT actual_bytes,reserved_bytes,raw_bytes,enhancement_bytes FROM account_usage')).fetchone()
                assert usage==(expected_actual,2*(1024+8000),raw_charge,0)
                assert (await (await conn.execute('SELECT label_bytes FROM device_storage_envelopes')).fetchone())[0]==4000
                assert (await (await conn.execute('SELECT octet_length(device) FROM stays')).fetchone())[0]==4000
                assert (await (await conn.execute('SELECT public.storage_usage_consistent()')).fetchone())[0]
        finally:
            monkeypatch.setattr(db_module,'MIGRATIONS_DIR',MIGRATIONS_DIR)
            await full_schema_reset(raw)
            await raw.close()
    asyncio.run(scenario())


def test_variable_operational_text_fields_are_charged_from_full_stored_values():
    async def body(raw,pool):
        async with pool.connection() as conn:
            before=await _usage(conn)
            await conn.execute(
                'UPDATE account_settings SET display_tz=%s,ntfy_topic=%s,email_to=%s,email_filing_reminder_mmdd=%s '
                'WHERE account_id=%s',('UTC','é','🚀','02-02',account_id(conn)))
            # Existing defaults: UTC, empty topic/address, and 01-15.
            assert (await _usage(conn))[0]==before[0]+6
            vehicle=(await (await conn.execute(
                'INSERT INTO vehicles(account_id,name,make,model,plate) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                (account_id(conn),'é','🚀','字','Z'))).fetchone())[0]
            assert (await _usage(conn))[0]==before[0]+6+128+2+4+3+1
            await conn.execute('UPDATE vehicles SET make=NULL,model=NULL,plate=NULL WHERE id=%s',(vehicle,))
            assert (await _usage(conn))[0]==before[0]+6+128+2
            await conn.execute(
                "INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,account_id,kind) VALUES('id','user','hash',%s,'legacy')",(account_id(conn),))
            assert (await _usage(conn))[0]==before[0]+6+128+2+128+2+4+4+6
            await conn.execute("UPDATE ingest_credentials SET secret_hash=%s WHERE public_id='id'",('🚀'*50,))
            assert (await _usage(conn))[0]==before[0]+6+128+2+128+2+4+200+6
        await _assert_consistent(raw)
    _run(body)


def test_bulk_stream_move_and_conflict_update_charge_statement_net_deltas():
    async def body(raw,pool):
        async with pool.connection() as conn:
            old=await seed_tracking_device(conn,'🚀')
            new=await seed_tracking_device(conn,'x')
            await conn.execute(
                'INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom) '
                "SELECT %s,%s,'🚀',now()+i*interval '1 second',ST_SetSRID(ST_MakePoint(i,i),4326)::geography FROM generate_series(1,2)i",
                (account_id(conn),old))
            before=await _usage(conn)
            await conn.execute('UPDATE points SET tracking_device_id=%s WHERE tracking_device_id=%s',(new,old))
            assert await _usage(conn)==(before[0],before[1]-12,before[2],before[3])
            states=await (await conn.execute('SELECT tracking_device_id,point_count,label_bytes FROM device_storage_envelopes ORDER BY tracking_device_id')).fetchall()
            assert states==[(old,0,4),(new,2,1)]
            await conn.execute("INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES(%s,1,2,'é')",(account_id(conn),))
            with_cache=await _usage(conn)
            await conn.execute(
                "INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES(%s,1,2,'🚀') "
                'ON CONFLICT(account_id,lat,lon) DO UPDATE SET address=EXCLUDED.address',(account_id(conn),))
            after=await _usage(conn)
            assert after==(with_cache[0]+2,with_cache[1],with_cache[2],with_cache[3]+2)
        await _assert_consistent(raw)
    _run(body)


def test_other_account_can_write_while_one_account_holds_usage_lock():
    async def body(raw,pool):
        other=await add_test_account(raw,42)
        async with pool.connection() as held:
            await held.execute("INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}')",(account_id(held),))
            async def write_other():
                async with other.connection() as conn:
                    await conn.execute("SET LOCAL lock_timeout='500ms'")
                    await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(%s,'{}')",(account_id(conn),))
            await asyncio.wait_for(write_other(),2)
        await _assert_consistent(raw)
    _run(body)
