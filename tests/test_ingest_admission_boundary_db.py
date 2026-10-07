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
import hashlib
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
from app.tracking import create_device, rotate_credential, revoke_credential
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

# A trigger, not a REVOKE: a REVOKE would break the validated role contract,
# and the point of the test is the SQLSTATE the route sees, not how it was
# produced. The privileged test pool installs and removes it.
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
    app.state.control_pool = pool.control_pool
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


def _post_body(client, body: bytes, auth_header=AUTH_HEADER):
    return client.post("/ingest", headers=auth_header, content=body)


def _device_auth(credential):
    basic = f"{credential.username}:{credential.secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(basic).decode()}


async def _receipt_count(pool):
    async with pool.admin_pool.connection() as conn:
        return (await (await conn.execute("SELECT count(*) FROM raw_replay_receipts")).fetchone())[0]


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
        async with pool.admin_pool.connection() as conn:
            await conn.execute(REFUSE_POINT_WRITES)
        try:
            response = await _post(client)
        finally:
            async with pool.admin_pool.connection() as conn:
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


def test_exact_replay_and_changed_canonical_payload():
    async def run(pool, client):
        wakes = []
        client._transport.app.state.detector_scheduler.poke = lambda: wakes.append(None)
        payload = {
            "_type": "location", "tid": "aa", "lat": 47.6, "lon": -122.3,
            "tst": int(time.time()) - 60, "extra": 1,
        }
        body = json.dumps(payload).encode()
        assert (await _post_body(client, body)).status_code == 200
        assert (await _post_body(client, body)).status_code == 200
        assert await _stored(pool) == (1, 1)
        assert await _receipt_count(pool) == 1

        # jsonb can consider these numbers equal while preserving their
        # distinct canonical text representations.
        payload["extra"] = 1.0
        assert (await _post_body(client, json.dumps(payload).encode())).status_code == 200
        assert await _stored(pool) == (2, 1)
        assert await _receipt_count(pool) == 2
        assert len(wakes) == 3

    asyncio.run(_scenario(run))


def test_raw_only_replay_uses_legacy_credential_without_provisioning_device():
    async def run(pool, client):
        waypoint = json.dumps({"_type": "waypoint", "tid": "aa", "name": "home"}).encode()
        invalid = json.dumps({"_type": "location", "tid": "aa", "lat": "bad"}).encode()
        for body in (waypoint, invalid):
            assert (await _post_body(client, body)).status_code == 200
            assert (await _post_body(client, body)).status_code == 200
        assert await _stored(pool) == (2, 0)
        assert await _receipt_count(pool) == 2
        async with pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM tracking_devices")).fetchone())[0] == 0

    asyncio.run(_scenario(run))


def test_unresolved_legacy_replay_namespace_changes_with_credential():
    async def run(pool, client):
        body = json.dumps({"_type": "waypoint", "name": "home"}).encode()
        assert (await _post_body(client, body)).status_code == 200
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO ingest_credentials "
                "(public_id,basic_username,secret_hash,account_id,kind) "
                "VALUES ('test-legacy-next','other',%s,%s,'legacy')",
                (hash_password("testpw"), pool.principal.account_id),
            )
        next_auth = {"Authorization": "Basic " + base64.b64encode(b"other:testpw").decode()}
        assert (await _post_body(client, body, next_auth)).status_code == 200
        assert await _stored(pool) == (2, 0)
        assert await _receipt_count(pool) == 2

    asyncio.run(_scenario(run))


