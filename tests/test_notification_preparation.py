import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.notification_preparation import (
    EmailTurnSelection, FLAGS, KINDS, Projection, QuarterlyJob,
    prepare_quarterly_message, quarterly_preferences_current, select_email_turn,
    _quarter_bounds,
    _open_captured,
)
from app.preparation import FRAME_BYTES, _json_frame
from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager

pytestmark = pytest.mark.unit


class Operation:
    def check(self): pass
    def remaining_ms(self): return 17000


class Session:
    def __init__(self): self.commands, self.chunks, self.finished = [], [], False
    async def send_command(self, value):
        _json_frame(value)
        self.commands.append(value)
    async def send_text(self, value):
        assert len(value) <= FRAME_BYTES
        self.chunks.append(value)
    async def request(self, value):
        await self.send_command(value)
        return {'due_count': 3, 'artifacts': {'mime': 'mail-mime'}} if value['type'] == 'render' else {'current': True}
    async def finish_input(self): self.finished = True


@pytest.fixture(autouse=True)
def account(monkeypatch):
    monkeypatch.setattr('app.notification_preparation.account_id', lambda conn: 41)


@pytest.mark.parametrize('flags,cursor,expected', [
    ((True,True,True,True), None, EmailTurnSelection(KINDS[0],0,True,1)),
    ((True,True,True,True), 2, EmailTurnSelection(KINDS[2],2,True,3)),
    ((False,False,False,True), 0, EmailTurnSelection(KINDS[3],3,False,None)),
    ((True,True,True,True), 4, EmailTurnSelection(None)),
    ((False,False,False,False), None, EmailTurnSelection(None)),
])
def test_scalar_selector_preserves_enabled_kind_cursor(monkeypatch, flags, cursor, expected):
    async def scalar(self, conn, query, params):
        assert 'FOR SHARE' in query and params == (41,)
        assert 'octet_length' in query and 'display_tz' not in query
        return dict(zip(FLAGS, flags), to_size=4)
    monkeypatch.setattr(Projection, 'scalar', scalar)
    assert asyncio.run(select_email_turn(None, cursor, operation=Operation())) == expected


def test_complete_literal_is_framed_before_encoding():
    value = '😀é<&' * 100000
    projection = Projection(Operation(), Session())
    asyncio.run(projection.literal('email_from', value))
    assert b''.join(projection.session.chunks) == value.encode('utf8')
    assert projection.session.commands == [{'type':'text','key':'email_from','size':len(value.encode('utf8')),'encoding':'utf8','literal':True}]
    assert max(map(len, projection.session.chunks)) <= FRAME_BYTES


def test_locked_database_text_projects_before_parent_materialization():
    source = '😀é<&' * 50000
    class TextProjection(Projection):
        async def scalar(self, conn, query, params=()):
            assert "substring(convert_to(email_to,current_setting('client_encoding'))" in query
            position,size,owner = params
            assert size == FRAME_BYTES and owner == 41
            return {'value':source.encode('utf8')[position-1:position-1+size]}
    projection = TextProjection(Operation(),Session())
    asyncio.run(projection.text(SimpleNamespace(info=SimpleNamespace(encoding='utf-8')),
        'email_to',len(source.encode('utf8')),'email_to'))
    assert b''.join(projection.session.chunks) == source.encode('utf8')
    assert max(map(len,projection.session.chunks)) <= FRAME_BYTES


def test_locked_preferences_short_circuit_fixed_scalar_changes(monkeypatch):
    async def scalar(self, conn, query, params):
        assert 'FOR SHARE' in query
        return {'tz_size': 3, 'to_size': 1, 'email_odometer_reminder': True, 'odometer_reminder_hour': 10}
    monkeypatch.setattr(Projection, 'scalar', scalar)
    session = Session()
    job = QuarterlyJob(Operation(), session, datetime.now(timezone.utc), 9)
    assert not asyncio.run(quarterly_preferences_current(None, job))
    assert not session.commands


def test_schedule_failures_keep_original_type_and_fixed_stage():
    class Session:
        async def request(self,command):
            assert command == {'type':'quarter_bounds','hour':9}
            raise ValueError('preparation helper failed')
    with pytest.raises(ValueError) as failure:
        asyncio.run(_quarter_bounds(Session(),9))
    assert failure.value.preparation_stage == 'schedule'


