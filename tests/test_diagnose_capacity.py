"""Serving diagnostics execute SQL through real managed account helpers."""
from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, Request
from psycopg.pq import TransactionStatus
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from app.account_context import AccountPool, AccountPrincipal
from app.auth import require_user
from app.capacity import AdmissionManager, CapacityBusy
from app.diagnose import _check_database, _check_migrations, _expected_migration_versions, build_report
from app.ui import make_router
from test_diagnose import _config

pytestmark = [pytest.mark.unit, pytest.mark.capacity_contract]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
    async def fetchone(self):
        return self.rows[0] if self.rows else None
    async def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.queries = []
    @asynccontextmanager
    async def transaction(self):
        self.info.transaction_status = TransactionStatus.INTRANS
        try:
            yield
        finally:
            self.info.transaction_status = TransactionStatus.IDLE
    async def execute(self, sql, parameters=()):
        self.queries.append(sql)
        if sql == 'SELECT set_config(%s, %s, true)':
            return _Cursor([(parameters[1],)])
        if sql == 'SELECT version FROM schema_migrations ORDER BY version':
            return _Cursor([(v,) for v in _expected_migration_versions()])
        return _Cursor([(1,)])


class _Pool:
    def __init__(self):
        self.conn = _Connection()
        self.live = self.maximum = 0
    @asynccontextmanager
    async def connection(self, **kwargs):
        self.live += 1
        self.maximum = max(self.maximum, self.live)
        try:
            await asyncio.sleep(0)
            yield self.conn
        finally:
            self.live -= 1
    def get_stats(self):
        return {'pool_size': 1, 'pool_available': 1 - self.live}


def _account_pool(**settings):
    raw = _Pool()
    manager = AdmissionManager(SimpleNamespace(**settings) if settings else None)
    account = AccountPool(manager.manage_pool(raw, 'runtime'), AccountPrincipal(7, True, 1))
    return raw, manager, account


def _assert_metadata_sql(raw, manager):
    assert raw.conn.queries.count('SELECT 1') == 1
    assert raw.conn.queries.count('SELECT version FROM schema_migrations ORDER BY version') == 1
    assert raw.maximum == 1 and raw.live == 0
    assert not any(manager._active.values())
    assert not any(manager._pending.values())


def test_serving_report_preserves_account_admission_and_sequential_metadata_borrows():
    async def run():
        raw, manager, account = _account_pool()
        report = await build_report(_config(), account)
        assert report.database.ok and report.database.error_type is None
        assert report.migrations.applied == _expected_migration_versions()
        _assert_metadata_sql(raw, manager)
    asyncio.run(run())


def test_admin_settings_diagnostics_run_actual_sql_in_managed_helper(monkeypatch):
    import app.ui.settings as settings
    async def rows(*args, **kwargs):
        return []
    for name in ('_fetch_rates_rows', 'list_vehicles', '_fetch_odometer_context',
                 '_fetch_places_rows', '_fetch_rules_rows', '_fetch_boundary_overrides_rows',
                 '_fetch_device_fixes'):
        monkeypatch.setattr(settings, name, rows)
    async def auto_assign(*args):
        return False
    async def schema_version(*args):
        return max(_expected_migration_versions())
    monkeypatch.setattr(settings, 'get_auto_assign_default_vehicle', auto_assign)
    monkeypatch.setattr(settings, '_fetch_schema_version', schema_version)
    async def render(request, template, context, **kwargs):
        report = context['diagnostics_report']
        return JSONResponse({'database_ok': report.database.ok,
                             'migrations': report.migrations.applied})
    monkeypatch.setattr(settings, 'render_page', render)
    async def run():
        raw, manager, account = _account_pool()
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='test-only-diagnostics')
        cfg = _config(display_tz=ZoneInfo('UTC'))
        app.state.config, app.state.capacity = cfg, manager
        async def user(request: Request):
            request.state.principal = account.principal
            request.state.account_pool = account
            request.state.config = cfg
            request.state.account_settings = SimpleNamespace()
            return {'id': 7, 'is_admin': True}
        app.dependency_overrides[require_user] = user
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/settings')
        assert response.status_code == 200
        assert response.json() == {'database_ok': True, 'migrations': _expected_migration_versions()}
        _assert_metadata_sql(raw, manager)
    asyncio.run(run())


