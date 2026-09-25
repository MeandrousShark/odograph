"""A challenge URL keeps bearer material out of referrers and caches."""
from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from starlette.responses import HTMLResponse

from app.main import SecurityHeadersMiddleware


def test_confirmation_headers_survive_outer_security_middleware():
    app = FastAPI()
    app.add_middleware(
        SecurityHeadersMiddleware, tile_host="https://tiles.example", hsts_max_age=0,
    )

    @app.get("/settings/account/email/confirm")
    async def confirmation():
        return HTMLResponse("<h1>Confirm email</h1>", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        })

    async def check():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/settings/account/email/confirm")
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store, private"
            assert response.headers["Referrer-Policy"] == "no-referrer"

    asyncio.run(check())
