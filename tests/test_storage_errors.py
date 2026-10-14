import asyncio

import httpx
import pytest
from fastapi import FastAPI
from psycopg import errors

from app.storage import is_account_grant_refusal
from app.storage_errors import register_storage_errors


def test_quota_failure_returns_retryable_refusal_without_database_details():
    app = FastAPI()
    register_storage_errors(app)

    @app.post("/save")
    async def save():
        raise errors.RaiseException("storage capacity exceeded: account")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/save")

    response = asyncio.run(run())
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert "This change wasn't saved" in response.json()["detail"]
    assert "existing data is still available" in response.json()["detail"]
    assert "storage capacity exceeded:" not in response.text


@pytest.mark.parametrize("message", ["some other database contract failure",
                                      "unrelated payload contains storage capacity exceeded:"])
def test_unrelated_database_failure_is_not_misrepresented_as_quota_pressure(message):
    app = FastAPI()
    register_storage_errors(app)

    @app.post("/save")
    async def save():
        raise errors.RaiseException(message)

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/save")

    response = asyncio.run(run())
    assert response.status_code == 500
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("exc, expected", [
    (errors.RaiseException("storage capacity exceeded: no funded account grant available"), True),
    (errors.RaiseException("storage capacity exceeded: account allowance"), False),
    (errors.RaiseException("invitation unavailable"), False),
    (errors.UniqueViolation("storage capacity exceeded: no funded account grant available"), False),
])
def test_account_grant_refusal_is_recognized_only_for_the_new_account_trigger(exc, expected):
    assert is_account_grant_refusal(exc) is expected