@asynccontextmanager
async def _routine_saturation(manager):
    release = asyncio.Event()
    entered = [asyncio.Event()]
    async def occupy(index):
        async with manager.operation('routine', AccountPrincipal(index + 8, True, 1)):
            entered[index].set()
            await release.wait()
    tasks = [asyncio.create_task(occupy(i)) for i in range(len(entered))]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 2)
        yield
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)


@pytest.mark.parametrize('check', [_check_database, _check_migrations])
def test_metadata_capacity_refusal_propagates_instead_of_reporting_database_failure(check):
    async def run():
        raw, manager, account = _account_pool(capacity_routine_pending=0)
        async with _routine_saturation(manager):
            with pytest.raises(CapacityBusy):
                await check(account)
        assert raw.conn.queries == []
        assert not any(manager._active.values())
    asyncio.run(run())


def test_settings_metadata_refusal_after_data_read_uses_real_app_busy_response(monkeypatch):
    from app.config import Config
    from app.main import create_app
    import app.ui.settings as settings
    async def rows(*args, **kwargs):
        return []
    for name in ('_fetch_rates_rows', 'list_vehicles', '_fetch_odometer_context',
                 '_fetch_places_rows', '_fetch_rules_rows', '_fetch_boundary_overrides_rows',
                 '_fetch_device_fixes'):
        monkeypatch.setattr(settings, name, rows)
    async def auto_assign(*args):
        return False
    async def schema_version(conn):
        await conn.execute("SELECT 'settings-data'")
        return max(_expected_migration_versions())
    monkeypatch.setattr(settings, 'get_auto_assign_default_vehicle', auto_assign)
    monkeypatch.setattr(settings, '_fetch_schema_version', schema_version)
    original_report = settings.build_report
    async def saturated_report(cfg, pool, state):
        async with _routine_saturation(state.capacity):
            return await original_report(cfg, pool, state)
    monkeypatch.setattr(settings, 'build_report', saturated_report)
    async def render(request, template, context, **kwargs):
        pytest.fail('capacity refusal must not render false database failure data')
    monkeypatch.setattr(settings, 'render_page', render)
    for name, value in {'DATABASE_URL': 'postgresql://localhost/test-only-diagnostics',
                        'SESSION_SECRET': 'test-only-diagnostics', 'DEV_NO_AUTH': '1',
                        'CAPACITY_ROUTINE_PENDING': '0'}.items():
        monkeypatch.setenv(name, value)
    async def run():
        # ASGITransport does not start lifespan, so this uses only the explicit
        # managed synthetic pool while exercising the real app error handler.
        app = create_app(Config.from_env())
        manager = app.state.capacity
        raw = _Pool()
        account = AccountPool(manager.manage_pool(raw, 'runtime'), AccountPrincipal(7, True, 1))
        async def user(request: Request):
            request.state.principal = account.principal
            request.state.account_pool = account
            request.state.config = app.state.config
            request.state.account_settings = SimpleNamespace()
            return {'id': 7, 'is_admin': True}
        app.dependency_overrides[require_user] = user
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/settings', headers={'accept': 'application/json'})
        assert response.status_code == 503
        assert response.headers['retry-after'] == '1'
        assert response.json()['error'] == 'capacity_busy'
        assert "SELECT 'settings-data'" in raw.conn.queries
        assert 'SELECT 1' not in raw.conn.queries
        assert 'SELECT version FROM schema_migrations ORDER BY version' not in raw.conn.queries
        assert not any(manager._active.values())
    asyncio.run(run())
