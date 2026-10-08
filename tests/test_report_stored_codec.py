"""Stored report text keeps full psycopg decoding before replay and prefixes."""
import time

import pytest

from app.preparation_resources import ResourceBudget, SpoolReservation
from app.report_renderer import Renderer

pytestmark = pytest.mark.unit


@pytest.fixture
def renderer(tmp_path):
    reservation = SpoolReservation.acquire(tmp_path/'spool', time.monotonic()+5)
    budget = ResourceBudget(reservation.directory)
    result = Renderer(budget)
    try:
        yield result
    finally:
        result.close()
        budget.close()
        reservation.release()


def stored(renderer, key, value, codec, chunk=7):
    raw = value.encode(codec)
    pieces = iter(raw[i:i+chunk] for i in range(0,len(raw),chunk))
    class Channel:
        def recv_text(self): return next(pieces)
    renderer.text(key,len(raw),Channel(),codec)
    return renderer.refs[key],raw.decode(codec)


@pytest.mark.parametrize('codec,value',[
    ('utf8','車😀__ODOGRAPH_TEXT_0000000000000000_0000000000000001__'*2000),
    ('latin1','Caféß,%"\r\n'*10000),('shift_jis','¥〜車,%"\r\n'*10000),
],ids=['utf8','latin1','sjis'])
def test_typed_refs_preserve_complete_decode_and_logical_prefix(renderer,codec,value):
    ref,expected = stored(renderer,'vehicle_name',value,codec)
    assert ref[2]==codec and ''.join(renderer.chunks(ref))==expected
    assert renderer.prefix(ref)==expected[:32767]
    token=renderer.token(ref)
    from app.report_renderer import _TOKEN
    match=_TOKEN.fullmatch(token.encode())
    assert match is not None and bytes.fromhex(match[3].decode()).decode()==codec
    local=(ref[0],ref[1],codec,13)
    assert ''.join(renderer.chunks(local))==expected[:13]


@pytest.mark.parametrize('value',['','\u3000\t\r\n'*10000,'\u3000\t¥\u3000'])
def test_trip_purpose_counts_python_whitespace_after_client_decode(renderer,value):
    ref,expected=stored(renderer,'field:purpose',value,'shift_jis')
    assert renderer.trip({'purpose':{'text':ref}})['purpose_nonblank']==bool(expected.strip())
    assert renderer.trip({'purpose':None})['purpose_nonblank'] is False


def test_complete_decoder_rejects_suffix_before_xlsx_prefix(renderer):
    raw=b'a'*32768+b'\xff'
    class Channel:
        def recv_text(self): return raw
    with pytest.raises(UnicodeDecodeError): renderer.text('field:notes',len(raw),Channel(),'utf8')
    assert 'field:notes' not in renderer.refs


def test_client_sqlascii_settings_keep_type_error_before_decoding(renderer):
    class Channel:
        def recv_text(self): raise AssertionError('must fail before decoding bytes')
    with pytest.raises(TypeError): renderer.text('display_tz',99,Channel(),'ascii')


def test_all_locked_postgresql_codec_tokens_fit_the_incremental_replay_bound():
    from psycopg._encodings import py_codecs
    from app.report_renderer import _TOKEN
    for codec in set(py_codecs.values()):
        token=Renderer.token((2**64-1,2**64-1,codec,2**64-1)).encode()
        assert len(token)<=128 and _TOKEN.fullmatch(token) is not None
