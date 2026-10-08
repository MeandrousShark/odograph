"""NULL purpose projection skips fragment queries without changing text decode."""
import asyncio
from types import SimpleNamespace

import pytest

from app.preparation import FRAME_BYTES
from app.preparation_resources import PreparationResourceError
from app.report_preparation import FETCH_ROWS, Projection
from test_report_stored_codec import renderer

pytestmark = pytest.mark.unit


class Operation:
    def __init__(self, fail_after=None):
        self.checks = 0
        self.fail_after = fail_after

    def remaining_ms(self):
        return 17000

    def check(self):
        self.checks += 1
        if self.checks == self.fail_after:
            raise PreparationResourceError('deadline expired')


class Session:
    def __init__(self):
        self.frames = []

    async def send_command(self, command):
        self.frames.append(('command', command))

    async def send_text(self, text):
        assert len(text) <= FRAME_BYTES
        self.frames.append(('text', text))

    def replay(self, renderer):
        frames = iter(self.frames)

        class Channel:
            def recv_text(self):
                kind, value = next(frames)
                assert kind == 'text'
                return value

        for kind, command in frames:
            assert kind == 'command' and command['type'] == 'text'
            renderer.text(command['key'], command['size'], Channel(), command['encoding'])


class Connection:
    def __init__(self, batches, sources=None, codec='utf8', client='UTF8'):
        self.batches = iter(batches)
        self.sources = sources or {}
        self.info = SimpleNamespace(encoding=codec, parameter_status=lambda name: client)
        self.fragment_ids = []
        self.timeout_calls = self.fetches = 0
        self.portal_closed = False
        self.query = self.params = None

    async def execute(self, query, params):
        assert 'statement_timeout' in query and params == ('15000',)
        self.timeout_calls += 1

    def cursor(self, **kwargs):
        connection = self

        class Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                if 'name' in kwargs:
                    connection.portal_closed = True

            async def execute(self, query, params):
                connection.query, connection.params = query, params

            async def fetchmany(self, size):
                assert size == FETCH_ROWS == 256
                connection.fetches += 1
                return next(connection.batches, [])

            async def stream(self, query, params, size):
                assert size == 1 and 'convert_to(purpose,%s)' in query
                assert params[:-1] == (FRAME_BYTES, FRAME_BYTES,
                    connection.info.parameter_status('client_encoding'), FRAME_BYTES, FRAME_BYTES, 7)
                ids = params[-1]
                assert len(ids) <= FETCH_ROWS
                connection.fragment_ids.append(ids)
                for id in ids:
                    raw = connection.sources[id]
                    for offset in range(0, len(raw), FRAME_BYTES):
                        yield {'id': id, 'number': 0, 'part': offset // FRAME_BYTES + 1,
                               'value': raw[offset:offset + FRAME_BYTES]}

        return Cursor()


QUERY = 'SELECT id,octet_length(convert_to(purpose,%s)) AS purpose_size FROM trips'


def test_all_null_batches_skip_fragment_queries_and_preserve_portal_bound():
    async def run():
        batches = [[{'id': i, 'purpose_size': None} for i in range(FETCH_ROWS)],
                   [{'id': FETCH_ROWS, 'purpose_size': None}]]
        conn = Connection(batches)
        operation, session = Operation(), Session()
        projection = Projection(operation, session)
        result = [row async for row in projection.rows_with_purpose(conn, QUERY, ('UTF8',), 7)]
        assert result == [{'id': i, 'purpose': None} for i in range(FETCH_ROWS + 1)]
        assert conn.fragment_ids == [] and session.frames == []
        assert conn.query == QUERY and conn.params == ('UTF8',)
        assert conn.fetches == 3 and conn.timeout_calls == 4 and conn.portal_closed
        assert operation.checks == FETCH_ROWS + 1
    asyncio.run(run())


def test_mixed_null_empty_nonempty_and_nontrip_rows_keep_order_and_full_strip(renderer):
    async def run():
        value = '\u3000\t' * 20000 + '¥'
        raw = value.encode('shift_jis')
        batch = [{'id': 1, 'event': 0, 'purpose_size': None},
                 {'id': 2, 'event': 1, 'purpose_size': None},
                 {'id': 3, 'event': 0, 'purpose_size': 0},
                 {'id': 4, 'event': 0, 'purpose_size': len(raw)},
                 {'id': 5, 'event': 2, 'purpose_size': None},
                 {'id': 6, 'event': 0, 'purpose_size': None}]
        conn = Connection([batch], {3: b'', 4: raw}, 'shift_jis', 'SJIS')
        session = Session()
        projection = Projection(Operation(), session)
        result = [row async for row in projection.rows_with_purpose(conn, QUERY, ('SJIS',), 7)]
        assert [row['id'] for row in result] == list(range(1, 7))
        assert conn.fragment_ids == [[3, 4]]
        assert result[0]['purpose'] is None and result[-1]['purpose'] is None
        assert result[1] == {'id': 2, 'event': 1, 'purpose_size': None}
        assert result[4] == {'id': 5, 'event': 2, 'purpose_size': None}
        assert result[2]['purpose']['text'] == [0, 0, 'shift_jis']
        assert result[3]['purpose']['text'] == [0, len(raw), 'shift_jis']
        session.replay(renderer)
        assert ''.join(renderer.chunks(result[3]['purpose']['text'])) == raw.decode('shift_jis')
        assert renderer.trip(result[0])['purpose_nonblank'] is False
        assert renderer.trip(result[2])['purpose_nonblank'] is False
        assert renderer.trip(result[3])['purpose_nonblank'] == bool(raw.decode('shift_jis').strip())
    asyncio.run(run())


def test_nontrip_only_batch_is_unchanged_without_fragment_query():
    async def run():
        batch = [{'id': 1, 'event': 1, 'purpose_size': None},
                 {'id': 2, 'event': 2, 'purpose_size': None}]
        expected = [dict(row) for row in batch]
        conn, session = Connection([batch]), Session()
        rows = [row async for row in Projection(Operation(), session).rows_with_purpose(conn, QUERY, ('UTF8',), 7)]
        assert rows == expected and conn.fragment_ids == [] and session.frames == []
    asyncio.run(run())


def test_null_fast_path_still_checks_stop_and_closes_without_prefetch():
    async def run():
        conn = Connection([[{'id': i, 'purpose_size': None} for i in range(FETCH_ROWS)]])
        projection = Projection(Operation(fail_after=2), Session())
        rows = projection.rows_with_purpose(conn, QUERY, ('UTF8',), 7)
        assert (await anext(rows))['purpose'] is None
        assert conn.fetches == 1 and not conn.portal_closed
        with pytest.raises(PreparationResourceError):
            await anext(rows)
        assert conn.portal_closed and conn.fetches == 1 and conn.fragment_ids == []
    asyncio.run(run())


def test_nonnull_invalid_suffix_is_transferred_completely_before_prefix(renderer):
    async def run():
        raw = b'a' * 32768 + b'\xff'
        batch = [{'id': 1, 'purpose_size': None}, {'id': 2, 'purpose_size': len(raw)}]
        conn, session = Connection([batch], {2: raw}), Session()
        projection = Projection(Operation(), session)
        rows = [row async for row in projection.rows_with_purpose(conn, QUERY, ('UTF8',), 7)]
        assert conn.fragment_ids == [[2]] and rows[0]['purpose'] is None
        assert b''.join(value for kind, value in session.frames if kind == 'text') == raw
        with pytest.raises(UnicodeDecodeError):
            session.replay(renderer)
        assert 'field:purpose' not in renderer.refs
    asyncio.run(run())
