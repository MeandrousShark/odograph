import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.preparation import _json_frame
from app.report_preparation import Projection, TEXT_CHARS, FETCH_ROWS, _encode, _WHITESPACE, _whitespace_bytes, _purpose_projection

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


@pytest.mark.parametrize('server,expected',[
    ('UTF8',_WHITESPACE),('LATIN1',''.join(c for c in _WHITESPACE if ord(c)<256)),
])
def test_trim_bytes_follow_server_encoding_independently_of_client(server,expected):
    class Info:
        def parameter_status(self,name):
            assert name=='server_encoding'
            return server
        @property
        def encoding(self):
            raise AssertionError('client encoding must not filter stored characters')
    class Connection:
        info=Info()
    assert _whitespace_bytes(Connection())==expected.encode('utf8')


@pytest.mark.parametrize('server',['EUC_TW','MULE_INTERNAL'])
def test_unmapped_server_trim_fallback_is_ascii_and_character_chunked(server):
    import re
    from types import SimpleNamespace
    conn=SimpleNamespace(info=SimpleNamespace(parameter_status=lambda name:server))
    sql,pattern=_purpose_projection(conn)
    assert 'generate_series' in sql and sql.count(str(TEXT_CHARS))==2
    assert "convert_to(substring(purpose" in sql
    assert len(pattern.encode('ascii'))==163
    whitespace=re.compile(pattern)
    for value in (_WHITESPACE*2000,'\u3000'*TEXT_CHARS+'Visit','\u3000'*TEXT_CHARS+'\xa0','','\x85',None):
        text=value or ''
        actual=any(whitespace.fullmatch(text[i:i+TEXT_CHARS].encode('utf8').hex()) is None for i in range(0,len(text),TEXT_CHARS))
        assert actual==bool(text.strip())


def test_sql_ascii_removes_complete_utf8_whitespace_across_byte_boundaries():
    from types import SimpleNamespace
    conn=SimpleNamespace(info=SimpleNamespace(parameter_status=lambda name:'SQL_ASCII'))
    sql,param=_purpose_projection(conn)
    assert sql.count('replace(')==29 and sql.count('%s')==1
    assert len(sql.encode('ascii'))<2500
    assert param==b'\xe3\x80\x80'
    for text in ('\u0085'*8191+'\u3000','\u3000'*5460+'\u0085','\u3000'*5461+'Visit','車','\u0085\u2003\u3000',''):
        value=text.encode('utf8')
        for character in _WHITESPACE:
            value=value.replace(character.encode('utf8'),b'')
        assert bool(value)==bool(text.strip())


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
