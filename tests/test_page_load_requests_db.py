"""Requests a signed-in page sends together do not refuse each other.

The header avatar loads beside each page's first request, so the pair runs
through the real application and admission manager, started together.
"""
import asyncio
from contextlib import asynccontextmanager
import os
import re

import httpx
import pytest

from app.account_context import control_connection
from app.accounts import set_account_avatar
from app.db import make_pool
from app.main import create_app
from auth_db_fixtures import auth_config
from avatar_image_fixtures import PNG_UPLOAD_BYTES
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [pytest.mark.capacity_contract,
              pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")]

ROUNDS = 5


@asynccontextmanager
async def signed_in_with_avatar():
    raw = make_pool(TEST_DB)
    await raw.open(wait=True)
    try:
        await reset_db(raw)
        config = auth_config(TEST_DB, dev_no_auth=False, initial_admin_signup=True,
                             raw_message_retention_days=0)
        app = create_app(config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://test") as client:
                page = await client.get("/signup")
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                response = await client.post("/signup", data={
                    "email": "page-load@example.invalid", "password": "page load test password",
                    "password_confirm": "page load test password", "csrf_token": csrf,
                    "display_timezone": "UTC"})
                assert response.status_code == 303
                async with raw.connection() as conn:
                    owner = (await (await conn.execute(
                        "SELECT id FROM accounts WHERE email=%s",
                        ("page-load@example.invalid",))).fetchone())[0]
                async with control_connection(app.state.control_pool) as conn:
                    await set_account_avatar(conn, owner, PNG_UPLOAD_BYTES, "image/png")
                yield client
        assert not app.state.capacity.snapshot()["leases"]
    finally:
        await raw.close()


async def avatar(client):
    response = await client.get("/account/avatar", params={"v": "1"},
                                headers={"Accept": "image/avif,image/webp,*/*"})
    if response.status_code == 200:
        assert response.content == PNG_UPLOAD_BYTES
    return response.status_code


async def places_heat(client):
    response = await client.get("/places/heat", params={"from": "2026-01-01", "to": "2026-12-31"},
                                headers={"Accept": "application/json"})
    return response.status_code


async def trips_list(client):
    response = await client.get("/trips/list", headers={"HX-Request": "true", "Accept": "text/html"})
    return response.status_code


REQUESTS = {"avatar": avatar, "places_heat": places_heat, "trips_list": trips_list}


@pytest.mark.parametrize("first, second", [
    ("avatar", "places_heat"), ("places_heat", "avatar"),
    ("avatar", "trips_list"), ("trips_list", "avatar"),
])
def test_the_avatar_and_a_page_request_both_succeed_when_sent_together(first, second):
    async def run():
        async with signed_in_with_avatar() as client:
            for _ in range(ROUNDS):
                codes = await asyncio.gather(REQUESTS[first](client), REQUESTS[second](client))
                assert codes == [200, 200], (first, second, codes)

    asyncio.run(run())