def test_one_statement_streams_names_without_prefetch_and_closes(monkeypatch):
    names = ['', '😀é<&' * 40000, 'duplicate']
    frames = []
    for identity, value in enumerate(names):
        raw = value.encode('utf8')
        for offset in range(0, max(len(raw), 1), FRAME_BYTES):
            frames.append({'id': identity, 'size': len(raw), 'position': offset+1, 'value': raw[offset:offset+FRAME_BYTES],'due':True})
    class Cursor:
        closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed = True
        async def stream(self, query, params, *, size):
            assert size == 1 and 'NOT EXISTS' in query and 'generate_series' in query
            assert params[1] == 41
            for row in frames:
                yield row
    cursor = Cursor()
    class Connection:
        info = SimpleNamespace(encoding='utf-8')
        def cursor(self, **kwargs): return cursor
        async def execute(self, query, params): assert params == ('15000',)
    session = Session()
    job = QuarterlyJob(Operation(), session, datetime.now(timezone.utc), 9)
    result = asyncio.run(prepare_quarterly_message(Connection(), job))
    assert result.due_count == 3 and session.finished and cursor.closed
    assert b''.join(session.chunks) == ''.join(names).encode()
    assert [command['id'] for command in session.commands if command['type'] == 'vehicle'] == [0,1,2]
    assert len([command for command in session.commands if command['type'] == 'text']) == 3


def test_stream_cancel_closes_actual_cursor_before_return():
    class Cursor:
        closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed = True
        async def stream(self, *args, **kwargs):
            yield {'id': 1, 'size': 3, 'position': 1, 'value': b'abc','due':True}
            raise asyncio.CancelledError
    cursor = Cursor()
    class Connection:
        info = SimpleNamespace(encoding='utf-8')
        def cursor(self, **kwargs): return cursor
        async def execute(self, *args): pass
    async def run():
        job = QuarterlyJob(Operation(), Session(), datetime.now(timezone.utc), 9)
        with pytest.raises(asyncio.CancelledError):
            await prepare_quarterly_message(Connection(), job)
        assert cursor.closed and not job.session.finished
    asyncio.run(run())


def test_sql_ascii_bytes_client_preserves_initial_settings_type_error():
    projection = Projection(Operation(),Session())
    with pytest.raises(TypeError):
        asyncio.run(projection.text(SimpleNamespace(info=SimpleNamespace(encoding='ascii')),
            'display_tz',3,'display_tz'))
    assert not projection.session.commands


@pytest.mark.parametrize('kind',['literal','timezone_paths'])
def test_metadata_length_pass_yields_before_finishing(kind,monkeypatch):
    value = '😀' * 100000
    # Patch only the consumer, preserving zoneinfo's dynamic TZPATH lookup.
    monkeypatch.setattr('app.notification_preparation.zoneinfo',SimpleNamespace(TZPATH=('/'+value,)))
    async def run():
        projection = Projection(Operation(),Session())
        coroutine = projection.literal('email_from',value) if kind=='literal' else projection.timezone_paths()
        task = asyncio.create_task(coroutine)
        await asyncio.sleep(0)
        assert not task.done() and not projection.session.commands
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not projection.session.commands
    asyncio.run(run())


def test_cancelled_replay_open_and_close_settle_actual_handle_before_lease_exit(tmp_path):
    path = tmp_path/'source'
    path.write_bytes(b'complete source')
    opened,allow_open,closing,allow_close = (threading.Event() for _ in range(4))
    class Source:
        def __init__(self): self.raw = path.open('rb')
        @property
        def closed(self): return self.raw.closed
        def close(self):
            closing.set()
            assert allow_close.wait(5)
            self.raw.close()
    class Budget:
        source = None
        def open(self,*args):
            self.source = Source()
            opened.set()
            assert allow_open.wait(5)
            return self.source
    budget = Budget()
    captured = SimpleNamespace(operation=SimpleNamespace(budget=budget))
    async def run():
        manager = AdmissionManager()
        async with manager.operation('foreground',AccountPrincipal(41,True,1)):
            async with manager.lease((41,)):
                task = asyncio.create_task(_open_captured(captured))
                try:
                    while not opened.is_set(): await asyncio.sleep(.001)
                    task.cancel()
                    task.cancel()
                    await asyncio.sleep(.01)
                    assert not task.done() and not budget.source.closed and manager.snapshot()['leases'] == 1
                    allow_open.set()
                    while not closing.is_set(): await asyncio.sleep(.001)
                    task.cancel()
                    await asyncio.sleep(.01)
                    assert not task.done() and not budget.source.closed and manager.snapshot()['leases'] == 1
                    allow_close.set()
                    with pytest.raises(asyncio.CancelledError): await task
                    assert budget.source.closed
                finally:
                    allow_open.set();allow_close.set()
                    await asyncio.gather(task,return_exceptions=True)
        assert manager.snapshot()['leases'] == manager.snapshot()['foreground']['active'] == 0
    asyncio.run(run())


