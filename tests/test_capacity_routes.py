"""Actual ASGI admission, bounded receipt and response ownership contracts."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from psycopg.pq import TransactionStatus
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from app.account_context import AccountPrincipal, control_connection
from app.auth import require_user
from app.capacity import AdmissionManager, current_owner, owned_thread
from app.capacity_routes import (
    AdmissionRoute, FOREGROUND_ROUTES, INTERACTIVE_ROUTES, NAVIGATION_ROUTES, capacity_policy,
)
from app.ingest import FailedAuthLimiter
from app.uploads import bounded_multipart_form

pytestmark = [pytest.mark.unit, pytest.mark.capacity_contract]


def _manager(**settings):
    return AdmissionManager(SimpleNamespace(**settings))


def _scope(path, method='POST', headers=()):
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
            'query_string': b'', 'headers': list(headers),
            'client': ('192.0.2.1', 1000), 'server': ('test', 80)}


def test_audited_inventory_uses_actual_registered_routes():
    from app.auth import make_router as auth_router
    from app.ingest import make_router as ingest_router
    from app.portable.routes import make_router as portable_router
    from app.ui import make_router as ui_router
    routes = [route for make in (auth_router, ingest_router, portable_router, ui_router)
              for route in make().routes]
    registered = {(method, route.path) for route in routes for method in route.methods}
    assert NAVIGATION_ROUTES <= registered
    assert NAVIGATION_ROUTES.isdisjoint(FOREGROUND_ROUTES)
    assert FOREGROUND_ROUTES <= registered
    assert INTERACTIVE_ROUTES <= registered
    for route in routes:
        assert isinstance(route, AdmissionRoute)
        for method in route.methods:
            if (method, route.path) in NAVIGATION_ROUTES:
                assert route.capacity_lane == 'navigation'
            if (method, route.path) in FOREGROUND_ROUTES:
                assert route.capacity_lane == 'foreground'
            if (method, route.path) in INTERACTIVE_ROUTES:
                assert route.capacity_lane == 'auth_interactive'
    policies = {(method, route.path): route.capacity_lane
                for route in routes for method in route.methods}
    assert policies[('GET', '/trips/{trip_id}/card')] is None
    assert policies[('GET', '/trips/{trip_id}/edit')] is None
    assert policies[('POST', '/trips/{trip_id}/edit')] == 'form'
    assert policies[('POST', '/trips/{trip_id}/delete')] == 'form'
    assert policies[('GET', '/review')] is None
    assert policies[('GET', '/settings')] is None


def test_busy_auth_refuses_before_any_body_receive():
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        router = APIRouter(route_class=AdmissionRoute)
        reached = False
        @router.post('/login/local')
        async def login(password: str = Form(...)):
            nonlocal reached
            reached = True
        app.include_router(router)
        reads = 0
        async def receive():
            nonlocal reads
            reads += 1
            return {'type': 'http.request', 'body': b'password=x'}
        messages = []
        async def send(message):
            messages.append(message)
        entered, release = asyncio.Event(), asyncio.Event()
        async def occupy():
            async with manager.operation('auth_interactive'):
                entered.set()
                await release.wait()
        occupied = asyncio.create_task(occupy())
        await entered.wait()
        await app(_scope('/login/local'), receive, send)
        release.set()
        await occupied
        assert reads == 0 and not reached
        assert messages[0]['status'] == 503
        assert (b'retry-after', b'1') in messages[0]['headers']
        assert not any(manager._active.values())
    asyncio.run(run())


@pytest.mark.parametrize('declared', [False, True])
def test_auth_form_cap_and_deadline_before_endpoint(declared):
    async def run():
        app = FastAPI()
        app.state.capacity = _manager(capacity_auth_form_max_bytes=16)
        router = APIRouter(route_class=AdmissionRoute)
        calls = []
        @router.post('/login/local')
        async def login(password: str = Form(...)):
            calls.append(password)
        app.include_router(router)
        headers = [(b'content-type', b'application/x-www-form-urlencoded')]
        if declared:
            headers.append((b'content-length', b'40'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/login/local', content=b'password=' + b'x' * 32, headers=dict(headers))
        assert response.status_code == 413
        assert calls == []
        assert not any(app.state.capacity._active.values())
    asyncio.run(run())


def test_auth_total_receipt_deadline_releases_owner():
    async def run():
        app = FastAPI()
        app.state.capacity = _manager(capacity_auth_body_timeout_s=.02)
        router = APIRouter(route_class=AdmissionRoute)
        @router.post('/login/local')
        async def login(password: str = Form(...)):
            pytest.fail('deadline must stop parsing')
        app.include_router(router)
        async def receive():
            await asyncio.sleep(.05)
            return {'type': 'http.request', 'body': b'password=x'}
        messages = []
        async def send(message):
            messages.append(message)
        await app(_scope('/login/local'), receive, send)
        assert messages[0]['status'] == 503
        assert not any(app.state.capacity._active.values())
    asyncio.run(run())


@pytest.mark.parametrize('path,lane', [('/export', 'foreground'), ('/', 'navigation')])
def test_foreground_retains_owner_and_lease_through_response_send(monkeypatch, path, lane):
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.control_pool = object()
        app.state.config = SimpleNamespace(preparation_spool_dir='')
        lease_live = False
        @asynccontextmanager
        async def lease(pool, *ids):
            nonlocal lease_live
            assert current_owner().lane == lane
            lease_live = True
            try:
                yield SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
            finally:
                lease_live = False
        target = 'report_account_work' if path == '/export' else 'external_account_work'
        monkeypatch.setattr('app.capacity_routes.' + target, lease)
        principal = AccountPrincipal(1, True, 1)
        async def identity(request: Request):
            request.state.principal = principal
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.get(path)
        async def export(request: Request, user=Depends(require_user)):
            assert current_owner().principal == principal
            return Response(b'export')
        app.include_router(router)
        async def receive():
            await asyncio.Future()
        async def send(message):
            assert lease_live
            assert len(manager._active[lane]) == 1
            await asyncio.sleep(.001)
        await app(_scope(path, 'GET'), receive, send)
        assert not lease_live and not any(manager._active.values())
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['overflow', 'disconnect', 'cancel'])
def test_partial_multipart_spools_close_on_every_receive_failure(monkeypatch, failure):
    import starlette.formparsers as parsers
    spools = []
    original = parsers.SpooledTemporaryFile
    def tracked(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool
    monkeypatch.setattr(parsers, 'SpooledTemporaryFile', tracked)
    async def run():
        first = b'--b\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\npart'
        reads = 0
        async def receive():
            nonlocal reads
            reads += 1
            if reads == 1:
                return {'type': 'http.request', 'body': first, 'more_body': True}
            if failure == 'disconnect':
                return {'type': 'http.disconnect'}
            if failure == 'cancel':
                raise asyncio.CancelledError()
            from fastapi import HTTPException
            raise HTTPException(413, 'too large')
        request = Request(_scope('/upload', headers=[(b'content-type', b'multipart/form-data; boundary=b')]), receive)
        expected = {'disconnect': ClientDisconnect, 'cancel': asyncio.CancelledError,
                    'overflow': __import__('fastapi').HTTPException}[failure]
        with pytest.raises(expected):
            async with bounded_multipart_form(request):
                pytest.fail('receive failed before form completion')
        assert spools and all(spool.closed for spool in spools)
    asyncio.run(run())


def test_failed_auth_map_is_bounded_and_does_not_evict_failure_history():
    now = [0.]
    limiter = FailedAuthLimiter(2, 10, clock=lambda: now[0], max_keys=2)
    limiter.record_failure('known')
    limiter.record_failure('known')
    limiter.record_failure('second')
    for index in range(100):
        limiter.record_failure('new' + str(index))
    assert len(limiter._failures) == 2
    assert limiter.blocked('known') and limiter.blocked('new')
    assert all(len(q) <= 2 for q in limiter._failures.values())
    now[0] = 11
    assert not limiter.blocked('new')
    assert not limiter._failures


@pytest.mark.parametrize('path,lane', [('/export', 'foreground'), ('/', 'navigation')])
def test_cancelled_render_keeps_asgi_lease_until_actual_thread_finishes(monkeypatch, path, lane):
    import threading
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.control_pool = object()
        app.state.config = SimpleNamespace(preparation_spool_dir='')
        started, release = threading.Event(), threading.Event()
        lease_live = False
        @asynccontextmanager
        async def lease(pool, *ids):
            nonlocal lease_live
            lease_live = True
            try:
                yield SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
            finally:
                lease_live = False
        target = 'report_account_work' if path == '/export' else 'external_account_work'
        monkeypatch.setattr('app.capacity_routes.' + target, lease)
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        def render():
            started.set()
            release.wait(2)
            return b'done'
        @router.get(path)
        async def export(request: Request, user=Depends(require_user)):
            return Response(await owned_thread(render))
        app.include_router(router)
        async def receive():
            await asyncio.Future()
        async def send(message):
            pass
        task = asyncio.create_task(app(_scope(path, 'GET'), receive, send))
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        assert lease_live and len(manager._active[lane]) == 1
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and lease_live
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not lease_live and not any(manager._active.values())
    asyncio.run(run())


@pytest.mark.parametrize('path,lane', [('/export', 'foreground'), ('/', 'navigation')])
def test_foreground_send_deadline_releases_owner_and_lease(monkeypatch, path, lane):
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager(capacity_response_timeout_s=.02)
        app.state.control_pool = object()
        app.state.config = SimpleNamespace(preparation_spool_dir='')
        lease_live = False
        @asynccontextmanager
        async def lease(pool, *ids):
            nonlocal lease_live
            lease_live = True
            try:
                yield SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
            finally:
                lease_live = False
        target = 'report_account_work' if path == '/export' else 'external_account_work'
        monkeypatch.setattr('app.capacity_routes.' + target, lease)
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.get(path)
        async def export(user=Depends(require_user)):
            return Response(b'data')
        app.include_router(router)
        async def receive():
            await asyncio.Future()
        async def send(message):
            await asyncio.sleep(.05)
        with pytest.raises(TimeoutError):
            await app(_scope(path, 'GET'), receive, send)
        assert not lease_live and not any(manager._active.values())
    asyncio.run(run())


def test_ingest_basic_header_bound_refuses_before_lookup_and_body(monkeypatch):
    from app.ingest import make_router
    async def unexpected(*args, **kwargs):
        pytest.fail('oversized Basic header reached credential lookup')
    monkeypatch.setattr('app.ingest.authenticate_ingest', unexpected)
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='')
        app.state.ingest_limiter = FailedAuthLimiter(10, 900)
        app.include_router(make_router())
        reads = 0
        async def receive():
            nonlocal reads
            reads += 1
            return {'type': 'http.request', 'body': b'payload'}
        messages = []
        async def send(message):
            messages.append(message)
        await app(_scope('/ingest', headers=[(b'authorization', b'Basic ' + b'x' * 8192)]), receive, send)
        assert messages[0]['status'] == 401 and reads == 0
        assert not any(manager._active.values())
    asyncio.run(run())


def test_streamed_import_envelope_cap_closes_spool_without_content_length(monkeypatch):
    from app.auth import require_import_user
    import starlette.formparsers as parsers
    spools = []
    original = parsers.SpooledTemporaryFile
    def tracked(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool
    monkeypatch.setattr(parsers, 'SpooledTemporaryFile', tracked)
    @asynccontextmanager
    async def lease(*args):
        yield
    monkeypatch.setattr('app.capacity_routes.external_account_work', lease)
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager(capacity_multipart_overhead_bytes=64)
        app.state.config = SimpleNamespace(portable_import_max_bytes=200)
        app.state.control_pool = object()
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_import_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.post('/settings/import/data')
        async def upload(request: Request, user=Depends(require_import_user)):
            async with bounded_multipart_form(request):
                pytest.fail('oversized envelope reached completed form')
        app.include_router(router)
        chunks = [b'--b\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\npart',
                  b'x' * 300]
        async def receive():
            return {'type': 'http.request', 'body': chunks.pop(0), 'more_body': True}
        messages = []
        async def send(message):
            messages.append(message)
        await app(_scope('/settings/import/data', headers=[(b'content-type', b'multipart/form-data; boundary=b')]),
                  receive, send)
        assert messages[0]['status'] == 413
        assert spools and all(spool.closed for spool in spools)
        assert not any(manager._active.values())
    asyncio.run(run())


def test_every_form_route_has_an_early_body_policy():
    from app.auth import make_router as auth_router
    from app.ui import make_router as ui_router
    for make in (auth_router, ui_router):
        for route in make().routes:
            if route.has_form:
                assert route.capacity_lane in ('navigation', 'foreground', 'auth_interactive', 'form')


def test_small_authenticated_form_validates_before_body_and_releases_parse_owner():
    from app.auth import AuthRedirect
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        router = APIRouter(route_class=AdmissionRoute)
        authenticated = False
        calls = []
        async def identity(request: Request):
            if not authenticated:
                raise AuthRedirect()
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        @app.exception_handler(AuthRedirect)
        async def denied(request, exception):
            return Response(status_code=401)
        @router.post('/small-edit')
        async def edit(request: Request, note: str = Form(...), user=Depends(require_user)):
            assert not manager._active['auth_interactive']
            calls.append(note)
            return Response(status_code=204)
        app.include_router(router)
        reads = 0
        async def body():
            nonlocal reads
            reads += 1
            yield b'note=hello'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            denied = await client.post('/small-edit', content=body(), headers={'content-type': 'application/x-www-form-urlencoded'})
            assert denied.status_code == 401 and reads == 0
            authenticated = True
            saved = await client.post('/small-edit', content=body(), headers={'content-type': 'application/x-www-form-urlencoded'})
            assert saved.status_code == 204 and reads == 1 and calls == ['hello']
        assert not any(manager._active.values())
    asyncio.run(run())


def test_foreground_batch_form_preserves_large_selection(monkeypatch):
    @asynccontextmanager
    async def lease(*args):
        yield
    monkeypatch.setattr('app.capacity_routes.external_account_work', lease)
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.control_pool = object()
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.post('/trips/batch_update')
        async def batch(selection: str = Form(...), user=Depends(require_user)):
            return Response(str(len(selection)).encode())
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/trips/batch_update', data={'selection': 'x' * 100000})
        assert response.status_code == 200 and response.text == '100000'
        assert not any(manager._active.values())
    asyncio.run(run())


def test_committed_small_edit_refresh_busy_reports_completion():
    from app.capacity import CapacityBusy
    async def run():
        app = FastAPI()
        app.state.capacity = _manager()
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.post('/small-edit')
        async def edit(request: Request, note: str = Form(...), user=Depends(require_user)):
            request.state._capacity_mutation_committed = True
            raise CapacityBusy('read-only refresh is busy')
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/small-edit', data={'note': 'saved'})
        assert response.status_code == 204 and response.headers['hx-refresh'] == 'true'
    asyncio.run(run())


def test_foreground_multipart_selection_retains_framework_one_megabyte_part_limit(monkeypatch):
    @asynccontextmanager
    async def lease(*args):
        yield
    monkeypatch.setattr('app.capacity_routes.external_account_work', lease)
    async def run():
        app = FastAPI()
        app.state.capacity = _manager()
        app.state.control_pool = object()
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.post('/trips/batch_update')
        async def batch(selection: str = Form(...), user=Depends(require_user)):
            return Response(str(len(selection)).encode())
        app.include_router(router)
        # A 50,000-ID snapshot submitted as one form field stays above 64 KiB.
        selection = ','.join(str(i) for i in range(50000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/trips/batch_update', files={'selection': (None, selection)})
        assert response.status_code == 200 and response.text == str(len(selection))
    asyncio.run(run())


def test_committed_tracking_credential_remains_visible_if_refresh_is_busy():
    from app.capacity import CapacityBusy
    from app.tracking import IssuedCredential
    from app.ui.tracking import _render_tracking
    @asynccontextmanager
    async def busy_connection():
        raise CapacityBusy('routine refresh queue expired')
        yield
    async def run():
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(app_url='https://test.example'))),
            state=SimpleNamespace(account_pool=SimpleNamespace(connection=busy_connection)),
        )
        issued = IssuedCredential('public', 'user', '<secret>', 1)
        response = await _render_tracking(request, {'id': 1}, issued)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert b'credential saved' in response.body
        assert b'&lt;secret&gt;' in response.body and b'<secret>' not in response.body
        assert b'user' in response.body
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['overflow', 'disconnect', 'cancel'])
def test_avatar_actual_http_chunked_failure_closes_spools_and_owner(monkeypatch, failure):
    from app import auth
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.responses import HTMLResponse
    import starlette.formparsers as parsers
    spools = []
    original = parsers.SpooledTemporaryFile
    def tracked(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool
    monkeypatch.setattr(parsers, 'SpooledTemporaryFile', tracked)
    @asynccontextmanager
    async def lease(*args):
        yield
    monkeypatch.setattr('app.capacity_routes.external_account_work', lease)
    @asynccontextmanager
    async def connection(pool, **kwargs):
        yield object()
    monkeypatch.setattr(auth, 'control_connection', connection)
    async def account(*args):
        return {'id': 7, 'email': 'avatar@example.com', 'password_hash': 'hash',
                'is_enabled': True, 'is_admin': True, 'auth_version': 3}
    monkeypatch.setattr(auth, 'get_account', account)
    async def unverified(*args):
        return False
    monkeypatch.setattr(auth, 'is_current_email_verified', unverified)
    async def run():
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key='test-only-avatar')
        manager = app.state.capacity = _manager(capacity_multipart_overhead_bytes=64)
        app.state.config = SimpleNamespace(dev_no_auth=False, account_avatar_max_bytes=128,
                                          smtp_host='', email_from='', app_url='')
        app.state.control_pool = object()
        app.state.oauth = None
        def render(request, name, context, **kwargs):
            return HTMLResponse(context.get('error') or '', status_code=kwargs.get('status_code', 200))
        app.state.templates = SimpleNamespace(TemplateResponse=render)
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(7, True, 3)
            return {'id': 7}
        app.dependency_overrides[require_user] = identity
        app.include_router(auth.make_router())
        reads = 0
        async def receive():
            nonlocal reads
            reads += 1
            if reads == 1:
                return {'type': 'http.request', 'more_body': True,
                        'body': b'--b\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\npart'}
            if failure == 'overflow':
                return {'type': 'http.request', 'more_body': True, 'body': b'x' * 300}
            if failure == 'disconnect':
                return {'type': 'http.disconnect'}
            raise asyncio.CancelledError()
        messages = []
        async def send(message):
            messages.append(message)
        scope = _scope('/settings/account/avatar', headers=[(b'content-type', b'multipart/form-data; boundary=b')])
        if failure == 'overflow':
            await app(scope, receive, send)
            assert messages[0]['status'] == 413
            assert b'Avatar exceeds the 128 bytes limit.' in messages[1]['body']
        else:
            with pytest.raises(ClientDisconnect if failure == 'disconnect' else asyncio.CancelledError):
                await app(scope, receive, send)
        assert spools and all(spool.closed for spool in spools)
        assert not any(manager._active.values())
    asyncio.run(run())


def test_avatar_cancelled_disk_spool_write_retains_owner_and_lease(monkeypatch):
    import threading
    import starlette.formparsers as parsers
    entered, release = threading.Event(), threading.Event()
    spools = []
    original = parsers.SpooledTemporaryFile
    class BlockingSpool:
        def __init__(self, *args, **kwargs):
            self.file = original(max_size=1)
            spools.append(self)
        def __getattr__(self, name):
            return getattr(self.file, name)
        def write(self, data):
            entered.set()
            release.wait(2)
            return self.file.write(data)
        def close(self):
            self.file.close()
    monkeypatch.setattr(parsers, 'SpooledTemporaryFile', BlockingSpool)
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager(capacity_multipart_overhead_bytes=64)
        app.state.config = SimpleNamespace(account_avatar_max_bytes=128)
        app.state.control_pool = object()
        lease_live = False
        @asynccontextmanager
        async def lease(*args):
            nonlocal lease_live
            lease_live = True
            try:
                yield
            finally:
                lease_live = False
        monkeypatch.setattr('app.capacity_routes.external_account_work', lease)
        async def identity(request: Request):
            request.state.principal = AccountPrincipal(1, True, 1)
            return {'id': 1}
        app.dependency_overrides[require_user] = identity
        router = APIRouter(route_class=AdmissionRoute)
        @router.post('/settings/account/avatar')
        async def avatar(request: Request, user=Depends(require_user)):
            async with bounded_multipart_form(request):
                pytest.fail('cancelled upload reached complete form')
        app.include_router(router)
        async def receive():
            return {'type': 'http.request', 'more_body': True,
                    'body': b'--b\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\npart'}
        async def send(message):
            pass
        task = asyncio.create_task(app(_scope('/settings/account/avatar', headers=[
            (b'content-type', b'multipart/form-data; boundary=b')]), receive, send))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and lease_live
            assert len(manager._active['foreground']) == 1
            assert spools and not spools[0].closed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not lease_live and all(spool.closed for spool in spools)
        assert not any(manager._active.values())
    asyncio.run(run())


def test_repeated_ingest_cancellation_retains_auth_owner_until_verifier_finishes(monkeypatch):
    import threading
    from app.ingest import make_router
    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager(
            capacity_auth_ingest_slots=1, capacity_auth_ingest_pending=0,
        )
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='')
        app.state.control_pool = object()
        limiter = app.state.ingest_limiter = FailedAuthLimiter(10, 900)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        finish_thread = threading.Event()
        calls = []

        def verify_thread():
            loop.call_soon_threadsafe(entered.set)
            assert finish_thread.wait(5), 'test did not release actual verification work'

        async def verify(*args, **kwargs):
            calls.append(True)
            await owned_thread(verify_thread)
            return None
        monkeypatch.setattr('app.ingest.authenticate_ingest', verify)
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            caller = asyncio.create_task(client.post('/ingest', auth=('user', 'password')))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                caller.cancel()
                await asyncio.sleep(0)
                caller.cancel()
                await asyncio.sleep(0)
                assert not caller.done()
                assert len(manager._active['auth_ingest']) == 1
                assert len(limiter._auth_tasks) == 1
                refused = await client.post('/ingest', auth=('another', 'password'))
                assert refused.status_code == 503
                assert refused.headers['retry-after'] == '1'
                assert calls == [True]
            finally:
                finish_thread.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
        assert not limiter._auth_tasks
        assert not any(manager._active.values())
        assert sum(len(q) for q in limiter._failures.values()) == 1
    asyncio.run(run())


def test_ingest_auth_queue_holds_two_active_and_eight_fifo_waiters(monkeypatch):
    from app.ingest import make_router

    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='', ingest_max_body_bytes=1000)
        app.state.control_pool = object()
        app.state.ingest_limiter = FailedAuthLimiter(20, 60)
        entered = {str(index): asyncio.Event() for index in range(11)}
        release = {name: asyncio.Event() for name in entered}
        calls = []

        async def verify(_pool, username, _password, **kwargs):
            calls.append(username)
            entered[username].set()
            await release[username].wait()
            return SimpleNamespace(account=AccountPrincipal(int(username) + 1, True, 1))

        monkeypatch.setattr('app.ingest.authenticate_ingest', verify)
        app.include_router(make_router())
        body_reads = []

        async def body():
            body_reads.append(True)
            yield b'{}'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            requests = []
            try:
                for name in ('0', '1'):
                    requests.append(asyncio.create_task(client.post('/ingest', content=b'', auth=(name, 'secret'))))
                    await asyncio.wait_for(entered[name].wait(), 2)
                assert manager.snapshot()['auth_ingest'] == {'active': 2, 'pending': 0}

                for index in range(2, 10):
                    name = str(index)
                    requests.append(asyncio.create_task(client.post('/ingest', content=b'', auth=(name, 'secret'))))
                    async def pending_count(expected):
                        while manager.snapshot()['auth_ingest']['pending'] != expected:
                            await asyncio.sleep(0)
                    await asyncio.wait_for(pending_count(index - 1), 2)

                async def eleventh_body():
                    body_reads.append(True)
                    yield b'private'

                refused = await client.post('/ingest', content=eleventh_body(), auth=('10', 'secret'))
                assert refused.status_code == 503 and refused.headers['retry-after'] == '1'
                assert manager.snapshot()['auth_ingest'] == {'active': 2, 'pending': 8}
                assert calls == ['0', '1'] and body_reads == []

                release['0'].set()
                assert (await asyncio.wait_for(requests[0], 2)).status_code == 200
                await asyncio.wait_for(entered['2'].wait(), 2)
                assert calls == ['0', '1', '2']
                assert manager.snapshot()['auth_ingest'] == {'active': 2, 'pending': 7}
            finally:
                for event in release.values():
                    event.set()
                await asyncio.gather(*requests, return_exceptions=True)

        assert manager.snapshot()['auth_ingest'] == {'active': 0, 'pending': 0}
        assert not any(manager.snapshot()[lane]['active'] or manager.snapshot()[lane]['pending'] for lane in manager.lanes)
    asyncio.run(run())


def test_ingest_identity_lookup_has_one_active_and_one_waiter(monkeypatch):
    from app.ingest import make_router

    class Cursor:
        async def fetchone(self):
            return (None,)

    class Connection:
        async def execute(self, *_args):
            return Cursor()

    class Pool:
        active = 0

        @asynccontextmanager
        async def connection(self):
            self.active += 1
            try:
                yield Connection()
            finally:
                self.active -= 1

    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='', ingest_max_body_bytes=1000)
        pool = Pool()
        app.state.control_pool = manager.manage_pool(pool, 'control')
        app.state.ingest_limiter = FailedAuthLimiter(20, 60)
        entered = {name: asyncio.Event() for name in ('first', 'second')}
        release = {name: asyncio.Event() for name in entered}

        async def verify(control_pool, username, _password, **kwargs):
            async with control_connection(control_pool, lane='ingest_identity'):
                entered[username].set()
                await release[username].wait()
            return SimpleNamespace(account=AccountPrincipal(1 if username == 'first' else 2, True, 1))

        monkeypatch.setattr('app.ingest.authenticate_ingest', verify)
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            first = asyncio.create_task(client.post('/ingest', content=b'', auth=('first', 'secret')))
            second = None
            try:
                await asyncio.wait_for(entered['first'].wait(), 2)
                second = asyncio.create_task(client.post('/ingest', content=b'', auth=('second', 'secret')))
                async def identity_waiter_ready():
                    while manager.snapshot()['ingest_identity']['pending'] != 1:
                        await asyncio.sleep(0)
                await asyncio.wait_for(identity_waiter_ready(), 2)
                assert manager.snapshot()['auth_ingest'] == {'active': 2, 'pending': 0}
                assert manager.snapshot()['ingest_identity'] == {'active': 1, 'pending': 1}
                assert pool.active == 1

                release['first'].set()
                assert (await asyncio.wait_for(first, 2)).status_code == 200
                await asyncio.wait_for(entered['second'].wait(), 2)
                assert manager.snapshot()['ingest_identity'] == {'active': 1, 'pending': 0}
                assert pool.active == 1
                release['second'].set()
                assert (await asyncio.wait_for(second, 2)).status_code == 200
            finally:
                for event in release.values():
                    event.set()
                await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
        assert pool.active == 0
        assert not any(manager.snapshot()[lane]['active'] or manager.snapshot()[lane]['pending'] for lane in manager.lanes)
    asyncio.run(run())


def test_ingest_auth_rechecks_failure_limiter_after_wait(monkeypatch):
    from app.ingest import make_router

    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager(capacity_auth_ingest_slots=1, capacity_auth_ingest_pending=1)
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='', ingest_max_body_bytes=1000)
        app.state.control_pool = object()
        app.state.ingest_limiter = FailedAuthLimiter(1, 60)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def verify(_pool, username, _password, **kwargs):
            calls.append(username)
            entered.set()
            await release.wait()
            return None

        monkeypatch.setattr('app.ingest.authenticate_ingest', verify)
        app.include_router(make_router())
        body_reads = []

        async def body():
            body_reads.append(True)
            yield b'private'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            first = asyncio.create_task(client.post('/ingest', content=b'', auth=('bad', 'secret')))
            waiter = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                waiter = asyncio.create_task(client.post('/ingest', content=body(), auth=('queued', 'secret')))
                async def waiter_ready():
                    while manager.snapshot()['auth_ingest']['pending'] != 1:
                        await asyncio.sleep(0)
                await asyncio.wait_for(waiter_ready(), 2)
                release.set()
                assert (await asyncio.wait_for(first, 2)).status_code == 401
                response = await asyncio.wait_for(waiter, 2)
                assert response.status_code == 503 and response.headers['retry-after'] == '60'
                assert calls == ['bad'] and body_reads == []
                assert sum(len(failures) for failures in app.state.ingest_limiter._failures.values()) == 1
            finally:
                release.set()
                await asyncio.gather(*(task for task in (first, waiter) if task is not None), return_exceptions=True)
        assert not any(manager.snapshot()[lane]['active'] or manager.snapshot()[lane]['pending'] for lane in manager.lanes)
    asyncio.run(run())


def test_oversized_and_malformed_basic_headers_are_rejected_before_body(monkeypatch):
    from app.ingest import make_router

    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager()
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='', ingest_max_body_bytes=1000)
        app.state.control_pool = object()
        app.state.ingest_limiter = FailedAuthLimiter(10, 60)
        app.include_router(make_router())
        occupied = asyncio.Event()
        release = asyncio.Event()
        entered = 0

        async def hold():
            nonlocal entered
            async with manager.operation('auth_ingest'):
                entered += 1
                if entered == 2:
                    occupied.set()
                await release.wait()

        holders = [asyncio.create_task(hold()) for _ in range(2)]
        await asyncio.wait_for(occupied.wait(), 2)
        received, sent = [], []

        async def receive():
            received.append(True)
            return {'type': 'http.request', 'body': b'private', 'more_body': False}

        async def send(message):
            sent.append(message)

        try:
            oversized = b'Basic ' + b'\xff' * 8192
            await app(_scope('/ingest', headers=[(b'authorization', oversized)]), receive, send)
            assert sent[0]['status'] == 401
            assert received == []
            assert manager.snapshot()['auth_ingest'] == {'active': 2, 'pending': 0}
            assert sum(len(failures) for failures in app.state.ingest_limiter._failures.values()) == 1
        finally:
            release.set()
            await asyncio.gather(*holders)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            malformed = await client.post('/ingest', headers={'authorization': 'Basic !!!'}, content=b'private')
        assert malformed.status_code == 401
        assert sum(len(failures) for failures in app.state.ingest_limiter._failures.values()) == 2
        sent.clear()
        await app(_scope('/ingest', headers=[(b'authorization', b'Basic \xff')]), receive, send)
        assert sent[0]['status'] == 401
        assert received == []
        assert sum(len(failures) for failures in app.state.ingest_limiter._failures.values()) == 3
        assert not any(manager.snapshot()[lane]['active'] or manager.snapshot()[lane]['pending'] for lane in manager.lanes)
    asyncio.run(run())


def test_ingest_auth_wait_expiry_cancellation_and_shutdown_drain(monkeypatch):
    from app.ingest import make_router

    async def run():
        app = FastAPI()
        manager = app.state.capacity = _manager(
            capacity_auth_ingest_slots=1, capacity_auth_ingest_pending=1,
            capacity_auth_ingest_wait_s=.03,
        )
        app.state.config = SimpleNamespace(ingest_username='', ingest_password='', ingest_max_body_bytes=1000)
        app.state.control_pool = object()
        app.state.ingest_limiter = FailedAuthLimiter(20, 60)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def verify(_pool, username, _password, **kwargs):
            calls.append(username)
            entered.set()
            await release.wait()
            return SimpleNamespace(account=AccountPrincipal(1, True, 1))

        monkeypatch.setattr('app.ingest.authenticate_ingest', verify)
        app.include_router(make_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            first = asyncio.create_task(client.post('/ingest', content=b'', auth=('active', 'secret')))
            cancelled = expired = shutting_down = shutdown = None
            try:
                await asyncio.wait_for(entered.wait(), 2)

                cancelled = asyncio.create_task(client.post('/ingest', content=b'', auth=('cancelled', 'secret')))
                async def pending_one():
                    while manager.snapshot()['auth_ingest']['pending'] != 1:
                        await asyncio.sleep(0)
                await asyncio.wait_for(pending_one(), 2)
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                assert manager.snapshot()['auth_ingest'] == {'active': 1, 'pending': 0}

                expired = asyncio.create_task(client.post('/ingest', content=b'', auth=('expired', 'secret')))
                await asyncio.wait_for(pending_one(), 2)
                response = await asyncio.wait_for(expired, 2)
                assert response.status_code == 503 and response.headers['retry-after'] == '1'
                assert calls == ['active']

                shutting_down = asyncio.create_task(client.post('/ingest', content=b'', auth=('shutdown', 'secret')))
                await asyncio.wait_for(pending_one(), 2)
                shutdown = asyncio.create_task(manager.shutdown())
                response = await asyncio.wait_for(shutting_down, 2)
                assert response.status_code == 503 and not shutdown.done()
                assert manager.snapshot()['auth_ingest'] == {'active': 1, 'pending': 0}
                release.set()
                assert (await asyncio.wait_for(first, 2)).status_code == 503
                await asyncio.wait_for(shutdown, 2)
            finally:
                release.set()
                await asyncio.gather(*(task for task in (first, cancelled, expired, shutting_down, shutdown)
                                       if task is not None), return_exceptions=True)
        assert calls == ['active']
        assert not any(manager.snapshot()[lane]['active'] or manager.snapshot()[lane]['pending'] for lane in manager.lanes)
    asyncio.run(run())
