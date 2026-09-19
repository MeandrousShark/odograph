"""Import clean-target admission serializes with ordinary personal writes."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

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


def test_import_waits_for_inflight_manual_insert_then_refuses_dirty_target(monkeypatch):
    async def scenario():
        async with _fixture() as (owner, _pools, _state, account, _other):
            checked, started = asyncio.Event(), asyncio.Event()
            original_check = importer._check_clean_target
            import_pid = None
            task = None

            async def check(conn):
                checked.set()
                return await original_check(conn)

            async def importing():
                nonlocal import_pid
                async with account.connection() as conn:
                    import_pid = await _pid(conn)
                    started.set()
                    return await importer._apply_import(conn, _bundle())

            monkeypatch.setattr(importer, "_check_clean_target", check)
            try:
                async with account.connection() as conn:
                    writer_pid = await _pid(conn)
                    trip_id = await _manual_trip(conn)
                    task = asyncio.create_task(importing())
                    await asyncio.wait_for(started.wait(), 5)
                    await _wait_blocked(owner, import_pid, writer_pid, task)
                    assert not checked.is_set()
                # The writer's real commit releases RowExclusiveLock. Import's
                # subsequent READ COMMITTED check must observe that new row.
                with pytest.raises(importer.PortableImportError) as refused:
                    await asyncio.wait_for(task, 5)
                assert checked.is_set()
                assert refused.value.error == "target_not_clean"
                assert refused.value.extra["conflicts"]["trips"]["count"] == 1
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
            import_pid = writer_pid = None
            import_task = writer_task = None

            async def check(conn):
                nonlocal import_pid
                conflicts = await original_check(conn)
                assert conflicts == {}
                import_pid = await _pid(conn)
                checked.set()
                await allow_import.wait()
                return conflicts

            async def importing():
                async with account.connection() as conn:
                    return await importer._apply_import(conn, _bundle())

            async def writing():
                nonlocal writer_pid
                async with account.connection() as conn:
                    writer_pid = await _pid(conn)
                    writer_started.set()
                    return await _manual_trip(conn)

            monkeypatch.setattr(importer, "_check_clean_target", check)
            try:
                import_task = asyncio.create_task(importing())
                await asyncio.wait_for(checked.wait(), 5)
                writer_task = asyncio.create_task(writing())
                await asyncio.wait_for(writer_started.wait(), 5)
                await _wait_blocked(owner, writer_pid, import_pid, writer_task)
                # The write fence still permits ordinary restricted reads.
                async with account.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT count(*) FROM trips WHERE account_id=%s", (account_id(conn),),
                    )).fetchone() == (0,)
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


def test_place_create_waits_for_global_lock_before_acquiring_table_write_lock(monkeypatch):
    async def scenario():
        async with _fixture() as (owner, _pools, _state, account, _other):
            global_locked, allow_table_locks = asyncio.Event(), asyncio.Event()
            original_version = importer._fetch_schema_version
            import_pid = None
            import_task = place_task = None

            async def version(conn):
                nonlocal import_pid
                result = await original_version(conn)
                import_pid = await _pid(conn)
                # Import holds the global detector key here, before requesting
                # any of the clean-target tables' SHARE ROW EXCLUSIVE locks.
                global_locked.set()
                await allow_table_locks.wait()
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
                # Import can now lock places, pass its check and commit; the
                # waiting UI create runs afterward, without a lock-order cycle.
                allow_table_locks.set()
                assert (await asyncio.wait_for(import_task, 5))["places"] == 1
                assert (await asyncio.wait_for(place_task, 5)).status_code == 204
                async with account.connection() as conn:
                    assert await (await conn.execute(
                        "SELECT name FROM places WHERE account_id=%s ORDER BY name", (account_id(conn),),
                    )).fetchall() == [("Concurrent place",), ("Imported place",)]
            finally:
                allow_table_locks.set()
                await _finish_tasks(import_task, place_task)

    asyncio.run(scenario())
