"""API routes under admission: bearer-only identity and one bearer check
per request."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.responses import Response

import app.api_v1 as api_v1
import app.capacity_routes as capacity_routes
from app.account_context import AccountPrincipal
from app.api_errors import ERRORS, register_api_errors
from app.api_v1 import WebSessionMiddleware, require_api_user
from app.auth import require_user
from app.capacity import AdmissionManager
from app.capacity_routes import AdmissionRoute, capacity_policy
from app.ingest import FailedAuthLimiter

TOKEN = "A" * 43


def _envelope(code):
    return {"error": {"code": code, "message": ERRORS[code][1], "fields": None}}


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://testserver")


def _run(check):
    asyncio.run(check())


def test_protected_dependency_names_the_bearer_dependency():
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/thing")
    async def thing(api_user=Depends(require_api_user)):
        return {}

    @router.get("/web/thing")
    async def web(user=Depends(require_user)):
        return {}

    routes = {route.path: route for route in router.routes}
    assert routes["/api/v1/thing"].protected_dependency() is require_api_user
    assert routes["/web/thing"].protected_dependency() is require_user


def _lane_app(monkeypatch, checks):
    app = FastAPI()
    app.add_middleware(WebSessionMiddleware, secret_key="disposable-secret", https_only=False)
    app.state.capacity = AdmissionManager(SimpleNamespace())
    app.state.api_auth_limiter = FailedAuthLimiter(50, 900)
    app.state.control_pool = object()

    @asynccontextmanager
    async def no_connection(*args, **kwargs):
        yield object()

    async def check_access_token(conn, token):
        checks.append(token)
        principal = SimpleNamespace(account_id=5, auth_version=1, grant_id="grant")
        return SimpleNamespace(principal=principal, known=True)

    async def get_account(conn, account):
        return {"id": account, "is_enabled": True, "auth_version": 1}

    async def bind_account(request, account):
        request.state.principal = AccountPrincipal(account["id"], True, 1)
        return {"id": account["id"]}

    monkeypatch.setattr(api_v1, "control_connection", no_connection)
    monkeypatch.setattr(api_v1, "check_access_token", check_access_token)
    monkeypatch.setattr(api_v1, "get_account", get_account)
    monkeypatch.setattr(api_v1, "_bind_account", bind_account)
    monkeypatch.setattr(capacity_routes, "external_account_work", no_connection)

    router = APIRouter(route_class=AdmissionRoute)

    @router.post("/test/session")
    async def session(request: Request):
        request.session.update(account_id=5, auth_version=1, csrf="csrf")
        return Response(status_code=204)

    @router.get("/api/v1/laned")
    @capacity_policy("foreground")
    async def laned(api_user=Depends(require_api_user)):
        return {"account": api_user.account["id"]}

    @router.get("/api/v1/laned/bare")
    @capacity_policy("foreground")
    async def bare():
        return {}

    register_api_errors(app)
    app.include_router(router)
    return app


def test_a_laned_api_route_authenticates_once_by_bearer_never_by_cookie(monkeypatch):
    checks = []
    app = _lane_app(monkeypatch, checks)

    async def check():
        async with _client(app) as client:
            assert (await client.post("/test/session")).status_code == 204
            assert client.cookies.get("session")
            for path in ("/api/v1/laned", "/api/v1/laned/bare"):
                response = await client.get(path)
                assert response.status_code == 401, path
                assert response.json() == _envelope("invalid_token")
            assert checks == []
            response = await client.get("/api/v1/laned", headers={"Authorization": f"Bearer {TOKEN}"})
            assert response.status_code == 200
            assert response.json() == {"account": 5}
            assert checks == [TOKEN]
            assert not any(app.state.capacity._active.values())
    _run(check)
