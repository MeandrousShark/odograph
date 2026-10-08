import asyncio
import time
from types import SimpleNamespace

import pytest

from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.export_preparation import ExportProjection, assemble_search_pattern, filter_sql
from app.export_filters import SEARCH_SETTING, TextStore, pattern
from app.preparation import FRAME_BYTES, _json_frame
from app.preparation_resources import SpoolReservation, ResourceBudget
from app.report_preparation import TEXT_CHARS, _DETAIL_TEXT
from app.ui._common import _trip_filter_sql
from test_export_filter_preparation import text

pytestmark=pytest.mark.unit


class Operation:
    def check(self): pass
    def remaining_ms(self): return 15000


class Session:
    def __init__(self): self.commands=[];self.chunks=[]
    async def send_command(self,value): _json_frame(value);self.commands.append(value)
    async def send_text(self,value): assert len(value)<=FRAME_BYTES;self.chunks.append(value)


def test_filter_sql_reuses_nonsearch_predicates_and_eight_original_expressions():
    metadata=dict(category='business',exclusion='not_deductible',vehicle='none',from_dt=None,to_dt=None,search=True)
    where,params=filter_sql(metadata,7)
    base,expected=_trip_filter_sql('business',None,None,'none',exclusion='not_deductible',owner_id=7)
    assert where.startswith(base+' AND (') and params==expected
    assert where.count('ILIKE')==8 and where.count(SEARCH_SETTING)==8
    metadata['search']=False
    assert filter_sql(metadata,7)==(base,expected)


def test_fragment_stream_keeps_delivered_batch_order_and_full_csv_references():
    fields=tuple(_DETAIL_TEXT)
    batch=[];sources={}
    for identity in (10,2):
        row={'id':identity}
        for field in fields:
            value='車😀_%"\r\n'*12000 if field=='notes' else str(identity)+field
            sources[identity,field]=value
            row[field+'_size']=len(value.encode('utf8'))
        batch.append(row)
    class Cursor:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def stream(self,query,params,*,size):
            assert size==1 and params==(FRAME_BYTES,FRAME_BYTES,[10,2],*(['UTF8']*len(fields)),FRAME_BYTES,FRAME_BYTES,7)
            assert 'WITH ORDINALITY' in query and 'ORDER BY chosen.ordinal' in query
            for identity in (10,2):
                for number,field in enumerate(fields):
                    value=sources[identity,field].encode('utf8')
                    for offset in range(0,len(value),FRAME_BYTES):
                        yield dict(id=identity,number=number,part=offset//FRAME_BYTES+1,value=value[offset:offset+FRAME_BYTES])
    class Connection:
        info=SimpleNamespace(encoding='utf8',parameter_status=lambda key:'UTF8')
        def cursor(self,**kwargs): return Cursor()
        async def execute(self,*args): pass
    projection=ExportProjection(Operation(),Session())
    asyncio.run(projection.text_batch(Connection(),7,batch,_DETAIL_TEXT))
    spool=b''.join(projection.session.chunks)
    for row in batch:
        for field in fields:
            offset,size,codec=row[field]['text']
            assert spool[offset:offset+size].decode(codec)==sources[row['id'],field]


def test_pattern_assembly_only_adapts_one_bounded_binary_record_at_a_time(tmp_path):
    reservation=SpoolReservation.acquire(tmp_path/'spool',time.monotonic()+5)
    budget=ResourceBudget(reservation.directory);store=TextStore(budget)
    try:
        metadata=pattern(store,text(store,'q','車%_\\'*30000),'utf8')
        class Projection:
            operation=SimpleNamespace(budget=budget)
            def __init__(self): self.calls=0
            async def scalar(self,conn,query,params=()):
                self.calls+=1
                assert 'octet_length(set_config' in query
                if params:
                    assert '%b' in query and len(params)==2
                    assert isinstance(params[0],bytes) and len(params[0])<=FRAME_BYTES and params[1]=='UTF8'
                return {'size':0}
        projection=Projection();conn=SimpleNamespace(info=SimpleNamespace(parameter_status=lambda key:'UTF8'))
        async def run():
            manager=AdmissionManager()
            async with manager.operation('foreground',AccountPrincipal(1,True,1)):
                await assemble_search_pattern(projection,conn,metadata)
        asyncio.run(run())
        assert projection.calls==metadata['pattern_records']+1
        assert not budget.path('export-pattern').exists()
    finally:
        store.close();budget.close();reservation.release()


def test_effective_rate_query_omits_only_unreachable_years():
    class Projection(ExportProjection):
        async def rows(self,conn,query,params):
            assert 'year BETWEEN 1 AND 9999' in query and 'max(year)' in query and 'year<=0' in query
            assert params==('UTF8','UTF8',7,7)
            for year in (0,1,2026,9999):
                yield dict(year=year,size1=100000,size2=None,h2_start_month=None)
        async def text(self,*args):
            assert args[2]==100000
    projection=Projection(Operation(),Session())
    asyncio.run(projection.rates(SimpleNamespace(info=SimpleNamespace(parameter_status=lambda key:'UTF8')),7))
    assert [command['row']['year'] for command in projection.session.commands]==[0,1,2026,9999]


def test_request_text_descriptor_scan_checks_deadline_and_yields_before_streaming():
    class CountingOperation(Operation):
        def __init__(self): self.checks=0
        def check(self): self.checks+=1
    async def run():
        operation=CountingOperation();session=Session()
        projection=ExportProjection(operation,session)
        progressed=[]
        async def witness():
            await asyncio.sleep(0)
            progressed.append(operation.checks)
        task=asyncio.create_task(witness())
        value='😀'*(TEXT_CHARS*3+1)
        await projection.literal_text('q',value)
        await task
        assert 0<progressed[0]<operation.checks==8
        assert session.commands==[{'type':'text','key':'q','size':len(value.encode('utf8'))}]
        assert b''.join(session.chunks)==value.encode('utf8')
    asyncio.run(run())


def test_cancelled_pattern_open_settles_and_closes_actual_file_before_owner_exit():
    import threading
    from app.export_preparation import pattern_source
    started,released=threading.Event(),threading.Event()
    class File:
        closed=False
        def close(self): self.closed=True
    file=File()
    class Budget:
        def open(self,*args):
            started.set()
            assert released.wait(2)
            return file
    async def run():
        manager=AdmissionManager()
        async with manager.operation('foreground',AccountPrincipal(1,True,1)) as owner:
            async def opening():
                async with pattern_source(Budget()):
                    raise AssertionError('cancelled input must not begin assembly')
            task=asyncio.create_task(opening())
            while not started.is_set(): await asyncio.sleep(.001)
            task.cancel();await asyncio.sleep(.001);task.cancel()
            assert not task.done() and owner._lifetime.threads and not file.closed
            released.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert file.closed and not owner._lifetime.threads
    asyncio.run(run())
