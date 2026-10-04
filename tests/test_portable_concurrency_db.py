"""Import clean-target admission serializes with ordinary personal writes."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from psycopg.errors import LockNotAvailable

from app.account_context import RUNTIME_ROLE, account_id
from app.portable import importer
from app.ui import make_router
from tests.test_ownership_integration_db import _ObservedPool, _bundle, _fixture, _wait_blocked

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires disposable PostGIS",
)


async def _pid(conn):
    pid, role = await (await conn.execute("SELECT pg_backend_pid(),current_user")).fetchone()
    assert role == RUNTIME_ROLE
    return pid


async def _manual_trip(conn):
    cur = await conn.execute(
        "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,notes) "
        "VALUES (%s,'manual','manual','2026-08-02T10:00:00Z','2026-08-02T11:00:00Z',"
        "1000,'Concurrent manual trip') RETURNING id", (account_id(conn),),
    )
    return (await cur.fetchone())[0]


async def _finish_tasks(*tasks):
    pending = [task for task in tasks if task is not None]
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def test_import_is_busy_while_inflight_manual_insert_holds_account_barrier(monkeypatch):
    async def scenario():
        async with _fixture() as (owner, _pools, _state, account, _other):
            checked, started = asyncio.Event(), asyncio.Event()
            original_check = importer._check_clean_target
            task = None

            async def check(conn):
                checked.set()
                return await original_check(conn)

            async def importing():
                async with account.connection() as conn:
                    await _pid(conn)
                    started.set()
                    return await importer._apply_import(conn, _bundle())

            monkeypatch.setattr(importer, "_check_clean_target", check)
            try:
                async with account.connection() as conn:
                    await _pid(conn)
                    trip_id = await _manual_trip(conn)
                    task = asyncio.create_task(importing())
                    await asyncio.wait_for(started.wait(), 5)
                    with pytest.raises(LockNotAvailable):
                        await asyncio.wait_for(task, 5)
                    assert not checked.is_set()
                # The failed lock upgrade leaves the committed manual trip intact.
                async with account.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT id,imported,notes FROM trips WHERE account_id=%s",
                        (account_id(conn),),
                    )).fetchall() == [(trip_id, False, "Concurrent manual trip")]
                    assert await (await conn.execute(
                        "SELECT name FROM vehicles WHERE account_id=%s", (account_id(conn),),
                    )).fetchall() == [("My Car",)]
            finally:
                await _finish_tasks(task)

    asyncio.run(scenario())


def test_manual_insert_after_clean_check_waits_until_import_commit(monkeypatch):
    async def scenario():
        async with _fixture() as (owner, _pools, _state, account, _other):
            checked, allow_import, writer_started = (asyncio.Event() for _ in range(3))
            original_check = importer._check_clean_target
            import_task = writer_task = None

            async def check(conn):
                conflicts = await original_check(conn)
                assert conflicts == {}
                await _pid(conn)
                checked.set()
                await allow_import.wait()
                return conflicts

            async def importing():
                async with account.connection() as conn:
                    return await importer._apply_import(conn, _bundle())

            async def writing():
                writer_started.set()
                async with account.connection() as conn:
                    await _pid(conn)
                    return await _manual_trip(conn)

            monkeypatch.setattr(importer, "_check_clean_target", check)
            try:
                import_task = asyncio.create_task(importing())
                await asyncio.wait_for(checked.wait(), 5)
                writer_task = asyncio.create_task(writing())
                await asyncio.wait_for(writer_started.wait(), 5)
                await asyncio.sleep(0.05)
                assert not writer_task.done()
                # Same-account admission waits until import's exclusive lock ends.
                allow_import.set()
                counts = await asyncio.wait_for(import_task, 5)
                trip_id = await asyncio.wait_for(writer_task, 5)
                assert counts["trips"] == 1
                async with account.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT source::text,imported,notes FROM trips WHERE account_id=%s ORDER BY imported",
                        (account_id(conn),),
                    )).fetchall() == [
                        ("manual", False, "Concurrent manual trip"),
                        ("detected", True, "Imported private note"),
                    ]
                    assert await (await conn.execute(
                        "SELECT id FROM trips WHERE account_id=%s AND NOT imported", (account_id(conn),),
                    )).fetchone() == (trip_id,)
            finally:
                allow_import.set()
                await _finish_tasks(import_task, writer_task)

    asyncio.run(scenario())


def test_import_upgrade_is_busy_if_place_create_waits_for_global_lock(monkeypatch):
    async def scenario():
        async with _fixture() as (owner, _pools, _state, account, _other):
            global_locked, allow_account_upgrade = asyncio.Event(), asyncio.Event()
            original_version = importer._fetch_schema_version
            import_pid = None
            import_task = place_task = None

            async def version(conn):
                nonlocal import_pid
                result = await original_version(conn)
                import_pid = await _pid(conn)
                # Import holds the global detector key before the account upgrade.
                global_locked.set()
                await allow_account_upgrade.wait()
                return result

            async def importing():
                async with account.connection() as conn:
                    return await importer._apply_import(conn, _bundle())

            endpoint = next(
                route.endpoint for route in make_router().routes
                if route.path == "/places" and "POST" in route.methods
            )
            observed = _ObservedPool(account)
            request = SimpleNamespace(state=SimpleNamespace(account_pool=observed), headers={})
            monkeypatch.setattr(importer, "_fetch_schema_version", version)
            try:
                import_task = asyncio.create_task(importing())
                await asyncio.wait_for(global_locked.wait(), 5)
                place_task = asyncio.create_task(endpoint(
                    request, name="Concurrent place", kind="work", lat=40, lon=-70,
                    radius_m=150, user={},
                ))
                await asyncio.wait_for(observed.started.wait(), 5)
                await _wait_blocked(owner, observed.pid, import_pid, place_task)
                async with owner.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT mode FROM pg_locks WHERE pid=%s AND relation='public.places'::regclass",
                        (observed.pid,),
                    )).fetchall() == []
                    assert await (await conn.execute(
                        "SELECT count(*) FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted",
                        (observed.pid,),
                    )).fetchone() == (1,)
                # NOWAIT fails the upgrade, releasing the detector lock and
                # allowing the already admitted place create to finish.
                allow_account_upgrade.set()
                with pytest.raises(LockNotAvailable):
                    await asyncio.wait_for(import_task, 5)
                assert (await asyncio.wait_for(place_task, 5)).status_code == 204
                async with account.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT name FROM places WHERE account_id=%s ORDER BY name", (account_id(conn),),
                    )).fetchall() == [("Concurrent place",)]
            finally:
                allow_account_upgrade.set()
                await _finish_tasks(import_task, place_task)

    asyncio.run(scenario())
