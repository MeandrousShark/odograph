"""Report admission retains actual resources through ASGI cleanup."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, Depends, FastAPI, Request
from psycopg.pq import TransactionStatus
import pytest

from app.account_context import AccountPrincipal
from app.auth import require_report_user
from app.capacity import AdmissionManager, current_owner
from app.capacity_routes import AdmissionRoute
from test_capacity_routes import _scope

pytestmark = [pytest.mark.unit, pytest.mark.capacity_contract]


def application(tmp_path, monkeypatch, endpoint):
    app = FastAPI()
    manager = app.state.capacity = AdmissionManager()
    app.state.config = SimpleNamespace(preparation_spool_dir=tmp_path/'spool')
    app.state.control_pool = object()
    live = {'lease':False, 'op':None}
    @asynccontextmanager
    async def lease(pool, principal):
        assert current_owner().principal == principal
        live['lease'] = True
        try:
            yield SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
        finally:
            if live['op'] is not None: assert live['op'].closed
            live['lease'] = False
    monkeypatch.setattr('app.capacity_routes.report_account_work',lease)
    principal = AccountPrincipal(1,True,1)
    async def identity(request:Request):
        request.state.principal = principal
        return {'id':1}
    app.dependency_overrides[require_report_user] = identity
    router = APIRouter(route_class=AdmissionRoute)
    @router.get('/report/{year}/export')
    async def report(request:Request,year:int,user=Depends(require_report_user)):
        live['op'] = request.state._preparation
        return await endpoint(request, live)
    app.include_router(router)
    return app, manager, live


@pytest.mark.parametrize('send_failure',[False,True])
def test_report_response_retains_lease_until_file_cleanup(tmp_path,monkeypatch,send_failure):
    async def endpoint(request,live):
        op = live['op']
        with op.budget.open('result','wb') as file: file.write(b'report')
        await op.finish_preparation()
        return op.prepared('result',media_type='text/plain')
    app,manager,live = application(tmp_path,monkeypatch,endpoint)
    async def run():
        async def receive(): await asyncio.Future()
        async def send(message):
            assert live['lease'] and live['op'].directory.exists()
            assert manager.snapshot()['foreground']['active'] == 1
            if send_failure and message['type']=='http.response.body': raise RuntimeError('send failed')
        if send_failure:
            with pytest.raises(RuntimeError,match='send failed'):
                await app(_scope('/report/2026/export','GET'),receive,send)
        else: await app(_scope('/report/2026/export','GET'),receive,send)
        assert live['op'].closed and not live['lease']
        assert not any(manager._active.values())
    asyncio.run(run())


def test_report_handler_failure_cleans_before_lease_unlock(tmp_path,monkeypatch):
    async def endpoint(request,live):
        with live['op'].budget.open('partial','wb') as file: file.write(b'partial')
        raise RuntimeError('render failed')
    app,manager,live = application(tmp_path,monkeypatch,endpoint)
    async def run():
        async def receive(): await asyncio.Future()
        async def send(message): pass
        with pytest.raises(RuntimeError,match='render failed'):
            await app(_scope('/report/2026/export','GET'),receive,send)
        assert live['op'].closed and not live['lease']
        assert not any(manager._active.values())
    asyncio.run(run())


def test_report_disconnect_drains_handler_cleanup_before_unlock(tmp_path,monkeypatch):
    async def run():
        entered,cleaning,release,disconnected = (asyncio.Event() for _ in range(4))
        async def endpoint(request,live):
            entered.set()
            try: await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                assert live['lease'] and not live['op'].closed
        app,manager,live = application(tmp_path,monkeypatch,endpoint)
        async def receive():
            await disconnected.wait()
            return {'type':'http.disconnect'}
        async def send(message): pass
        task = asyncio.create_task(app(_scope('/report/2026/export','GET'),receive,send))
        await entered.wait(); disconnected.set(); await cleaning.wait()
        task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
        assert not task.done() and live['lease']
        assert manager.snapshot()['foreground']['active'] == 1
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert live['op'].closed and not live['lease']
        assert not any(manager._active.values())
    asyncio.run(run())
