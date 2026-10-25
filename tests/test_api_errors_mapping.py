"""Framework 405, 413 and 500 errors use the envelope on API paths, while
web paths keep their framework responses."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI, Form

from app.api_errors import ERRORS, register_api_errors
from app.capacity import AdmissionManager
from app.capacity_routes import AdmissionRoute, capacity_policy


def _envelope(code):
    return {"error": {"code": code, "message": ERRORS[code][1], "fields": None}}


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://testserver")


def _run(check):
    asyncio.run(check())


def _error_app():
    app = FastAPI()
    app.state.capacity = AdmissionManager(SimpleNamespace(capacity_auth_form_max_bytes=16))
    router = APIRouter(route_class=AdmissionRoute)
    for prefix in ("/api/v1", "/web"):
        @router.get(f"{prefix}/thing")
        async def thing():
            return {}

        @router.get(f"{prefix}/boom")
        async def boom():
            raise RuntimeError("unhandled")

        @router.post(f"{prefix}/form")
        @capacity_policy("auth_interactive")
        async def form(value: str = Form(...)):
            return {}

    register_api_errors(app)
    app.include_router(router)
    return app


def test_framework_errors_use_the_envelope_only_under_api():
    app = _error_app()
    form = {"content-type": "application/x-www-form-urlencoded"}

    async def check():
        async with _client(app) as client:
            response = await client.post("/api/v1/thing")
            assert response.status_code == 405
            assert response.json() == _envelope("method_not_allowed")
            assert response.headers["allow"] == "GET"
            response = await client.post("/api/v1/form", content=b"value=" + b"x" * 64, headers=form)
            assert response.status_code == 413
            assert response.json() == _envelope("payload_too_large")
            response = await client.get("/api/v1/boom")
            assert response.status_code == 500
            assert response.json() == _envelope("internal_error")
            assert response.headers["cache-control"] == "no-store, private"
            response = await client.get("/api/v1/missing")
            assert response.json() == _envelope("not_found")

            response = await client.post("/web/thing")
            assert response.status_code == 405
            assert response.json() == {"detail": "Method Not Allowed"}
            assert response.headers["allow"] == "GET"
            response = await client.post("/web/form", content=b"value=" + b"x" * 64, headers=form)
            assert response.status_code == 413
            assert response.json() == {"detail": "Request body exceeds the configured limit"}
            response = await client.get("/web/boom")
            assert response.status_code == 500
            assert response.headers["content-type"].startswith("text/plain")
            assert response.text == "Internal Server Error"
            assert "cache-control" not in response.headers
    _run(check)


def test_web_500_matches_the_framework_default():
    app = FastAPI()

    @app.get("/web/boom")
    async def boom():
        raise RuntimeError("unhandled")

    async def check():
        async with _client(app) as client:
            default = await client.get("/web/boom")
        register_api_errors(app)
        app.middleware_stack = None
        async with _client(app) as client:
            mapped = await client.get("/web/boom")
        assert (mapped.status_code, mapped.text, mapped.headers["content-type"]) == (
            default.status_code, default.text, default.headers["content-type"])
    _run(check)
