"""Real snapshot, restricted-role filters and complete generic export output."""
import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from app.account_context import account_id
from app.account_work import report_account_work
from app.export import to_csv,to_xlsx
from app.export_filters import TextStore,SEARCH_SETTING,pattern
from app.export_preparation import ExportProjection,assemble_search_pattern,prepare_export
from app.preparation import PreparationOperation
from app.rates import load_rates
from app.ui._common import TRIP_COLUMNS,_trip_filter_sql
from test_capacity_db import _scenario
from test_report_projection_db import _seed,TZ
from test_export_filter_preparation import text
from test_streamed_exports import signature

pytestmark=[pytest.mark.db,pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='requires disposable PostGIS')]


@pytest.mark.parametrize('format,q',[('csv',''),('xlsx',''),('csv','Client'),('xlsx','Client')])
def test_complete_prepared_export_matches_existing_db_oracle_and_releases_after_send(tmp_path,format,q):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,other=accounts
            async with account.connection() as conn:
                await _seed(conn)
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s',(str(TZ),account_id(conn)))
                await conn.execute('UPDATE trips SET notes=%s WHERE account_id=%s',('車,%"\r\n'*6000,account_id(conn)))
            async with manager.operation('foreground',account.principal):
                async with account.connection() as conn:
                    where,params=_trip_filter_sql('',None,None,q=q,owner_id=account_id(conn))
                    cur=await conn.execute(f'SELECT {TRIP_COLUMNS} FROM trips {where} ORDER BY started_at DESC',params)
                    rows=await cur.fetchall()
                    columns=[column.name for column in cur.description]
                    trips=[dict(zip(columns,row)) for row in rows]
                    rates=await load_rates(conn)
                expected=to_csv(trips,rates,TZ) if format=='csv' else to_xlsx(trips,rates,TZ)
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    async with report_account_work(control,account.principal) as lease:
                        state=SimpleNamespace(_preparation=operation,_report_control_connection=lease,principal=account.principal,account_pool=account)
                        request=SimpleNamespace(state=state)
                        messages=[]
                        async def send(message):
                            assert lease.info.transaction_status==TransactionStatus.IDLE
                            assert operation.process.returncode==0
                            messages.append(message)
                        async def prepare_and_send():
                            response=await prepare_export(request,{'id':account.principal.account_id},format=format,q=q)
                            assert operation.finished and not operation.closed
                            await response({},None,send)
                        await operation.perform(prepare_and_send)
                        actual=b''.join(message.get('body',b'') for message in messages)
                        assert actual==expected if format=='csv' else signature(actual)==signature(expected)
                        headers=dict(messages[0]['headers'])
                        assert headers[b'content-disposition']==f'attachment; filename="trips.{format}"'.encode()
                        assert operation.closed and not operation.directory.exists()
            async with other.connection() as conn:
                assert (await (await conn.execute('SELECT count(*) FROM trips')).fetchone())[0]==0
    asyncio.run(run())


@pytest.mark.parametrize('client,value',[('UTF8','\u3000車%_\\\u3000'),('LATIN1','\u3000Café%_\\\u3000'),('SJIS','\u3000¥車%_\\\u3000')])
def test_real_pattern_conversion_matches_legacy_parameter_and_transaction_local_cleanup(tmp_path,client,value):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with manager.operation('foreground',account.principal):
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    store=TextStore(operation.budget)
                    try:
                        async with account.connection() as conn:
                            await conn.execute("SELECT set_config('client_encoding',%s,true)",(client,))
                            metadata=pattern(store,text(store,'q',value),conn.info.encoding)
                            projection=ExportProjection(operation,None)
                            await assemble_search_pattern(projection,conn,metadata)
                            expected='%'+value.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                            row=await (await conn.execute(f"SELECT convert_to(current_setting('{SEARCH_SETTING}'),'UTF8')=convert_to(%s::text,'UTF8')",(expected,))).fetchone()
                            assert row[0]
                        async with account.connection() as conn:
                            assert (await (await conn.execute(f"SELECT current_setting('{SEARCH_SETTING}',true)")).fetchone())[0] in (None,'')
                    finally: store.close()
    asyncio.run(run())


