"""hmac.compare_digest raises TypeError on non-ASCII str operands, which
would otherwise turn a wrong/non-ASCII Basic-auth credential into a 500
(app/ingest.py's _check_basic_auth). No DB needed -- the auth check runs
before anything in the handler touches the pool.
"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.ingest import FailedAuthLimiter, make_router


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.state.pool = None
    app.state.config = SimpleNamespace(
        ingest_username="owntracks", ingest_password="testpw", ingest_max_body_bytes=1_000_000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(1000, 60.0)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda: None)
    app.include_router(make_router())
    return app


def test_ingest_rejects_non_ascii_basic_auth_credentials_with_401_not_500():
    header = {
        "Authorization": "Basic " + base64.b64encode("öwntracks:tëstpw".encode("utf-8")).decode()
    }

    async def run():
        transport = httpx.ASGITransport(app=_bare_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/ingest", headers=header, content=b"{}")

    response = asyncio.run(run())
    assert response.status_code == 401
