"""Admission on the actual matched route, before FastAPI reads form bodies."""
from __future__ import annotations

import asyncio
import inspect
from contextlib import AsyncExitStack
import time

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.params import Form as FormParameter
from starlette.responses import HTMLResponse, JSONResponse, Response

from app.account_work import external_account_work
from app.capacity import CapacityBusy

# These paths materialize complete selections or perform structural mutations.
# The keys describe server-owned route definitions, never client account claims.
NAVIGATION_ROUTES = frozenset({
    ('GET', '/'), ('POST', '/trips/{trip_id}/tag'),
})
FOREGROUND_ROUTES = frozenset({
    ('GET', '/stats'), ('GET', '/expenses'),
    ('GET', '/trips/selection'), ('GET', '/trips/month/{year}/{month}'),
    ('GET', '/trips/{trip_id}'), ('GET', '/trips/{trip_id}/points'),
    ('GET', '/export'), ('GET', '/report/range'), ('GET', '/report/range/export'),
    ('GET', '/report/{year}'), ('GET', '/report/{year}/export'),
    ('GET', '/settings/export/data'), ('POST', '/settings/import/data'),
    ('POST', '/settings/account/avatar'),
    ('POST', '/trips/batch_update'), ('POST', '/trips/batch_delete'),
    ('POST', '/trips/{trip_id}/merge_next'), ('POST', '/trips/{trip_id}/merge_prev'),
    ('POST', '/trips/merge_selected'), ('POST', '/trips/{trip_id}/split'),
    ('POST', '/settings/diagnostics/check'),
    ('POST', '/settings/boundary_overrides/{override_id}/delete'),
    ('POST', '/places'), ('POST', '/places/{place_id}/update'),
    ('POST', '/places/{place_id}/delete'), ('POST', '/rules'),
    ('POST', '/rules/{rule_id}/delete'),
})
INTERACTIVE_ROUTES = frozenset({
    ('POST', '/login/local'), ('POST', '/signup'), ('POST', '/invite'),
    ('POST', '/invite/oidc'), ('GET', '/login/oidc'), ('GET', '/auth/callback'),
    ('POST', '/account/establish'), ('POST', '/reset-password'),
    ('POST', '/forgot-password'), ('POST', '/settings/account/password'),
    ('POST', '/settings/account/email/change/request'),
    ('POST', '/settings/account/email/verify/request'),
    ('POST', '/settings/account/oidc/link'), ('POST', '/settings/account/oidc/reauth'),
    ('POST', '/settings/account/oidc/unlink'),
    ('POST', '/settings/tracking/devices'),
    ('POST', '/settings/tracking/devices/{device_id}/convert'),
    ('POST', '/settings/tracking/credentials/{public_id}/rotate'),
})


def capacity_setting(manager, name):
    from app.config import Config
    return getattr(getattr(manager, "config", None), name, Config.__dataclass_fields__[name].default)


async def release_authentication(request):
    """Release completed auth phases in their owning request task, before mail."""
    release = getattr(getattr(request, 'state', None), '_capacity_release_auth', None)
    if release is not None:
        await release()


def capacity_policy(lane):
    """Declare an audited route policy when its router lives in another module."""
    def decorate(endpoint):
        endpoint.capacity_lane = lane
        return endpoint
    return decorate


def busy_response(request: Request, *, importing=False):
    if importing:
        from app.auth import import_busy_response
        return import_busy_response()
    detail = 'The server is busy. Please try again.'
    headers = {'Retry-After': '1'}
    if request.headers.get('HX-Request') or 'text/html' in request.headers.get('accept', ''):
        return HTMLResponse(
            '<p role="alert">The server is busy. Please try again.</p>',
            status_code=503, headers=headers,
        )
    return JSONResponse({'ok': False, 'error': 'capacity_busy', 'detail': detail},
                        status_code=503, headers=headers)