def test_partial_pattern_assembly_rolls_back_and_connection_reuse_cannot_see_prior_search(tmp_path):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            await runtime.resize(min_size=1,max_size=1)
            async with manager.operation('foreground',account.principal):
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    store=TextStore(operation.budget)
                    try:
                        metadata=pattern(store,text(store,'q','old%_\\'*10000),'utf8')
                        class CancelledProjection(ExportProjection):
                            def __init__(self): super().__init__(operation,None);self.calls=0
                            async def scalar(self,*args,**kwargs):
                                result=await super().scalar(*args,**kwargs)
                                self.calls+=1
                                if self.calls==3: raise asyncio.CancelledError
                                return result
                        with pytest.raises(asyncio.CancelledError):
                            async with account.connection() as conn:
                                backend=(await (await conn.execute('SELECT pg_backend_pid()')).fetchone())[0]
                                await assemble_search_pattern(CancelledProjection(),conn,metadata)
                        async with account.connection() as conn:
                            assert (await (await conn.execute('SELECT pg_backend_pid()')).fetchone())[0]==backend
                            assert conn.info.transaction_status==TransactionStatus.INTRANS
                            assert (await (await conn.execute(f"SELECT current_setting('{SEARCH_SETTING}',true)")).fetchone())[0] in (None,'')
                            operation.budget.remove('export-pattern')
                            replacement=pattern(store,text(store,'q','new'),'utf8')
                            await assemble_search_pattern(ExportProjection(operation,None),conn,replacement)
                            assert (await (await conn.execute(f"SELECT current_setting('{SEARCH_SETTING}')")).fetchone())[0]=='%new%'
                    finally: store.close()
    asyncio.run(run())


def test_prepared_export_reads_trip_rate_and_timezone_from_one_snapshot(tmp_path,monkeypatch):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with account.connection() as conn:
                await _seed(conn)
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s',(str(TZ),account_id(conn)))
                cur=await conn.execute(f'SELECT {TRIP_COLUMNS} FROM trips WHERE account_id=%s ORDER BY started_at DESC',(account_id(conn),))
                rows=await cur.fetchall()
                columns=[column.name for column in cur.description]
                expected=to_csv([dict(zip(columns,row)) for row in rows],await load_rates(conn),TZ)
            original=ExportProjection.rates
            mutated=False
            async def mutate_after_import(projection,conn,owner):
                nonlocal mutated
                async with raw.connection() as concurrent:
                    await concurrent.execute("UPDATE trips SET purpose='Later purpose',notes='Later notes' WHERE account_id=%s",(owner,))
                    await concurrent.execute("UPDATE account_settings SET display_tz='UTC' WHERE account_id=%s",(owner,))
                    await concurrent.execute("UPDATE mileage_rates SET rate_per_mi=9 WHERE account_id=%s",(owner,))
                mutated=True
                await original(projection,conn,owner)
            monkeypatch.setattr(ExportProjection,'rates',mutate_after_import)
            async with manager.operation('foreground',account.principal):
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    async with report_account_work(control,account.principal) as lease:
                        request=SimpleNamespace(state=SimpleNamespace(_preparation=operation,_report_control_connection=lease,principal=account.principal,account_pool=account))
                        messages=[]
                        async def send(message): messages.append(message)
                        async def prepare_and_send():
                            response=await prepare_export(request,{'id':account.principal.account_id})
                            await response({},None,send)
                        await operation.perform(prepare_and_send)
                        assert mutated and b''.join(message.get('body',b'') for message in messages)==expected
    asyncio.run(run())


def test_eight_search_fields_keep_database_ilike_and_literal_escape_semantics(tmp_path):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with account.connection() as conn:
                await _seed(conn)
                owner=account_id(conn)
                await conn.execute('UPDATE trips SET notes=%s,purpose=%s WHERE account_id=%s',('Note%_\\Marker','Purpose%_\\Marker',owner))
                await conn.execute("UPDATE trips SET start_label=%s,end_label=%s WHERE account_id=%s AND source='manual'",('StartLabel%_\\Marker','EndLabel%_\\Marker',owner))
                await conn.execute('UPDATE places SET name=%s WHERE account_id=%s',('Place%_\\Marker',owner))
                await conn.execute('UPDATE geocode_cache SET address=%s WHERE account_id=%s',('Address%_\\Marker',owner))
            async with manager.operation('foreground',account.principal):
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    store=TextStore(operation.budget)
                    try:
                        async with account.connection() as conn:
                            for q in ('note%_\\marker','purpose%_\\marker','startlabel%_\\marker','endlabel%_\\marker','place%_\\marker','address%_\\marker','NO MATCH','%_\\'):
                                metadata=dict(category='',exclusion='',from_dt=None,to_dt=None,vehicle=None,**pattern(store,text(store,'q',q),conn.info.encoding))
                                await assemble_search_pattern(ExportProjection(operation,None),conn,metadata)
                                from app.export_preparation import filter_sql
                                where,params=filter_sql(metadata,account_id(conn))
                                expected_where,expected_params=_trip_filter_sql('',None,None,q=q,owner_id=account_id(conn))
                                actual=await (await conn.execute(f'SELECT id FROM trips {where} ORDER BY started_at DESC',params)).fetchall()
                                expected=await (await conn.execute(f'SELECT id FROM trips {expected_where} ORDER BY started_at DESC',expected_params)).fetchall()
                                assert actual==expected
                    finally: store.close()
    asyncio.run(run())


