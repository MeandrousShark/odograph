"""Navigation reserves full-dashboard work before any tag form is parsed."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from starlette.responses import Response

from app.account_context import AccountPrincipal
from app.auth import AuthRedirect, require_csrf, require_user
from app.capacity import AdmissionManager, CapacityBusy, current_owner
from app.capacity_routes import AdmissionRoute
from app.page import render_template

pytestmark = [pytest.mark.unit, pytest.mark.capacity_contract]


def _app(monkeypatch, **settings):
    app = FastAPI()
    app.state.capacity = AdmissionManager(SimpleNamespace(**settings))
    app.state.control_pool = object()

    @asynccontextmanager
    async def lease(_pool, account_id):
        assert current_owner().principal.account_id == account_id
        yield

    monkeypatch.setattr('app.capacity_routes.external_account_work', lease)

    async def identity(request: Request):
        account_id = int(request.headers.get('x-test-account', '1'))
        request.state.principal = AccountPrincipal(account_id, True, 1)
        return {'id': account_id}

    app.dependency_overrides[require_user] = identity
    return app


def test_tag_navigation_reservation_precedes_form_reads_and_preserves_part_bound(monkeypatch):
    async def run():
        app = _app(monkeypatch, capacity_navigation_wait_s=.02)
        router = APIRouter(route_class=AdmissionRoute)
        parsed = []

        @router.post('/trips/{trip_id}/tag')
        async def tag(trip_id: int, category: str = Form(...), user=Depends(require_user)):
            assert current_owner().lane == 'navigation'
            parsed.append(category)
            return Response(str(len(category)).encode())

        app.include_router(router)
        reads = []

        async def body():
            reads.append(True)
            yield b'category=business'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            occupied, release = asyncio.Event(), asyncio.Event()

            async def hold():
                async with app.state.capacity.operation('navigation', principal=AccountPrincipal(2, True, 1)):
                    occupied.set()
                    await release.wait()

            holder = asyncio.create_task(hold())
            try:
                await asyncio.wait_for(occupied.wait(), 2)
                refused = await client.post('/trips/1/tag', content=body(),
                    headers={'content-type': 'application/x-www-form-urlencoded'})
                assert refused.status_code == 503 and refused.headers['retry-after'] == '1'
                assert reads == [] and parsed == []
            finally:
                release.set()
                await holder
            # Navigation preserves the existing 1 MiB foreground form-part bound.
            large = await client.post('/trips/1/tag', files={'category': (None, 'x' * 100000)})
            assert large.status_code == 200 and large.text == '100000'
            oversized = await client.post('/trips/1/tag', files={'category': (None, 'x' * (1024 * 1024 + 1))})
            assert oversized.status_code == 400
            assert len(parsed) == 1
        assert not any(app.state.capacity._active.values())
    asyncio.run(run())


@pytest.mark.parametrize('route_path,lane', [
    ('/trips/{trip_id}/tag', 'navigation'), ('/trips/{trip_id}/split', 'foreground'),
])
def test_full_result_form_authenticates_before_body_and_bounds_receipt(monkeypatch, route_path, lane):
    async def run():
        app = _app(monkeypatch, capacity_import_body_timeout_s=.02)
        permitted = False

        async def identity(request: Request):
            if not permitted:
                raise AuthRedirect()
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}

        app.dependency_overrides[require_user] = identity

        @app.exception_handler(AuthRedirect)
        async def denied(_request, _exception):
            return Response(status_code=401)

        router = APIRouter(route_class=AdmissionRoute)

        @router.post(route_path)
        async def tag(trip_id: int, category: str = Form(...), user=Depends(require_user)):
            pytest.fail('denied or expired body reached endpoint')

        app.include_router(router)
        reads = []

        async def body():
            reads.append(True)
            assert current_owner().lane == lane
            await asyncio.sleep(.05)
            yield b'category=business'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            rejected = await client.post(route_path.format(trip_id=1), content=body(),
                headers={'content-type': 'application/x-www-form-urlencoded'})
            assert rejected.status_code == 401 and reads == []
            permitted = True
            expired = await client.post(route_path.format(trip_id=1), content=body(),
                headers={'content-type': 'application/x-www-form-urlencoded'})
            assert expired.status_code == 503 and expired.headers['retry-after'] == '1'
        assert len(reads) == 1 and not any(app.state.capacity._active.values())
    asyncio.run(run())


def test_real_tag_committed_mutation_survives_busy_dashboard_refresh(monkeypatch):
    from app.ui import make_router

    async def run():
        app = _app(monkeypatch)
        committed = []

        @asynccontextmanager
        async def connection():
            yield object()

        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            request.state.account_pool = SimpleNamespace(connection=connection)
            request.state.config = SimpleNamespace(display_tz=timezone.utc)
            return {'id': 1}

        async def csrf():
            pass

        async def mutate(_conn, trip_id, category):
            assert current_owner().lane == 'navigation'
            committed.append((trip_id, category))

        async def refresh(request, _anchor, _now):
            assert request.state._capacity_mutation_committed
            assert current_owner().lane == 'navigation'
            raise CapacityBusy('dashboard refresh is busy')

        app.dependency_overrides[require_user] = identity
        app.dependency_overrides[require_csrf] = csrf
        monkeypatch.setattr('app.ui.trips._apply_human_tag', mutate)
        monkeypatch.setattr('app.ui.stats._build_week_dashboard_context', refresh)
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/trips/9/tag', data={
                'category': 'business', 'dashboard_week': '2026-10-04',
            })
        assert committed == [(9, 'business')]
        assert response.status_code == 204 and response.headers['hx-refresh'] == 'true'
        assert not any(app.state.capacity._active.values())
    asyncio.run(run())


def test_distinct_account_navigation_serves_during_bulk_export(monkeypatch):
    async def run():
        app = _app(monkeypatch)
        router = APIRouter(route_class=AdmissionRoute)
        entered, release = asyncio.Event(), asyncio.Event()

        @router.get('/export')
        async def export(user=Depends(require_user)):
            assert current_owner().lane == 'foreground'
            entered.set()
            await release.wait()
            return Response(b'export')

        @router.get('/')
        async def dashboard(user=Depends(require_user)):
            assert current_owner().lane == 'navigation'
            assert app.state.capacity.snapshot()['foreground']['active'] == 1
            return Response(b'all dashboard trips')

        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            bulk = asyncio.create_task(client.get('/export', headers={'x-test-account': '1'}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                dashboard_response = await asyncio.wait_for(client.get('/', headers={'x-test-account': '2'}), 1)
                assert dashboard_response.status_code == 200 and dashboard_response.content == b'all dashboard trips'
                same_account = await client.get('/', headers={'x-test-account': '1'})
                assert same_account.status_code == 503 and same_account.headers['retry-after'] == '1'
            finally:
                release.set()
                await bulk
        assert not any(app.state.capacity._active.values())
    asyncio.run(run())


@pytest.mark.parametrize('lane', ['navigation', 'foreground'])
def test_full_result_render_retains_owner_until_actual_thread_finishes(lane):
    import threading

    async def run():
        manager = AdmissionManager(SimpleNamespace())
        started, release = threading.Event(), threading.Event()
        caller_thread = threading.get_ident()
        rendered = []

        def render(_request, template, context, **kwargs):
            assert threading.get_ident() != caller_thread
            assert current_owner().lane == lane
            started.set()
            release.wait(2)
            rendered.append((template, context, kwargs))
            return Response(b'all trips')

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            templates=SimpleNamespace(TemplateResponse=render))))

        async def work():
            async with manager.operation(lane, principal=AccountPrincipal(1, True, 1)):
                return await render_template(request, 'dashboard.html', {'trips': [1, 2]}, status_code=201)

        task = asyncio.create_task(work())
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and manager.snapshot()[lane]['active'] == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rendered == [('dashboard.html', {'trips': [1, 2]}, {'status_code': 201})]
        assert manager.snapshot()[lane]['active'] == 0
    asyncio.run(run())
