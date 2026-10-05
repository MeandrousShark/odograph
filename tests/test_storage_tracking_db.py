"""Fresh tracking admission precedes accounting and revocation row locks."""
import asyncio
import os
import time

import psycopg
import pytest

from app.db import make_pool
from app.tracking import IngestPrincipal, TrackingStream, admit_ingest, revoke_credential
from conftest import reset_account_db, seed_tracking_device

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


def test_revocation_waits_for_admitted_ingest_before_acquiring_usage_lock():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            owner = pool.principal.account_id
            async with pool.connection() as conn:
                device = await seed_tracking_device(conn)
                await conn.execute(
                    "INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,"
                    "account_id,tracking_device_id,kind) VALUES('lock-test','lock-test','unused',%s,%s,'device')",
                    (owner, device),
                )
            credential = IngestPrincipal(pool.principal, 'lock-test', 1, 'device', device, 1)
            stream = TrackingStream(device, 'phone', 1)
            admitted = asyncio.Event()
            revoke_started = asyncio.Event()
            backend = []

            async def ingest():
                async with pool.connection() as conn:
                    await admit_ingest(conn, credential, stream)
                    admitted.set()
                    await revoke_started.wait()
                    # Confirm the revoker reached a row-lock wait, rather than
                    # relying on scheduler timing to expose the inverted order.
                    deadline = time.monotonic() + 2
                    while True:
                        async with raw.connection() as probe:
                            wait = await (await probe.execute(
                                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (backend[0],)
                            )).fetchone()
                        if wait == ('Lock',):
                            break
                        assert time.monotonic() < deadline
                        await asyncio.sleep(.01)
                    await conn.execute(
                        "INSERT INTO raw_messages(account_id,tracking_device_id,payload) VALUES(%s,%s,'{}')",
                        (owner, device),
                    )

            async def revoke():
                await admitted.wait()
                async with pool.connection() as conn:
                    await conn.execute("SET LOCAL lock_timeout='3s'")
                    backend.append((await (await conn.execute('SELECT pg_backend_pid()')).fetchone())[0])
                    revoke_started.set()
                    await revoke_credential(conn, credential.public_id)

            await asyncio.wait_for(asyncio.gather(ingest(), revoke()), 5)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with pool.connection() as conn:
                    await admit_ingest(conn, credential, stream)
            async with raw.connection() as conn:
                assert (await (await conn.execute('SELECT count(*) FROM raw_messages')).fetchone())[0] == 1
                assert (await (await conn.execute('SELECT storage_usage_consistent()')).fetchone())[0]
        finally:
            await raw.close()
    asyncio.run(scenario())
