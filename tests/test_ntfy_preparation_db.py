"""Prepared NTFY turns retain real restricted roles, locks and window ledgers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import ntfy_preparation as preparation
from app.account_context import account_id
from app.notifications import odometer_reminder_vehicles
from app.nudge import nudge_message, latest_window_end
from app.odometer import latest_quarter_start
from app.odometer_reminder import reminder_message
from conftest import seed_tracking_device
from test_capacity_db import _scenario
from test_nudge_db import _insert_trip
from test_prepared_ntfy_ops import Receiver

pytestmark = [pytest.mark.db, pytest.mark.capacity_contract,
              pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='requires disposable PostGIS')]
TZ = ZoneInfo('America/New_York')
NOW = datetime(2026, 11, 2, 19, tzinfo=TZ)


def clock(monkeypatch):
    capture = preparation.capture_ntfy_job
    async def fixed(conn, operation, config, kind, **kwargs):
        assert (await (await conn.execute('SELECT current_user')).fetchone())[0] == 'odograph_runtime'
        return await capture(conn, operation, config, kind, now=NOW)
    monkeypatch.setattr(preparation, 'capture_ntfy_job', fixed)


async def seed(pool, kind, zero):
    async with pool.connection() as conn:
        await conn.execute("UPDATE account_settings SET display_tz=%s,ntfy_topic='topic',nudge_weekly_hour=18,"
                           'odometer_reminder_requested=true,odometer_reminder_hour=9 WHERE account_id=%s',
                           (str(TZ), account_id(conn)))
        if kind == 'weekly':
            await seed_tracking_device(conn, 'phone', device_id=1)
            end = latest_window_end(NOW, 18)
            if not zero:
                await _insert_trip(conn, end - timedelta(days=1))
            return nudge_message(1, end, 'https://example.test///') if not zero else None
        await conn.execute('UPDATE vehicles SET active=false WHERE account_id=%s', (account_id(conn),))
        for name, logged in [('z last', False), ('é very long ' + '界' * 40000, zero), ('already logged ' + '界' * 40000, True)]:
            cur = await conn.execute('INSERT INTO vehicles (account_id,name,active) VALUES (%s,%s,true) RETURNING id',
                                     (account_id(conn), name))
            identifier = (await cur.fetchone())[0]
            if zero or logged:
                await conn.execute('INSERT INTO odometer_readings (account_id,vehicle_id,recorded_at,odometer_m) '
                    'VALUES (%s,%s,%s,1000)', (account_id(conn), identifier, NOW))
        names = await odometer_reminder_vehicles(conn, latest_quarter_start(NOW, 9))
        return reminder_message(names, 'https://example.test///') if names else None


async def invoke(manager, pool, worker, kind, held):
    function = preparation.run_prepared_nudge_turn if kind == 'weekly' else preparation.run_prepared_odometer_turn
    async with manager.operation('background', pool.principal):
        return await function(worker, pool.principal, None, http_client=held)


@pytest.mark.parametrize('kind', ['weekly', 'quarterly'])
@pytest.mark.parametrize('zero', [False, True])
def test_real_helper_transport_complete_order_ledger_once_zero_skip_and_isolation(monkeypatch, tmp_path, kind, zero):
    clock(monkeypatch)
    async def scenario():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            pool, other = accounts
            expected = await seed(pool, kind, zero)
            receiver = Receiver()
            port = await receiver.start()
            root = tmp_path / 'spool'
            worker = SimpleNamespace(config=SimpleNamespace(preparation_spool_dir=root,
                app_url='https://example.test///', ntfy_url=f'http://127.0.0.1:{port}',
                ntfy_token='', ntfy_username='', ntfy_password=''),
                pools=SimpleNamespace(runtime=runtime, control=control))
            async with httpx.AsyncClient() as held:
                try:
                    await invoke(manager, pool, worker, kind, held)
                    await invoke(manager, pool, worker, kind, held)
                    assert [body.decode('utf8') for _, body in receiver.requests] == ([] if zero else [expected])
                    table = 'nudge_delivery_windows' if kind == 'weekly' else 'odometer_reminder_windows'
                    field = 'trip_count' if kind == 'weekly' else 'reminded'
                    async with pool.connection() as conn:
                        assert (await (await conn.execute(f'SELECT {field} FROM {table}')).fetchall()) == [(0 if zero else 1,)]
                    async with other.connection() as conn:
                        assert (await (await conn.execute(f'SELECT count(*) FROM {table}')).fetchone()) == (0,)
                    assert not list(root.glob('op-*'))
                    assert manager.snapshot()['background']['active'] == 0
                    assert manager.snapshot()['leases'] == 0
                    assert manager.snapshot()['routine']['active'] == 0
                finally:
                    await receiver.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['weekly', 'quarterly'])
def test_http_failure_keeps_cookies_but_rolls_back_ledger_and_releases_resources(monkeypatch, tmp_path, kind):
    clock(monkeypatch)
    async def scenario():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            pool, _ = accounts
            await seed(pool, kind, False)
            receiver = Receiver(b'HTTP/1.1 500 Failed\r\nSet-Cookie: accepted=before-error; Path=/\r\n'
                                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            port = await receiver.start()
            root = tmp_path / 'spool'
            worker = SimpleNamespace(config=SimpleNamespace(preparation_spool_dir=root,
                app_url='', ntfy_url=f'http://127.0.0.1:{port}', ntfy_token='', ntfy_username='', ntfy_password=''),
                pools=SimpleNamespace(runtime=runtime, control=control))
            async with httpx.AsyncClient() as held:
                try:
                    with pytest.raises(httpx.HTTPStatusError):
                        await invoke(manager, pool, worker, kind, held)
                    assert held.cookies.get('accepted') == 'before-error'
                    table = 'nudge_delivery_windows' if kind == 'weekly' else 'odometer_reminder_windows'
                    async with pool.connection() as conn:
                        assert (await (await conn.execute(f'SELECT count(*) FROM {table}')).fetchone()) == (0,)
                    assert not list(root.glob('op-*'))
                    assert manager.snapshot()['leases'] == 0
                    assert manager.snapshot()['background']['active'] == 0
                finally:
                    await receiver.close()
    asyncio.run(scenario())


def test_stale_exact_topic_recheck_skips_transport_and_ledger(monkeypatch, tmp_path):
    clock(monkeypatch)
    async def scenario():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            pool, _ = accounts
            await seed(pool, 'weekly', False)
            delivery = preparation._delivery
            async def change(conn, job, client):
                async with raw.connection() as write:
                    await write.execute("UPDATE account_settings SET ntfy_topic='topic-changed' WHERE account_id=%s",
                                        (pool.principal.account_id,))
                return await delivery(conn, job, client)
            monkeypatch.setattr(preparation, '_delivery', change)
            receiver = Receiver()
            port = await receiver.start()
            root = tmp_path / 'spool'
            worker = SimpleNamespace(config=SimpleNamespace(preparation_spool_dir=root,
                app_url='', ntfy_url=f'http://127.0.0.1:{port}', ntfy_token='', ntfy_username='', ntfy_password=''),
                pools=SimpleNamespace(runtime=runtime, control=control))
            async with httpx.AsyncClient() as held:
                try:
                    await invoke(manager, pool, worker, 'weekly', held)
                    assert not receiver.requests
                    async with pool.connection() as conn:
                        assert (await (await conn.execute('SELECT count(*) FROM nudge_delivery_windows')).fetchone()) == (0,)
                    assert not list(root.glob('op-*'))
                finally:
                    await receiver.close()
    asyncio.run(scenario())