async def _dependency(request, dependency):
    override = request.app.dependency_overrides.get(dependency, dependency)
    result = override(request)
    return await result if inspect.isawaitable(result) else result


class AdmissionRoute(APIRoute):
    @property
    def has_form(self):
        return self.body_field is not None and isinstance(self.body_field.field_info, FormParameter)

    def protected_dependency(self):
        from app.auth import require_admin, require_legacy_establishment, require_user
        calls = []
        def visit(dependant):
            calls.append(dependant.call)
            for child in dependant.dependencies:
                visit(child)
        visit(self.dependant)
        for dependency in (require_admin, require_legacy_establishment, require_user):
            if dependency in calls:
                return dependency
        return None

    def get_route_handler(self):
        original = super().get_route_handler()
        if not self.has_form:
            return original
        async def handler(request):
            if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() == 'multipart/form-data':
                from app.uploads import bounded_multipart_form
                # Preserve framework field limits for large authenticated selections.
                async with bounded_multipart_form(request, max_files=1000, max_fields=1000,
                        max_part_size=1024 * 1024 if self.capacity_lane in ("navigation", "foreground") else 64 * 1024) as form:
                    request._form = form
                    return await original(request)
            return await original(request)
        return handler

    @property
    def capacity_lane(self):
        explicit = getattr(self.endpoint, 'capacity_lane', None)
        if explicit:
            return explicit
        if any((method, self.path) in NAVIGATION_ROUTES for method in self.methods):
            return 'navigation'
        if any((method, self.path) in FOREGROUND_ROUTES for method in self.methods):
            return 'foreground'
        if any((method, self.path) in INTERACTIVE_ROUTES for method in self.methods):
            return 'auth_interactive'
        if self.path == '/ingest':
            return 'ingest'
        if self.has_form:
            return 'form'
        return None

    async def handle(self, scope, receive, send):
        if self.methods and scope['method'] not in self.methods:
            return await super().handle(scope, receive, send)
        lane = self.capacity_lane
        if lane is None:
            return await super().handle(scope, receive, send)
        request = Request(scope, receive, send)
        manager = request.app.state.capacity
        importing = self.path == '/settings/import/data'
        avatar_upload = self.path == '/settings/account/avatar'
        response_started = False
        body_receipt_busy = False
        started_send = None
        auth_stack = AsyncExitStack()

        async def release_auth():
            await auth_stack.aclose()
            scope.setdefault('state', {}).pop('_capacity_release_auth', None)

        async def bounded_send(message):
            nonlocal response_started, started_send
            # Framework form parsing converts receipt exceptions to HTTP 400.
            # Restore admission expiry before any replacement response starts.
            if body_receipt_busy and not response_started:
                raise CapacityBusy('body deadline expired')
            if started_send is None:
                await release_auth()
                started_send = time.monotonic()
            if message['type'] == 'http.response.start':
                response_started = True
            remaining = capacity_setting(manager, "capacity_response_timeout_s") - (time.monotonic() - started_send)
            if remaining <= 0:
                raise TimeoutError('response deadline expired')
            async with asyncio.timeout(remaining):
                await send(message)

        async def invoke(body_cap=None, body_timeout=None):
            started_body = time.monotonic()
            total = 0

            async def bounded_receive():
                nonlocal total, body_receipt_busy
                remaining = body_timeout - (time.monotonic() - started_body)
                if remaining <= 0:
                    body_receipt_busy = True
                    raise CapacityBusy('body deadline expired')
                try:
                    async with asyncio.timeout(remaining):
                        message = await receive()
                except TimeoutError:
                    body_receipt_busy = True
                    raise CapacityBusy('body deadline expired') from None
                if message['type'] == 'http.request':
                    total += len(message.get('body', b''))
                    if body_cap is not None and total > body_cap:
                        if avatar_upload:
                            from app.uploads import UploadEnvelopeTooLarge
                            raise UploadEnvelopeTooLarge()
                        raise HTTPException(status_code=413, detail='Request body exceeds the configured limit')
                return message

            active_receive = bounded_receive if body_timeout is not None else receive
            if lane in ('auth_interactive', 'form') and scope['method'] == 'POST':
                # Buffer only the bounded tiny form, so framework multipart cleanup
                # never receives a timeout/overflow while it owns partial spools.
                body = bytearray()
                disconnected = False
                while True:
                    message = await active_receive()
                    if message['type'] == 'http.disconnect':
                        disconnected = True
                        break
                    body.extend(message.get('body', b''))
                    if not message.get('more_body', False):
                        break
                replayed = False
                async def replay():
                    nonlocal replayed
                    if replayed:
                        return await receive()
                    replayed = True
                    if disconnected:
                        return {'type': 'http.disconnect'}
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                active_receive = replay
                if lane == 'form':
                    await release_auth()
            await super(AdmissionRoute, self).handle(scope, active_receive, bounded_send)

        try:
            if lane == 'ingest':
                from app.ingest import authenticate_request, preflight_authentication_request
                preflight_response = preflight_authentication_request(request)
                if preflight_response is not None:
                    await preflight_response(scope, receive, send)
                    return
                async with manager.operation('auth_ingest'):
                    credential = await authenticate_request(request, preflight=False)
                if isinstance(credential, Response):
                    return await credential(scope, receive, send)
                scope.setdefault('state', {})['_capacity_ingest_credential'] = credential
                async with manager.operation('ingest', principal=credential.account):
                    # Ingest's own reader retains poison-body acknowledgement semantics.
                    await invoke(body_timeout=capacity_setting(manager, "capacity_ingest_body_timeout_s"))
            elif lane in ('auth_interactive', 'form'):
                if lane == 'auth_interactive':
                    await auth_stack.enter_async_context(manager.operation('auth_interactive'))
                    scope.setdefault('state', {})['_capacity_release_auth'] = release_auth
                dependency = self.protected_dependency()
                if dependency is not None:
                    user = await _dependency(request, dependency)
                    from app.auth import require_legacy_establishment
                    if dependency is not require_legacy_establishment:
                        scope.setdefault('state', {})['_capacity_authenticated_user'] = user
                if lane == 'form':
                    await auth_stack.enter_async_context(manager.operation('auth_interactive'))
                    scope.setdefault('state', {})['_capacity_release_auth'] = release_auth
                await invoke(capacity_setting(manager, "capacity_auth_form_max_bytes"),
                             capacity_setting(manager, "capacity_auth_body_timeout_s"))
            else:
                from app.auth import require_import_user, require_user
                dependency = require_import_user if importing else (self.protected_dependency() or require_user)
                user = await _dependency(request, dependency)
                if isinstance(user, Response):
                    return await user(scope, receive, send)
                # Cache only the completed enabled/version/tab-checked binding.
                scope.setdefault('state', {})['_capacity_authenticated_user'] = user
                principal = request.state.principal
                async with manager.operation(lane, principal=principal):
                    async with external_account_work(request.app.state.control_pool, principal.account_id):
                        if importing or avatar_upload:
                            cfg = request.app.state.config
                            file_cap = cfg.account_avatar_max_bytes if avatar_upload else cfg.portable_import_max_bytes
                            await invoke(file_cap + capacity_setting(manager, "capacity_multipart_overhead_bytes"),
                                         capacity_setting(manager, "capacity_import_body_timeout_s"))
                        else:
                            await invoke(body_timeout=capacity_setting(manager, "capacity_import_body_timeout_s")
                                         if self.has_form else None)
        except (CapacityBusy, TimeoutError):
            if response_started:
                raise
            if getattr(request.state, '_capacity_mutation_committed', False):
                response = Response(status_code=204, headers={'HX-Refresh': 'true'})
                await response(scope, receive, send)
            else:
                await busy_response(request, importing=importing)(scope, receive, send)

        finally:
            await release_auth()
