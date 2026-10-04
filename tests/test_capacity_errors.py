"""Routine admission errors use the application's retryable busy contract."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request

from app.account_context import AccountPool, AccountPrincipal
from app.auth import require_user
from app.main import create_app
from tests.auth_db_fixtures import auth_config

pytestmark = pytest.mark.capacity_contract


class NoBorrowPool:
    def connection(self, **kwargs):
        raise AssertionError("a saturated lane must reject before borrowing")


@pytest.mark.parametrize("endpoint,accept", [("/healthz", "application/json"), ("/settings", "text/html")])
def test_routine_pressure_is_retryable_before_any_connection(endpoint, accept):
    async def scenario():
        cfg = replace(auth_config("postgresql://unused"), capacity_identity_pending=0,
                      capacity_routine_pending=0)
        app = create_app(cfg)
        manager = app.state.capacity
        app.state.control_pool = manager.manage_pool(NoBorrowPool(), "control")
        app.state.runtime_pool = manager.manage_pool(NoBorrowPool(), "runtime")

        async def account(request: Request):
            principal = AccountPrincipal(3, True, 1)
            request.state.principal = principal
            request.state.account_pool = AccountPool(app.state.runtime_pool, principal)
            request.state.config = cfg
            request.state.account_settings = SimpleNamespace()
            return {"id": 3, "is_admin": True}

        app.dependency_overrides[require_user] = account
        ready = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def hold(index):
            lane = "identity" if endpoint == "/healthz" else "routine"
            principal = None if lane == "identity" else AccountPrincipal(index + 1, True, 1)
            async with manager.operation(lane, principal):
                ready[index].set()
                await release.wait()

        holders = [asyncio.create_task(hold(index)) for index in range(2)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 2)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
                response = await client.get(endpoint, headers={"accept": accept})
            assert response.status_code == 503
            assert response.headers["retry-after"] == "1"
            if accept == "application/json":
                assert response.json()["error"] == "capacity_busy"
            else:
                assert "The server is busy. Please try again." in response.text
        finally:
            release.set()
            await asyncio.gather(*holders)
            await manager.shutdown()

    asyncio.run(scenario())
