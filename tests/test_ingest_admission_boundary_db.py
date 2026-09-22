"""DB-backed tests for /ingest's admission boundary.

`app/ingest.py` answers 401 when the *credential* is refused and 5xx when the
server itself fails, because OwnTracks iOS deletes the payload it is holding
on any 4xx but keeps it queued on 5xx. Both halves are raised as the same
`InsufficientPrivilege` (SQLSTATE 42501) by PostgreSQL: the admission function
raises it for a revoked credential or a retired legacy alias, and a missing
grant or an unsatisfied row-level policy raises it for an ordinary INSERT.

Telling them apart by *where* they were raised is the whole contract, so both
directions are pinned here. Getting it wrong in the storing direction loses a
real location fix to a server-side misconfiguration.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.db import make_pool
from app.ingest import FailedAuthLimiter, make_router
from app.local_auth import hash_password
from conftest import reset_account_db, seed_tracking_device

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
    ),
]

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"owntracks:testpw").decode()
}

# A trigger, not a REVOKE: the disposable fixture connects as the database
# owner, whose privileges cannot be revoked out from under it, and the point
# of the test is the SQLSTATE the route sees, not how it was produced.
REFUSE_POINT_WRITES = """
CREATE FUNCTION public.refuse_point_write() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'points write refused' USING ERRCODE = '42501';
END $$;
CREATE TRIGGER refuse_point_write BEFORE INSERT ON points
FOR EACH ROW EXECUTE FUNCTION public.refuse_point_write();
"""
DROP_REFUSE_POINT_WRITES = """
DROP TRIGGER IF EXISTS refuse_point_write ON points;
DROP FUNCTION IF EXISTS public.refuse_point_write();
"""


def _bare_app(pool) -> FastAPI:
    app = FastAPI()
    app.state.control_pool = pool.runtime_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.config = SimpleNamespace(
        ingest_username="owntracks",
        ingest_password="testpw",
        ingest_max_body_bytes=1_000_000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(1000, 60.0)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda *key: None)
    app.include_router(make_router())
    return app


async def _scenario(coro) -> None:
    raw_pool = make_pool(TEST_DB)
    await raw_pool.open(wait=True)
    try:
        pool = await reset_account_db(raw_pool)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO ingest_credentials (public_id,basic_username,secret_hash,account_id,kind) "
                "VALUES ('test-legacy','owntracks',%s,%s,'legacy')",
                (hash_password("testpw"), pool.principal.account_id),
            )
        transport = httpx.ASGITransport(
            app=_bare_app(pool), raise_app_exceptions=False
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await coro(pool, client)
    finally:
        await raw_pool.close()


async def _stored(pool) -> tuple[int, int]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (SELECT count(*) FROM raw_messages), (SELECT count(*) FROM points)"
        )
        return await cur.fetchone()


def _post(client):
    body = json.dumps({
        "_type": "location", "tid": "aa",
        "lat": 47.6, "lon": -122.3, "tst": int(time.time()) - 60,
    }).encode()
    return client.post("/ingest", headers=AUTH_HEADER, content=body)


def test_refused_admission_answers_401_and_stores_nothing():
    """A retired legacy alias is a refused sender, so 4xx is correct here.

    Conversion disables the alias deliberately, precisely so queued uploads
    under the shared login stop recreating that stream.
    """
    async def run(pool, client):
        async with pool.connection() as conn:
            device = await seed_tracking_device(conn, "aa")
            await conn.execute(
                "INSERT INTO tracking_device_aliases "
                "(account_id,original_label,tracking_device_id,enabled) "
                "VALUES (%s,'aa',%s,false)",
                (pool.principal.account_id, device),
            )
        response = await _post(client)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="ingest"'
        assert await _stored(pool) == (0, 0)

    asyncio.run(_scenario(run))


def test_privilege_error_while_storing_answers_5xx_not_401():
    """An admitted sender must never be told its credential was rejected.

    Without the admission boundary this lands as a 401 and iOS drops the
    queued fix over what is really a server-side grant or policy fault.
    """
    async def run(pool, client):
        async with pool.connection() as conn:
            await conn.execute(REFUSE_POINT_WRITES)
        try:
            response = await _post(client)
        finally:
            async with pool.connection() as conn:
                await conn.execute(DROP_REFUSE_POINT_WRITES)
        assert response.status_code >= 500, response.status_code
        # The whole transaction rolled back, so the raw message the route had
        # already written is gone too -- nothing is half-stored.
        assert await _stored(pool) == (0, 0)

    asyncio.run(_scenario(run))


def test_healthy_write_still_succeeds_and_stores_the_fix():
    """Contrast case, so the two tests above cannot both pass vacuously."""
    async def run(pool, client):
        response = await _post(client)
        assert response.status_code == 200
        assert await _stored(pool) == (1, 1)

    asyncio.run(_scenario(run))