@pytest.mark.parametrize('format',['csv','xlsx'])
@pytest.mark.parametrize('client,notes',[('UTF8','車😀,%"\r\n'*20000),('LATIN1','Café,%"\r\n'*20000),('SJIS','¥～車,%"\r\n'*20000)],ids=['utf8','latin1','sjis'])
def test_full_stored_client_conversion_and_unicode_prefix_match_oracle(tmp_path,format,client,notes):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with account.connection() as conn:
                await _seed(conn)
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s',(str(TZ),account_id(conn)))
                await conn.execute('UPDATE trips SET notes=%s WHERE account_id=%s',(notes,account_id(conn)))
            class ClientPool:
                @asynccontextmanager
                async def connection(self,**kwargs):
                    async with account.connection(**kwargs) as conn:
                        await conn.execute("SELECT set_config('client_encoding',%s,true)",(client,))
                        yield conn
            selected=ClientPool()
            async with manager.operation('foreground',account.principal):
                async with selected.connection() as conn:
                    cur=await conn.execute(f'SELECT {TRIP_COLUMNS} FROM trips WHERE account_id=%s ORDER BY started_at DESC',(account_id(conn),))
                    rows=await cur.fetchall();columns=[column.name for column in cur.description]
                    trips=[dict(zip(columns,row)) for row in rows]
                    rates=await load_rates(conn)
                    expected=to_csv(trips,rates,TZ) if format=='csv' else to_xlsx(trips,rates,TZ)
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    async with report_account_work(control,account.principal) as lease:
                        request=SimpleNamespace(state=SimpleNamespace(_preparation=operation,_report_control_connection=lease,principal=account.principal,account_pool=selected))
                        messages=[]
                        async def send(message): messages.append(message)
                        async def run_export():
                            response=await prepare_export(request,{'id':account.principal.account_id},format=format)
                            await response({},None,send)
                        await operation.perform(run_export)
                        actual=b''.join(message.get('body',b'') for message in messages)
                        assert actual==expected if format=='csv' else signature(actual)==signature(expected)
                        assert operation.closed and not operation.directory.exists()
    asyncio.run(run())


def test_xlsx_does_not_suppress_client_conversion_error_beyond_old_prefix(tmp_path):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with account.connection() as conn:
                await _seed(conn)
                await conn.execute('UPDATE trips SET notes=%s WHERE account_id=%s',('a'*32768+'車',account_id(conn)))
            class ClientPool:
                @asynccontextmanager
                async def connection(self,**kwargs):
                    async with account.connection(**kwargs) as conn:
                        await conn.execute("SELECT set_config('client_encoding','LATIN1',true)")
                        yield conn
            selected=ClientPool()
            from psycopg.errors import UntranslatableCharacter
            async with manager.operation('foreground',account.principal):
                with pytest.raises(UntranslatableCharacter):
                    async with selected.connection() as conn:
                        await (await conn.execute('SELECT notes FROM trips')).fetchall()
                with pytest.raises(UntranslatableCharacter):
                    async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                        async with report_account_work(control,account.principal) as lease:
                            request=SimpleNamespace(state=SimpleNamespace(_preparation=operation,_report_control_connection=lease,principal=account.principal,account_pool=selected))
                            await operation.perform(prepare_export,request,{'id':account.principal.account_id},format='xlsx')
                assert operation.closed and not operation.directory.exists()
                assert operation.process.returncode is not None
    asyncio.run(run())


def test_client_sqlascii_keeps_legacy_bytes_timezone_failure(tmp_path):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            class ClientPool:
                @asynccontextmanager
                async def connection(self,**kwargs):
                    async with account.connection(**kwargs) as conn:
                        await conn.execute("SELECT set_config('client_encoding','SQL_ASCII',true)")
                        yield conn
            selected=ClientPool()
            from zoneinfo import ZoneInfo
            async with manager.operation('foreground',account.principal):
                with pytest.raises(TypeError):
                    async with selected.connection() as conn:
                        value=(await (await conn.execute('SELECT display_tz FROM account_settings')).fetchone())[0]
                        ZoneInfo(value)
                with pytest.raises(TypeError):
                    async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                        async with report_account_work(control,account.principal) as lease:
                            request=SimpleNamespace(state=SimpleNamespace(_preparation=operation,_report_control_connection=lease,principal=account.principal,account_pool=selected))
                            await operation.perform(prepare_export,request,{'id':account.principal.account_id})
                assert operation.closed and not operation.directory.exists()
    asyncio.run(run())
