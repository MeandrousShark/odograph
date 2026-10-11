"""Generic exports through real sessions, middleware and resource ownership."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import psycopg
from psycopg.pq import TransactionStatus
from openpyxl.utils.exceptions import IllegalCharacterError
import pytest

from app.account_context import AccountPool, AccountPrincipal, account_id
from app.capacity import current_owner
from app.db import make_pool
from app.export import _trip_distance_and_deduction, to_csv, to_xlsx
from app.export_preparation import ExportProjection
from app.main import create_app
from app.preparation import PreparationOperation
from app.preparation_resources import ResourceBudget
from app.rates import load_rates
from app.ui._common import TRIP_COLUMNS, _trip_filter_sql
from auth_db_fixtures import auth_config
from conftest import add_test_account, reset_db
from test_capacity_routes import _scope
from test_report_projection_db import TZ
from test_streamed_exports import signature

TEST_DB = os.environ.get('TEST_DATABASE_URL')
pytestmark = [pytest.mark.db, pytest.mark.capacity_contract,
              pytest.mark.skipif(not TEST_DB, reason='requires disposable PostGIS')]


@asynccontextmanager
async def application(tmp_path):
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        await reset_db(raw)
        config = auth_config(TEST_DB, dev_no_auth=False, initial_admin_signup=True,
            preparation_spool_dir=str(tmp_path / 'spool'), raw_message_retention_days=0)
        app = create_app(config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url='https://test') as client:
                page = await client.get('/signup')
                assert page.status_code == 200
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                response = await client.post('/signup', data={
                    'email': 'export-owner@example.invalid', 'password': 'export test password',
                    'password_confirm': 'export test password', 'csrf_token': csrf,
                    'display_timezone': str(TZ)})
                assert response.status_code == 303
                async with raw.connection() as conn:
                    owner = (await (await conn.execute(
                        'SELECT id FROM accounts WHERE email=%s',
                        ('export-owner@example.invalid',))).fetchone())[0]
                account = AccountPool(app.state.runtime_pool, AccountPrincipal(owner, True, 1))
                async with account.connection() as conn:
                    vehicle = (await (await conn.execute(
                        'SELECT id FROM vehicles WHERE account_id=%s AND is_default',
                        (owner,))).fetchone())[0]
                    await conn.execute('UPDATE vehicles SET name=%s WHERE account_id=%s',
                                       ('Route car 車', owner))
                    for index in range(4):
                        start = datetime(2026, 4, 15 + index, 9, tzinfo=TZ)
                        notes = '車,%"\r\n' * 6000 + ('literal%_\\' if index % 2 else 'other')
                        await conn.execute(
                            'INSERT INTO trips(account_id,device,source,started_at,ended_at,'
                            'distance_m,category,vehicle_id,start_label,end_label,purpose,notes) '
                            "VALUES(%s,'route','manual',%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (owner, start, start + timedelta(minutes=30), 1609.344 + index,
                             'business' if index % 2 else 'personal', vehicle,
                             'Start 車', 'End', 'Client visit', notes))
                yield SimpleNamespace(raw=raw, app=app, client=client, account=account,
                    owner=owner, cookie=client.cookies.get('session'), config=config)
        assert not app.state.capacity.snapshot()['leases']
        assert all(not lane['active'] and not lane['pending']
                   for lane in app.state.capacity.snapshot().values() if isinstance(lane, dict))
    finally:
        await raw.close()


async def oracle(state, format, q=''):
    async with state.account.connection() as conn:
        where, params = _trip_filter_sql('', None, None, q=q, owner_id=account_id(conn))
        cursor = await conn.execute(f'SELECT {TRIP_COLUMNS} FROM trips {where} ORDER BY started_at DESC', params)
        columns = [column.name for column in cursor.description]
        trips = [dict(zip(columns, row)) for row in await cursor.fetchall()]
        rates = await load_rates(conn)
    output = to_csv(trips, rates, TZ) if format == 'csv' else to_xlsx(trips, rates, TZ)
    return trips, rates, output


def observe(monkeypatch):
    observations = SimpleNamespace(operations=[], owners=[], snapshots=[])
    original_start = PreparationOperation.start_helper
    original_connection = AccountPool.connection

    async def start(operation, *args, **kwargs):
        observations.operations.append(operation)
        observations.owners.append(current_owner())
        return await original_start(operation, *args, **kwargs)

    @asynccontextmanager
    async def connection(pool, **kwargs):
        snapshot = None
        try:
            async with original_connection(pool, **kwargs) as conn:
                if kwargs.get('snapshot_id') is not None:
                    snapshot = {'connection': conn, 'backend': conn.info.backend_pid, 'active': True}
                    observations.snapshots.append(snapshot)
                    assert (await (await conn.execute('SELECT session_user')).fetchone())[0] == 'odograph_runtime'
                yield conn
        finally:
            if snapshot is not None:
                snapshot['active'] = False

    monkeypatch.setattr(PreparationOperation, 'start_helper', start)
    monkeypatch.setattr(AccountPool, 'connection', connection)
    return observations


class Exchange:
    def __init__(self, state, **params):
        self.scope = _scope('/export', 'GET', [(b'cookie', ('session=' + state.cookie).encode())])
        self.scope.update(scheme='https', server=('test', 443), query_string=urlencode(params).encode())
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({'type': 'http.request', 'body': b'', 'more_body': False})
        self.messages = []

    async def receive(self):
        return await self.incoming.get()

    async def send(self, message):
        self.messages.append(message)

    def disconnect(self):
        self.incoming.put_nowait({'type': 'http.disconnect'})

    @property
    def body(self):
        return b''.join(message.get('body', b'') for message in self.messages)


async def assert_lease(state, held):
    async with state.raw.connection() as conn:
        acquired = (await (await conn.execute('SELECT pg_try_advisory_lock(%s)', (-state.owner,))).fetchone())[0]
        if acquired:
            assert (await (await conn.execute('SELECT pg_advisory_unlock(%s)', (-state.owner,))).fetchone())[0]
        assert acquired is not held


def assert_clean(state, observations):
    snapshot = state.app.state.capacity.snapshot()
    assert snapshot['foreground'] == {'active': 0, 'pending': 0}
    assert snapshot['leases'] == 0
    assert not list(Path(state.config.preparation_spool_dir).glob('op-*'))
    for operation, owner in zip(observations.operations, observations.owners):
        assert operation.closed and not operation.directory.exists()
        assert operation.process.returncode is not None
        assert owner._lifetime.released and not owner._lifetime.threads


@pytest.mark.parametrize('format', ['csv', 'xlsx'])
def test_export_route_preserves_full_output_one_snapshot_and_middleware(tmp_path, monkeypatch, format):
    observations = observe(monkeypatch)
    async def run():
        async with application(tmp_path) as state:
            other = await add_test_account(state.raw, 99)
            async with other.connection() as conn:
                start = datetime(2026, 4, 1, tzinfo=TZ)
                await conn.execute("INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,category,notes) "
                    "VALUES(%s,'foreign','manual',%s,%s,999,'business','FOREIGN EXPORT MARKER literal%%_\\')",
                    (account_id(conn), start, start + timedelta(minutes=5)))
            trips, rates, expected = await oracle(state, format, 'literal%_\\')
            assert len(trips) == 2 and all(trip['category'] == 'business' for trip in trips)
            assert sum(_trip_distance_and_deduction(trip, rates, TZ)[1] for trip in trips) > 0
            original_rates = ExportProjection.rates
            mutated = False
            async def rates(projection, conn, owner):
                nonlocal mutated
                async with state.raw.connection() as concurrent:
                    await concurrent.execute("UPDATE trips SET purpose='Later purpose',notes='Later notes' WHERE account_id=%s", (owner,))
                    await concurrent.execute("UPDATE account_settings SET display_tz='UTC' WHERE account_id=%s", (owner,))
                    await concurrent.execute('UPDATE mileage_rates SET rate_per_mi=9 WHERE account_id=%s', (owner,))
                mutated = True
                await original_rates(projection, conn, owner)
            monkeypatch.setattr(ExportProjection, 'rates', rates)
            exchange = Exchange(state, format=format, q='literal%_\\')
            async def send(message):
                if message['type'] == 'http.response.start':
                    operation = observations.operations[0]
                    assert operation.finished and not operation.closed and operation.process.returncode == 0
                    assert observations.snapshots and all(not item['active'] for item in observations.snapshots)
                    assert all(item['connection'].info.transaction_status == TransactionStatus.IDLE
                               for item in observations.snapshots)
                    async with state.raw.connection() as probe:
                        for item in observations.snapshots:
                            assert (await (await probe.execute(
                                'SELECT xact_start FROM pg_stat_activity WHERE pid=%s',
                                (item['backend'],))).fetchone()) == (None,)
                    control = exchange.scope['state']['_report_control_connection']
                    assert control.info.user == 'odograph_control'
                    assert control.info.transaction_status == TransactionStatus.IDLE
                    assert current_owner().lane == 'foreground'
                    assert state.app.state.capacity.snapshot()['leases'] == 1
                await exchange.send(message)
            await state.app(exchange.scope, exchange.receive, send)
            assert mutated and exchange.messages[0]['status'] == 200
            actual = exchange.body
            assert actual == expected if format == 'csv' else signature(actual) == signature(expected)
            headers = dict(exchange.messages[0]['headers'])
            media = b'text/csv; charset=utf-8' if format == 'csv' else b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            assert headers[b'content-type'] == media
            assert headers[b'content-length'] == str(len(actual)).encode()
            assert headers[b'content-disposition'] == f'attachment; filename="trips.{format}"'.encode()
            assert headers[b'cache-control'] == b'no-store, private'
            assert headers[b'x-odograph-account'] == str(state.owner).encode()
            assert_clean(state, observations)
            await assert_lease(state, False)
    asyncio.run(run())


@pytest.mark.parametrize('case', ['anonymous', 'disabled', 'version', 'format'])
def test_export_route_auth_and_format_precede_variable_settings(tmp_path, monkeypatch, case):
    observations = observe(monkeypatch)
    async def run():
        async with application(tmp_path) as state:
            async with state.raw.connection() as conn:
                await conn.execute("UPDATE account_settings SET display_tz='Missing/RouteTimezone' WHERE account_id=%s", (state.owner,))
                if case in ('disabled', 'version'):
                    field = 'is_enabled=false' if case == 'disabled' else 'auth_version=auth_version+1'
                    await conn.execute(f'UPDATE accounts SET {field} WHERE id=%s', (state.owner,))
            if case == 'anonymous':
                state.client.cookies.clear()
            response = await state.client.get('/export', params={
                'format': 'invalid', 'from': 'bad-date', 'vehicle': 'bad-id', 'q': '\x00'})
            if case == 'format':
                assert response.status_code == 400
                assert response.json() == {'detail': 'format must be csv or xlsx'}
                assert response.headers['x-odograph-account'] == str(state.owner)
            else:
                assert response.status_code == 303 and response.headers['location'] == '/login'
            assert not observations.operations and not observations.snapshots
            assert_clean(state, observations)
            await assert_lease(state, False)
    asyncio.run(run())


@pytest.mark.parametrize('format', ['csv', 'xlsx'])
@pytest.mark.parametrize('send_failure', [False, True])
def test_export_route_holds_real_lease_until_blocked_send_finishes(tmp_path, monkeypatch, format, send_failure):
    observations = observe(monkeypatch)
    async def run():
        async with application(tmp_path) as state:
            entered, release = asyncio.Event(), asyncio.Event()
            exchange = Exchange(state, format=format)
            async def send(message):
                if message['type'] == 'http.response.body' and message.get('body'):
                    entered.set()
                    await release.wait()
                    if send_failure:
                        raise RuntimeError('fixture send failure')
                await exchange.send(message)
            task = asyncio.create_task(state.app(exchange.scope, exchange.receive, send))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                operation = observations.operations[0]
                assert not task.done() and operation.finished and not operation.closed
                assert operation.process.returncode == 0 and operation.directory.exists()
                assert state.app.state.capacity.snapshot()['foreground']['active'] == 1
                assert state.app.state.capacity.snapshot()['leases'] == 1
                await assert_lease(state, True)
                release.set()
                if send_failure:
                    with pytest.raises(RuntimeError, match='fixture send failure'):
                        await task
                else:
                    await task
                    assert exchange.messages[-1]['more_body'] is False
                assert_clean(state, observations)
                await assert_lease(state, False)
            finally:
                release.set()
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_export_route_disconnect_and_repeated_cancel_drain_real_query_and_rollback(tmp_path, monkeypatch):
    observations = observe(monkeypatch)
    original_fetch = psycopg.AsyncServerCursor.fetchmany
    original_cancel = psycopg.AsyncConnection._try_cancel
    original_exit = psycopg.AsyncTransaction.__aexit__
    async def run():
        async with application(tmp_path) as state:
            entered, cancelling, rollback, release_cancel, release_rollback = (asyncio.Event() for _ in range(5))
            target = target_connection = None
            async def fetch(cursor, size=0):
                nonlocal target, target_connection
                if cursor.name.startswith('export_') and target is None:
                    target_connection = cursor.connection
                    target = cursor.connection.info.backend_pid
                    entered.set()
                    await cursor.connection.execute('SELECT pg_sleep(10)')
                return await original_fetch(cursor, size)
            async def cancel(conn, **kwargs):
                if conn.info.backend_pid == target:
                    cancelling.set()
                    await release_cancel.wait()
                return await original_cancel(conn, **kwargs)
            async def exit_transaction(transaction, exc_type, exc_value, traceback):
                if transaction.pgconn.backend_pid == target and exc_type is not None:
                    assert transaction.connection is target_connection
                    rollback.set()
                    await release_rollback.wait()
                return await original_exit(transaction, exc_type, exc_value, traceback)
            monkeypatch.setattr(psycopg.AsyncServerCursor, 'fetchmany', fetch)
            monkeypatch.setattr(psycopg.AsyncConnection, '_try_cancel', cancel)
            monkeypatch.setattr(psycopg.AsyncTransaction, '__aexit__', exit_transaction)
            exchange = Exchange(state)
            task = asyncio.create_task(state.app(exchange.scope, exchange.receive, exchange.send))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                async with state.raw.connection() as conn:
                    # Wait for actual SQL submission rather than only the wrapper event.
                    async with asyncio.timeout(5):
                        while True:
                            await conn.execute('SELECT pg_stat_clear_snapshot()')
                            row = await (await conn.execute('SELECT state,query FROM pg_stat_activity WHERE pid=%s', (target,))).fetchone()
                            if row and row[0] == 'active' and 'pg_sleep' in row[1]: break
                            await asyncio.sleep(.005)
                exchange.disconnect()
                await asyncio.wait_for(cancelling.wait(), 5)
                task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
                assert not task.done() and not exchange.messages
                assert state.app.state.capacity.snapshot()['foreground']['active'] == 1
                assert state.app.state.capacity.snapshot()['leases'] == 1
                await assert_lease(state, True)
                release_cancel.set()
                await asyncio.wait_for(rollback.wait(), 5)
                task.cancel(); await asyncio.sleep(.01)
                assert not task.done() and not observations.operations[0].closed
                assert observations.operations[0].directory.is_dir()
                # SQL abort clears xact_start before the failed block's ROLLBACK.
                assert target_connection.info.transaction_status == TransactionStatus.INERROR
                assert state.app.state.capacity.snapshot()['foreground']['active'] == 1
                assert state.app.state.capacity.snapshot()['leases'] == 1
                async with state.raw.connection() as conn:
                    row = await (await conn.execute('SELECT state FROM pg_stat_activity WHERE pid=%s', (target,))).fetchone()
                    assert row == ('idle in transaction (aborted)',)
                await assert_lease(state, True)
                release_rollback.set()
                with pytest.raises(asyncio.CancelledError): await task
                assert not exchange.messages
                assert_clean(state, observations)
                assert target_connection.closed or target_connection.info.transaction_status == TransactionStatus.IDLE
                async with state.raw.connection() as conn:
                    row = await (await conn.execute('SELECT state,xact_start FROM pg_stat_activity WHERE pid=%s', (target,))).fetchone()
                    assert row is None or row == ('idle', None)
                await assert_lease(state, False)
            finally:
                release_cancel.set(); release_rollback.set()
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_export_route_disconnect_and_repeated_cancel_wait_for_actual_file_close(tmp_path, monkeypatch):
    observations = observe(monkeypatch)
    original_open = ResourceBudget.open
    closing, release = threading.Event(), threading.Event()
    streams = []
    class HeldClose:
        def __init__(self, stream): self.stream = stream
        def read(self, size): return self.stream.read(size)
        def close(self):
            closing.set()
            assert release.wait(5), 'test did not release actual file close'
            self.stream.close()
    def open_file(budget, name, mode='w+b'):
        stream = original_open(budget, name, mode)
        if name == 'trips.csv' and mode == 'rb':
            streams.append(stream)
            return HeldClose(stream)
        return stream
    monkeypatch.setattr(ResourceBudget, 'open', open_file)
    async def run():
        async with application(tmp_path) as state:
            sending = asyncio.Event()
            exchange = Exchange(state)
            async def send(message):
                if message['type'] == 'http.response.body' and message.get('body'):
                    sending.set()
                    await asyncio.Future()
                await exchange.send(message)
            task = asyncio.create_task(state.app(exchange.scope, exchange.receive, send))
            try:
                await asyncio.wait_for(sending.wait(), 5)
                exchange.disconnect()
                async with asyncio.timeout(5):
                    while not closing.is_set(): await asyncio.sleep(.001)
                task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
                operation = observations.operations[0]
                assert not task.done() and not operation.closed and operation.directory.exists()
                assert streams and not streams[0].closed
                assert state.app.state.capacity.snapshot()['foreground']['active'] == 1
                assert state.app.state.capacity.snapshot()['leases'] == 1
                await assert_lease(state, True)
                release.set()
                with pytest.raises(asyncio.CancelledError): await task
                assert streams[0].closed
                assert_clean(state, observations)
                await assert_lease(state, False)
            finally:
                release.set()
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('prefix_length', [32766, 32767], ids=['illegal-in-prefix', 'illegal-after-prefix'])
def test_export_route_preserves_illegal_cell_failure_and_old_prefix(tmp_path, monkeypatch, prefix_length):
    from app import capacity_routes

    observations = observe(monkeypatch)
    controls = []
    original_work = capacity_routes.report_account_work
    @asynccontextmanager
    async def work(*args, **kwargs):
        async with original_work(*args, **kwargs) as connection:
            controls.append((connection, connection.info.backend_pid))
            yield connection
    monkeypatch.setattr(capacity_routes, 'report_account_work', work)

    async def run():
        async with application(tmp_path) as state:
            notes = '車' * prefix_length + '\x01private cell suffix'
            async with state.account.connection() as conn:
                await conn.execute('UPDATE trips SET notes=%s WHERE account_id=%s', (notes, state.owner))
            exchange = Exchange(state, format='xlsx')
            if prefix_length == 32766:
                with pytest.raises(IllegalCharacterError):
                    await oracle(state, 'xlsx')
                with pytest.raises(IllegalCharacterError) as failure:
                    await state.app(exchange.scope, exchange.receive, exchange.send)
                assert str(failure.value) == 'preparation cell is invalid'
                assert 'private cell suffix' not in str(failure.value)
                assert exchange.messages[0]['status'] == 500
                assert all(message.get('status') != 200 for message in exchange.messages)
                assert b'private cell suffix' not in exchange.body
                assert b'PK\x03\x04' not in exchange.body
                assert len(observations.operations) == 1
                assert not observations.operations[0].finished
            else:
                trips, _, expected = await oracle(state, 'xlsx')
                assert len(trips) == 4 and all(trip['notes'] == notes for trip in trips)
                await state.app(exchange.scope, exchange.receive, exchange.send)
                assert exchange.messages[0]['status'] == 200
                assert signature(exchange.body) == signature(expected)
                assert exchange.messages[-1]['more_body'] is False
                assert dict(exchange.messages[0]['headers'])[b'content-length'] == str(len(exchange.body)).encode()
            assert observations.snapshots and all(not item['active'] for item in observations.snapshots)
            assert all(item['connection'].closed or item['connection'].info.transaction_status == TransactionStatus.IDLE
                       for item in observations.snapshots)
            assert '_report_control_connection' not in exchange.scope['state']
            assert len(controls) == 1
            control, backend = controls[0]
            assert control.closed or control.info.transaction_status == TransactionStatus.IDLE
            async with state.raw.connection() as conn:
                row = await (await conn.execute('SELECT state,xact_start FROM pg_stat_activity WHERE pid=%s',
                                                (backend,))).fetchone()
                assert row is None or row == ('idle', None)
            assert_clean(state, observations)
            await assert_lease(state, False)
            async with state.account.connection() as conn:
                assert (await (await conn.execute('SELECT count(*) FROM trips WHERE account_id=%s',
                                                 (state.owner,))).fetchone())[0] == 4
    asyncio.run(run())