def test_digest_history_projects_fixed_ordered_batches_and_restores_timeout():
    from app.notification_preparation import EmailJob, _history
    rows = [(datetime(2026,9,1,tzinfo=timezone.utc),'business',None,1e20),
            (datetime(2026,9,2,tzinfo=timezone.utc),'unclassified','not_deductible',1609.344)]
    class Result:
        def __init__(self,row): self.row=row
        async def fetchone(self): return self.row
    class Cursor:
        closed=False
        async def execute(self,query,params):
            assert 'ORDER BY started_at ASC,id ASC' in query and params[0] == 41
        async def fetchmany(self,count):
            assert count == 256
            result,self.rows = self.rows,[]
            return result
        async def close(self): self.closed=True
    cursor=Cursor(); cursor.rows=rows
    class Connection:
        def __init__(self): self.settings=[]
        def cursor(self,**kwargs):
            if 'name' not in kwargs:
                class Rates:
                    async def __aenter__(self): return self
                    async def __aexit__(self,*args): pass
                    async def stream(self,query,params,*,size):
                        assert 'WITH selected AS' in query and size == 1
                        if False: yield None
                return Rates()
            assert kwargs['name'].startswith('notification_') and not kwargs['withhold']
            return cursor
        async def execute(self,query,params=()):
            if query.startswith('SELECT extract'): return Result((9000,))
            if 'set_config' in query: self.settings.append(params[0]); return Result(None)
            raise AssertionError(query)
    conn=Connection(); session=Session()
    job=EmailJob(Operation(),session,'monthly_summary',datetime.now(timezone.utc),9,
        dict(range_start='2026-09-01T00:00:00+00:00',range_end='2026-10-01T00:00:00+00:00',year=2026))
    asyncio.run(_history(conn,job))
    assert cursor.closed and conn.settings[-1] == '9000ms'
    command=next(c for c in session.commands if c['type']=='history_rows')
    assert command['rows'] == [[t.isoformat(),c,e,d.hex()] for t,c,e,d in rows]
    assert session.commands[-1] == {'type':'history_finish'}


def test_digest_deadline_keeps_actual_fetch_cleanup_and_cursor_owned(monkeypatch):
    from app.notification_preparation import EmailJob, _history
    from app.digest_summary import DigestPreparationTimeout
    monkeypatch.setattr('app.digest_summary.PREPARATION_SECONDS',.01)
    async def run():
        stopping,release=asyncio.Event(),asyncio.Event()
        class Result:
            async def fetchone(self): return (0,)
        class Cursor:
            closed=False
            async def execute(self,*args): pass
            async def fetchmany(self,count):
                try: await asyncio.Future()
                except asyncio.CancelledError:
                    stopping.set()
                    await release.wait()
                    raise
            async def close(self): self.closed=True
        cursor=Cursor()
        class Connection:
            def cursor(self,**kwargs): return cursor
            async def execute(self,*args): return Result()
        session=Session()
        job=EmailJob(Operation(),session,'monthly_summary',datetime.now(timezone.utc),9,
            dict(range_start='2026-09-01T00:00:00+00:00',range_end='2026-10-01T00:00:00+00:00',year=2026))
        task=asyncio.create_task(_history(Connection(),job))
        await asyncio.wait_for(stopping.wait(),1)
        assert not task.done() and not cursor.closed
        release.set()
        with pytest.raises(DigestPreparationTimeout): await task
        assert cursor.closed and not session.commands
    asyncio.run(run())


def test_literal_codec_preserves_every_python_codepoint_in_bounded_frames():
    value=('😀é\ud800\udc80\udfff'+'x'*16377)*5
    projection=Projection(Operation(),Session())
    asyncio.run(projection.literal('app_url',value))
    assert projection.session.commands == [{'type':'text','key':'app_url',
        'size':len(value.encode('utf8','surrogatepass')),'encoding':'utf8','literal':True}]
    assert b''.join(projection.session.chunks).decode('utf8','surrogatepass') == value
    assert max(map(len,projection.session.chunks)) <= FRAME_BYTES
