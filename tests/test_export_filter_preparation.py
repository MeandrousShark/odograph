import time

import pytest
from psycopg.errors import DataError
from zoneinfo import ZoneInfo

from app.export_filters import TextStore, RECORD, pattern, prepare_filters
from app.preparation_resources import SpoolReservation, ResourceBudget
from app.ui._common import _escape_ilike_term, parse_date_range, _parse_vehicle_id

pytestmark=pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    reservation=SpoolReservation.acquire(tmp_path/'spool',time.monotonic()+5)
    result=TextStore(ResourceBudget(reservation.directory))
    try: yield result
    finally:
        result.close();result.budget.close();reservation.release()


def text(store,key,value,size=16380):
    raw=value.encode('utf8')
    iterator=iter(raw[i:i+size] for i in range(0,len(raw),size))
    class Channel:
        def recv_text(self): return next(iterator)
    store.receive(key,len(raw),Channel())
    return store.refs[key]


def records(store,result):
    output=[]
    with store.budget.open(result['pattern_path'],'rb') as source:
        while header:=source.read(RECORD.size):
            size,=RECORD.unpack(header)
            assert 0<size<=65520
            output.append(source.read(size))
    assert len(output)==result['pattern_records']
    assert sum(map(len,output))==result['pattern_bytes']
    return output


@pytest.mark.parametrize('chunk',[1,7,16380,65520])
def test_search_strip_and_escape_are_exact_across_source_boundaries(store,chunk):
    white=''.join(chr(i) for i in range(0x3100) if chr(i).isspace())
    value=white*700+'車\\%_\ufeff\n'+('a\\%_'*5000)+white*800
    result=pattern(store,text(store,'q',value,chunk),'utf8')
    actual=b''.join(records(store,result)).decode('utf8')
    assert actual=='%'+_escape_ilike_term(value.strip())+'%'


@pytest.mark.parametrize('codec,value',[
    ('latin1','\u3000 Café%_\\\u3000'),('shift_jis','\u3000¥\\%_車\u3000'),
    ('utf8','\u2003車😀ß%_\\\u202f'),
])
def test_pattern_encoding_matches_effective_client_dumper_including_lossy_mapping(store,codec,value):
    result=pattern(store,text(store,'q',value),codec)
    assert b''.join(records(store,result))==('%'+_escape_ilike_term(value.strip())+'%').encode(codec)


@pytest.mark.parametrize('value,error', [('a\x00b',DataError),('車',UnicodeEncodeError),('hello\ud800',UnicodeEncodeError)])
def test_pattern_rejects_original_invalid_client_inputs(store,value,error):
    if '\ud800' in value:
        # Direct callers with surrogates fail UTF-8 source streaming, too.
        with pytest.raises(error): text(store,'q',value)
    else:
        with pytest.raises(error): pattern(store,text(store,'q',value),'latin1')


def test_blank_query_does_not_create_or_read_a_pattern(store):
    result=pattern(store,text(store,'q','\u3000\t\xa0'*20000),'latin1')
    assert result=={'search':False}
    assert not store.budget.path('export-pattern').exists()


@pytest.mark.parametrize('from_,to,vehicle',[
    ('','junk','garbage'),('2026-03-08','2026-11-01','none'),('2026-07-01','2026-01-01','-2'),
])
def test_filter_dates_and_vehicle_preserve_existing_forgiving_parsers(store,from_,to,vehicle):
    for key,value in (('from',from_),('to',to),('vehicle',vehicle),('q',''),('category','bogus'),('exclusion','none')):
        text(store,key,value)
    tz=ZoneInfo('America/New_York')
    result=prepare_filters(store,tz,'utf8')
    bounds=parse_date_range(from_,to,tz)
    assert (result['from_dt'],result['to_dt'])==tuple(v.isoformat() if v else None for v in bounds)
    assert result['vehicle']==_parse_vehicle_id(vehicle)
    assert result['category']=='' and not result['search']


def test_date_overflow_preserves_original_failure(store):
    for key,value in (('from',''),('to','9999-12-31'),('vehicle',''),('q',''),('category',''),('exclusion','')):
        text(store,key,value)
    with pytest.raises(OverflowError): prepare_filters(store,ZoneInfo('UTC'),'utf8')


@pytest.mark.parametrize('codec,value',[('latin1','Café%_\\'*10000),('shift_jis','車¥%_\\'*10000),('utf8','車😀'*40000)])
def test_stored_client_encoded_refs_decode_all_frames_before_character_prefix(store,codec,value):
    raw=value.encode(codec)
    expected=raw.decode(codec)
    chunks=iter(raw[i:i+7] for i in range(0,len(raw),7))
    channel=type('Channel',(),{'recv_text':lambda self:next(chunks)})()
    store.receive('field:notes',len(raw),channel,codec)
    ref=store.refs['field:notes']
    assert ref[2]==codec and ''.join(store.chunks(ref))==expected
    assert store.prefix(ref)==expected[:32767]


def test_stored_decoder_checks_invalid_suffix_beyond_xlsx_prefix(store):
    raw=b'a'*32768+b'\xff'
    chunks=iter(raw[i:i+65520] for i in range(0,len(raw),65520))
    channel=type('Channel',(),{'recv_text':lambda self:next(chunks)})()
    with pytest.raises(UnicodeDecodeError): store.receive('field:notes',len(raw),channel,'utf8')
    assert 'field:notes' not in store.refs


def test_client_sqlascii_timezone_rejects_bytes_before_any_decoding(store):
    class Channel:
        def recv_text(self): raise AssertionError('legacy bytes type failure precedes decoding')
    with pytest.raises(TypeError): store.receive('display_tz',100,Channel(),'ascii')