def test_device_replay_survives_rotation_but_revocation_blocks_it():
    async def run(pool, client):
        async with pool.connection() as conn:
            issued = await create_device(conn, "phone")
        body = json.dumps({
            "_type": "location", "tid": "ph", "lat": 47.6, "lon": -122.3,
            "tst": int(time.time()) - 60,
        }).encode()
        assert (await _post_body(client, body, _device_auth(issued))).status_code == 200
        async with pool.connection() as conn:
            rotated = await rotate_credential(conn, issued.public_id)
        assert (await _post_body(client, body, _device_auth(issued))).status_code == 401
        assert (await _post_body(client, body, _device_auth(rotated))).status_code == 200
        assert await _stored(pool) == (1, 1)
        assert await _receipt_count(pool) == 1
        async with pool.connection() as conn:
            await revoke_credential(conn, issued.public_id)
        assert (await _post_body(client, body, _device_auth(rotated))).status_code == 401

    asyncio.run(_scenario(run))


def test_replay_at_raw_ceiling_succeeds_and_new_raw_is_retryable():
    async def run(pool, client):
        first = json.dumps({"_type": "waypoint", "name": "first"}).encode()
        changed = json.dumps({"_type": "waypoint", "name": "changed"}).encode()
        assert (await _post_body(client, first)).status_code == 200
        async with pool.admin_pool.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET raw_limit_bytes = "
                "(SELECT raw_bytes FROM account_usage WHERE account_id = %s) "
                "WHERE account_id = %s",
                (pool.principal.account_id, pool.principal.account_id),
            )
        assert (await _post_body(client, first)).status_code == 200
        response = await _post_body(client, changed)
        assert response.status_code == 503
        assert int(response.headers["Retry-After"]) > 0
        assert await _stored(pool) == (1, 0)
        assert await _receipt_count(pool) == 1

    asyncio.run(_scenario(run))


def test_rejected_first_message_leaves_no_raw_or_receipt_for_retry():
    async def run(pool, client):
        async with pool.admin_pool.connection() as conn:
            await conn.execute(
                "UPDATE storage_grants SET raw_limit_bytes = 1 WHERE account_id = %s",
                (pool.principal.account_id,),
            )
        body = json.dumps({"_type": "waypoint", "name": "new"}).encode()
        for _ in range(2):
            response = await _post_body(client, body)
            assert response.status_code == 503
            assert int(response.headers["Retry-After"]) > 0
        assert await _stored(pool) == (0, 0)
        assert await _receipt_count(pool) == 0

    asyncio.run(_scenario(run))


def test_concurrent_identical_messages_share_one_retained_raw_row():
    async def run(pool, client):
        body = json.dumps({"_type": "waypoint", "name": "home"}).encode()
        responses = await asyncio.gather(*(_post_body(client, body) for _ in range(2)))
        assert any(response.status_code == 200 for response in responses)
        for response in responses:
            if response.status_code == 503:
                assert int(response.headers["Retry-After"]) > 0
                assert (await _post_body(client, body)).status_code == 200
            else:
                assert response.status_code == 200
        assert await _stored(pool) == (1, 0)
        assert await _receipt_count(pool) == 1

    asyncio.run(_scenario(run))


def test_digest_candidate_requires_full_canonical_match_and_retention_removes_receipt():
    async def run(pool, client):
        first = json.dumps({"_type": "waypoint", "name": "first"}).encode()
        second = json.dumps({"_type": "waypoint", "name": "second"}).encode()
        assert (await _post_body(client, first)).status_code == 200
        async with pool.admin_pool.connection() as conn:
            canonical = (await (await conn.execute(
                "SELECT %s::jsonb::text", (second.decode(),),
            )).fetchone())[0]
            await conn.execute(
                "UPDATE raw_replay_receipts SET payload_sha256 = %s",
                (hashlib.sha256(canonical.encode()).digest(),),
            )
        assert (await _post_body(client, second)).status_code == 200
        assert await _stored(pool) == (2, 0)
        assert await _receipt_count(pool) == 2

        async with pool.admin_pool.connection() as conn:
            await conn.execute("DELETE FROM raw_messages")
        assert await _receipt_count(pool) == 0
        assert (await _post_body(client, first)).status_code == 200
        assert await _stored(pool) == (1, 0)

    asyncio.run(_scenario(run))
