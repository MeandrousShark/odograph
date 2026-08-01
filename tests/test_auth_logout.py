"""Tests for POST-only /logout (Batch B item 4): a bare GET no longer logs
anyone out (CSRF-able), and the CSRF-protected POST behaves like the old
GET did. No DB needed -- logout only touches the session.

Session state is seeded/inspected through two test-only routes bolted onto
the bare app below, the same "smallest ASGI app that exercises real
dependency injection" approach tests/test_dashboard_db.py uses for its
unauthenticated-redirect case, since `require_csrf` (a FastAPI dependency)
only actually runs when dispatched through the router.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from starlette.middleware.sessions import SessionMiddleware

from app.auth import make_router


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )

    @app.post("/test/seed-session")
    async def seed(request: Request):
        request.session["user"] = {"sub": "u1"}
        request.session["csrf"] = "test-csrf-token"
        return Response(status_code=204)

    @app.get("/test/session")
    async def read_session(request: Request):
        return {"has_user": "user" in request.session}

    app.include_router(make_router())
    return app


async def _client():
    transport = httpx.ASGITransport(app=_bare_app())
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    )


def test_get_logout_is_no_longer_allowed():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.get("/logout")
            assert response.status_code == 405

            still_logged_in = await client.get("/test/session")
            assert still_logged_in.json() == {"has_user": True}

    asyncio.run(run())


def test_post_logout_without_csrf_token_is_rejected():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.post("/logout")
            assert response.status_code == 403

            still_logged_in = await client.get("/test/session")
            assert still_logged_in.json() == {"has_user": True}

    asyncio.run(run())


def test_post_logout_with_valid_csrf_clears_session_and_redirects():
    async def run():
        async with await _client() as client:
            await client.post("/test/seed-session")
            response = await client.post(
                "/logout", headers={"X-CSRF-Token": "test-csrf-token"}
            )
            assert response.status_code == 204
            assert response.headers["HX-Redirect"] == "/login"

            logged_out = await client.get("/test/session")
            assert logged_out.json() == {"has_user": False}

    asyncio.run(run())
