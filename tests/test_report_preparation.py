import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.preparation import _json_frame
from app.report_preparation import Projection, TEXT_CHARS, FETCH_ROWS, _encode, _WHITESPACE

pytestmark = pytest.mark.unit


class Operation:
    def check(self): pass
    def remaining_ms(self): return 17000


class Session:
    def __init__(self): self.commands, self.chunks = [], []
    async def send_command(self, value):
        _json_frame(value)
        self.commands.append(value)
    async def send_text(self, value):
        assert len(value) <= 65520
        self.chunks.append(value)


class TextProjection(Projection):
    def __init__(self, text):
        super().__init__(Operation(),Session())
        self.source = text
        self.reads = []
    async def scalar(self, conn, query, params=()):
        assert 'substring(' in query
        position,size,*identity = params
        assert size == TEXT_CHARS and identity == [7]
        self.reads.append((position,size))
        return {'value':self.source[position-1:position-1+size]}


def test_source_chunks_are_character_bounded_before_parent_decode():
    value = '😀<&ß' * 40000
    projection = TextProjection(value)
    ref = asyncio.run(projection.text(None,'email',len(value.encode()),'email','accounts','id=%s',(7,)))
    assert ref == [0,len(value.encode())]
    assert b''.join(projection.session.chunks) == value.encode()
    assert max(map(len,projection.session.chunks)) <= 65520
    assert len(projection.reads) > 1
    assert projection.text_offset == len(value.encode())


def test_scalar_codec_preserves_float_bits_and_decimal_text():
    row = {'float':-0.0,'decimal':Decimal('1.00000000000000000000000001'),'date':date(2026,1,1),'time':datetime(2026,1,1,tzinfo=timezone.utc)}
    encoded = _encode(row)
    assert encoded['float'] == '-0x0.0p+0'
    assert encoded['decimal'] == str(row['decimal'])
    assert encoded['date'] == '2026-01-01'
    assert encoded['time'] == '2026-01-01T00:00:00+00:00'
    _json_frame(encoded)


def test_python_strip_set_includes_all_unicode_whitespace():
    assert _WHITESPACE == ''.join(chr(i) for i in range(0x110000) if chr(i).isspace())


def test_server_portal_fetches_no_more_than_256_without_prefetch():
    class Cursor:
        def __init__(self): self.fetches=0;self.closed=False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): self.closed=True
        async def execute(self,*args): pass
        async def fetchmany(self,size):
            assert size == FETCH_ROWS == 256
            self.fetches += 1
            return [{'id':i} for i in range(256)] if self.fetches==1 else []
    class Connection:
        def __init__(self): self.portal=Cursor()
        def cursor(self,**kwargs): assert kwargs['name']=='report_1';return self.portal
        async def execute(self,query,params): assert params == ('15000',)
    async def run():
        projection=Projection(Operation(),Session());conn=Connection()
        rows=projection.rows(conn,'SELECT id FROM trips')
        first=await anext(rows)
        assert first['id']==0 and conn.portal.fetches==1
        await asyncio.sleep(0)
        assert conn.portal.fetches==1
        await rows.aclose()
        assert conn.portal.closed and conn.portal.fetches==1
    asyncio.run(run())


def test_previous_scalar_batch_is_released_before_next_fetch():
    import weakref
    class Row(dict): pass
    class Cursor:
        def __init__(self): self.fetches=0; self.previous=[]
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def execute(self,*args): pass
        async def fetchmany(self,size):
            self.fetches+=1
            if self.fetches==1:
                rows=[Row(id=i) for i in range(size)]
                self.previous=[weakref.ref(row) for row in rows]
                return rows
            assert all(ref() is None for ref in self.previous)
            return []
    class Connection:
        def __init__(self): self.portal=Cursor()
        def cursor(self,**kwargs): return self.portal
        async def execute(self,*args): pass
    async def run():
        projection=Projection(Operation(),Session())
        count=0
        async for row in projection.rows(Connection(),'SELECT id FROM trips'):
            count+=1
            del row
        assert count==256
    asyncio.run(run())


def test_detail_batch_single_row_stream_preserves_refs_and_prefix():
    from app.report_preparation import _DETAIL_TEXT
    fields=tuple(_DETAIL_TEXT)
    sources={}
    batch=[]
    for i in (2,10):
        row={'id':i}
        for field in fields:
            value=('車ß<&' * 12000) if field=='notes' else str(i)+field
            value=value[:32767]
            sources[i,field]=value
            row[field+'_size']=len(value.encode())
        batch.append(row)
    class Cursor:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def stream(self,query,params,*,size):
            assert size==1 and params[:4]==(TEXT_CHARS,)*4 and params[4:]==(7,[2,10])
            assert 'generate_series' in query
            for i in (2,10):
                for number,field in enumerate(fields):
                    value=sources[i,field]
                    for offset in range(0,len(value),TEXT_CHARS):
                        yield {'id':i,'number':number,'part':offset//TEXT_CHARS+1,'value':value[offset:offset+TEXT_CHARS]}
    class Connection:
        def cursor(self,**kwargs): return Cursor()
        async def execute(self,*args): pass
    projection=Projection(Operation(),Session())
    asyncio.run(projection.detail_text_batch(Connection(),7,batch))
    spool=b''.join(projection.session.chunks)
    for row in batch:
        for field in fields:
            offset,length=row[field]['text']
            assert spool[offset:offset+length].decode()==sources[row['id'],field]
    assert len(projection.session.commands)==len(fields)*2


def test_timezone_paths_json_is_escaped_before_bounded_encoding():
    import json
    from app.report_preparation import _timezone_path_chunks
    paths=('/'+('😀ß\\\"\x01' * 30000),'/surrogate\udcff','/plain')
    chunks=list(_timezone_path_chunks(paths))
    assert max(map(len,chunks)) <= 49152
    assert json.loads(b''.join(chunks)) == list(paths)
