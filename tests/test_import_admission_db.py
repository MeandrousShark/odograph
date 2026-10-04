"""Account-scoped admission and concurrency tests for portable imports."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from psycopg import errors
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

import app.portable.routes as portable_routes
from app.account_context import current_account_context
from app.account_lifecycle import set_account_enabled
from app.accounts import get_account
from app.auth import AuthRedirect
from app.db import _fetch_schema_version, make_pool
from app.ingest import FailedAuthLimiter, make_router as make_ingest_router
from app.main import make_templates
from app.portable.importer import PortableImportError
from app.tracking import create_device
from conftest import add_test_account, reset_account_db
from tests.auth_db_fixtures import auth_config

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")


@asynccontextmanager
async def _fixture():
    admin = make_pool(TEST_DB)
    await admin.open(wait=True)
    try:
        account_a = await reset_account_db(admin)
        account_b = await add_test_account(admin, 84)
        yield admin, account_a, account_b
    finally:
        await admin.close()


def _import_app(bound) -> FastAPI:
    """Use signed sessions and the real auth dependencies on import routes."""
    app = FastAPI()
    cfg = auth_config(TEST_DB, dev_no_auth=False)
    app.state.config = cfg
    app.state.control_pool = bound.control_pool
    app.state.runtime_pool = bound.runtime_pool
    app.state.make_detector_runner = lambda pool: SimpleNamespace(pool=pool)
    app.state.templates = make_templates(cfg)
    app.add_middleware(
        SessionMiddleware, secret_key=cfg.session_secret, same_site="lax", https_only=False,
    )

    @app.post("/__test/session")
    async def seed_session(request: Request):
        request.session.update({
            "account_id": bound.principal.account_id,
            "auth_version": bound.principal.auth_version,
            "csrf": "import-admission-test-csrf",
        })
        return Response(status_code=204)

    @app.exception_handler(AuthRedirect)
    async def auth_redirect(request, _exc):
        return RedirectResponse("/login", status_code=303)

    app.include_router(portable_routes.make_router())
    return app


def _ingest_app(bound) -> FastAPI:
    app = FastAPI()
    app.state.control_pool = bound.control_pool
    app.state.runtime_pool = bound.runtime_pool
    app.state.config = SimpleNamespace(
        ingest_username="legacy", ingest_password="unused-test-secret",
        ingest_max_body_bytes=10000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(100, 60)
    app.state.detector_scheduler = SimpleNamespace(poke=lambda: None)
    app.include_router(make_ingest_router())
    return app


async def _import_client(bound):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_import_app(bound)),
        base_url="http://testserver",
    )
    response = await client.post("/__test/session")
    if response.status_code != 204:
        await client.aclose()
    assert response.status_code == 204
    return client


def _bundle(schema_version: int, *, vehicle_name="Imported Car") -> dict:
    return {
        "format": "odograph-portable",
        "format_version": 1 if schema_version == 21 else 2 if schema_version <= 25 else 3,
        "schema_version": schema_version,
        "exported_at": "2026-08-05T00:00:00+00:00",
        "vehicles": [{
            "$id": 1, "name": vehicle_name, "make": None, "model": None,
            "plate": None, "is_default": True, "active": True,
        }],
        "places": [], "tag_rules": [], "mileage_rates": [], "trips": [],
        "expenses": [], "odometer_readings": [],
        "settings": {"auto_assign_default_vehicle": False, "display_tz": "UTC"},
    }


async def _current_bundle(bound, *, vehicle_name="Imported Car") -> dict:
    async with bound.connection() as conn:
        schema_version = await _fetch_schema_version(conn)
    return _bundle(schema_version, vehicle_name=vehicle_name)


async def _post_import(client, bundle: dict, *, dry_run=False) -> httpx.Response:
    data = {"csrf_token": "import-admission-test-csrf"}
    if dry_run:
        data["dry_run"] = "1"
    return await client.post(
        "/settings/import/data", data=data,
        files={"file": ("bundle.json", json.dumps(bundle).encode(), "application/json")},
    )


async def _vehicle_names(admin, owner: int) -> list[str]:
    async with admin.connection() as conn:
        cur = await conn.execute(
            "SELECT name FROM vehicles WHERE account_id = %s ORDER BY id", (owner,),
        )
        return [row[0] for row in await cur.fetchall()]


async def _has_blocked_query(admin, blocker_pid: int, query_fragment: str) -> bool:
    async with admin.connection() as conn:
        cur = await conn.execute(
            "WITH RECURSIVE lock_chain(waiter_pid, blocker_pid, path) AS ("
            "SELECT activity.pid, blocker.pid, ARRAY[activity.pid, blocker.pid] "
            "FROM pg_stat_activity AS activity "
            "CROSS JOIN LATERAL unnest(pg_blocking_pids(activity.pid)) AS blocker(pid) "
            "WHERE activity.datname = current_database() AND activity.state = 'active' "
            "AND activity.wait_event_type = 'Lock' AND strpos(activity.query, %s) > 0 "
            "UNION ALL "
            "SELECT lock_chain.waiter_pid, next_blocker.pid, "
            "lock_chain.path || next_blocker.pid "
            "FROM lock_chain "
            "CROSS JOIN LATERAL unnest(pg_blocking_pids(lock_chain.blocker_pid)) "
            "AS next_blocker(pid) "
            "WHERE NOT next_blocker.pid = ANY(lock_chain.path)) "
            "SELECT EXISTS (SELECT 1 FROM lock_chain WHERE blocker_pid = %s)",
            (query_fragment, blocker_pid),
        )
        return (await cur.fetchone())[0]


async def _wait_for_blocked_query(admin, blocker_pid: int, query_fragment: str, task) -> None:
    async def poll():
        while not await _has_blocked_query(admin, blocker_pid, query_fragment):
            assert not task.done(), f"{query_fragment} finished while the import lock was held"
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 5)


@pytest.mark.db
def test_import_exclusive_function_checks_account_context_and_auth_version():
    async def run():
        async with _fixture() as (_admin, account_a, account_b):
            owner = account_a.principal.account_id
            version = account_a.principal.auth_version
            async with account_a.connection() as conn:
                await conn.execute(
                    "SELECT public.assert_import_account_exclusive(%s, %s)", (owner, version),
                )
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT public.assert_import_account_exclusive(%s, %s)",
                            (account_b.principal.account_id, account_b.principal.auth_version),
                        )
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT public.assert_import_account_exclusive(%s, %s)",
                            (owner, version + 1),
                        )

            async with account_a.runtime_pool.connection() as raw:
                async with raw.transaction():
                    assert await current_account_context(raw) is None
                    with pytest.raises(errors.InsufficientPrivilege):
                        await raw.execute(
                            "SELECT public.assert_import_account_exclusive(%s, %s)",
                            (owner, version),
                        )

            async with account_a.control_pool.connection() as control:
                with pytest.raises(errors.InsufficientPrivilege):
                    await control.execute(
                        "SELECT public.assert_import_account_exclusive(%s, %s)",
                        (owner, version),
                    )

    asyncio.run(run())


@pytest.mark.db
def test_import_excludes_same_account_ingest_but_other_account_ingest_commits(monkeypatch):
    async def run():
        async with _fixture() as (admin, account_a, account_b):
            async with account_a.connection() as conn:
                phone_a = await create_device(conn, "phone-a")
            async with account_b.connection() as conn:
                phone_b = await create_device(conn, "phone-b")

            bundle = await _current_bundle(account_a)
            entered, release = asyncio.Event(), asyncio.Event()
            import_pid = None
            original_apply = portable_routes._apply_import

            async def hold_after_apply(conn, normalized):
                nonlocal import_pid
                summary = await original_apply(conn, normalized)
                import_pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                entered.set()
                await release.wait()
                return summary

            monkeypatch.setattr(portable_routes, "_apply_import", hold_after_apply)
            import_client = await _import_client(account_a)
            ingest_app = _ingest_app(account_a)
            ingest_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=ingest_app), base_url="http://testserver",
            )
            import_task = same_task = None
            try:
                import_task = asyncio.create_task(_post_import(import_client, bundle))
                await asyncio.wait_for(entered.wait(), 5)

                async def ingest(phone, label):
                    return await ingest_client.post(
                        "/ingest",
                        json={"_type": "location", "tid": label, "tst": 1700000000,
                              "lat": 47.6, "lon": -122.3},
                        auth=(phone.username, phone.secret),
                    )

                other_response = await asyncio.wait_for(ingest(phone_b, "phone-b"), 5)
                assert other_response.status_code == 200
                async with admin.connection() as conn:
                    rows = await (await conn.execute(
                        "SELECT account_id, count(*) FROM raw_messages GROUP BY account_id ORDER BY account_id"
                    )).fetchall()
                assert rows == [(account_b.principal.account_id, 1)]

                same_task = asyncio.create_task(ingest(phone_a, "phone-a"))
                await _wait_for_blocked_query(
                    admin, import_pid, "assert_account_active", same_task,
                )

                release.set()
                import_response = await asyncio.wait_for(import_task, 5)
                same_response = await asyncio.wait_for(same_task, 5)
                assert import_response.status_code == 200
                assert same_response.status_code == 200
                async with admin.connection() as conn:
                    rows = await (await conn.execute(
                        "SELECT account_id, count(*) FROM raw_messages GROUP BY account_id ORDER BY account_id"
                    )).fetchall()
                assert rows == [
                    (account_a.principal.account_id, 1),
                    (account_b.principal.account_id, 1),
                ]
            finally:
                release.set()
                for task in (import_task, same_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (import_task, same_task) if task is not None),
                    return_exceptions=True,
                )
                await ingest_client.aclose()
                await import_client.aclose()

    asyncio.run(run())


@pytest.mark.db
def test_same_account_import_contender_returns_busy_before_reading_upload(monkeypatch):
    async def run():
        async with _fixture() as (_admin, account_a, _account_b):
            bundle = await _current_bundle(account_a)
            entered, release = asyncio.Event(), asyncio.Event()
            original_apply = portable_routes._apply_import

            async def hold_after_apply(conn, normalized):
                summary = await original_apply(conn, normalized)
                entered.set()
                await release.wait()
                return summary

            monkeypatch.setattr(portable_routes, "_apply_import", hold_after_apply)
            client = await _import_client(account_a)
            first = None
            try:
                first = asyncio.create_task(_post_import(client, bundle))
                await asyncio.wait_for(entered.wait(), 5)

                boundary = "c1-import-admission-boundary"
                raw = (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n"
                    f"import-admission-test-csrf\r\n--{boundary}\r\n"
                    "Content-Disposition: form-data; name=\"file\"; filename=\"bundle.json\"\r\n"
                    "Content-Type: application/json\r\n\r\n"
                    f"{json.dumps(bundle)}\r\n--{boundary}--\r\n"
                ).encode()
                body_started = asyncio.Event()

                async def body_stream():
                    body_started.set()
                    yield raw

                started_at = time.monotonic()
                contender = await asyncio.wait_for(client.post(
                    "/settings/import/data", content=body_stream(),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                ), 4)
                elapsed = time.monotonic() - started_at
                assert contender.status_code == 503
                assert contender.headers.get("retry-after") == "1"
                assert contender.json()["error"] == "import_busy"
                assert 0.75 <= elapsed < 3.0
                assert not body_started.is_set(), "busy import read its multipart upload before admission"
            finally:
                release.set()
                if first is not None and not first.done():
                    first.cancel()
                if first is not None:
                    await asyncio.gather(first, return_exceptions=True)
                await client.aclose()

    asyncio.run(run())


@pytest.mark.db
def test_import_barrier_serializes_account_reads_and_admin_disablement(monkeypatch):
    async def run():
        async with _fixture() as (admin, account_a, account_b):
            bundle = await _current_bundle(account_b)
            entered, release = asyncio.Event(), asyncio.Event()
            import_pid = None
            original_apply = portable_routes._apply_import

            async def hold_after_apply(conn, normalized):
                nonlocal import_pid
                summary = await original_apply(conn, normalized)
                import_pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                entered.set()
                await release.wait()
                return summary

            monkeypatch.setattr(portable_routes, "_apply_import", hold_after_apply)
            client = await _import_client(account_b)
            import_task = read_task = disable_task = None
            try:
                import_task = asyncio.create_task(_post_import(client, bundle))
                await asyncio.wait_for(entered.wait(), 5)

                async def read_target():
                    async with account_b.connection() as conn:
                        return await (await conn.execute(
                            "SELECT count(*) FROM vehicles WHERE account_id=%s",
                            (account_b.principal.account_id,),
                        )).fetchone()

                read_task = asyncio.create_task(read_target())
                await _wait_for_blocked_query(
                    admin, import_pid, "assert_account_active", read_task,
                )

                disable_started = asyncio.Event()

                async def disable_target():
                    async with account_a.control_pool.connection() as control:
                        actor = await get_account(control, account_a.principal.account_id)
                        disable_started.set()
                        return await set_account_enabled(
                            control, actor, account_b.principal.account_id, enable=False,
                        )

                disable_task = asyncio.create_task(disable_target())
                await asyncio.wait_for(disable_started.wait(), 5)
                await _wait_for_blocked_query(
                    admin, import_pid, "admin_set_account_enabled", disable_task,
                )

                async with admin.connection() as conn:
                    state = await (await conn.execute(
                        "SELECT is_enabled, auth_version FROM accounts WHERE id=%s",
                        (account_b.principal.account_id,),
                    )).fetchone()
                assert state == (True, account_b.principal.auth_version)

                release.set()
                import_response = await asyncio.wait_for(import_task, 5)
                assert import_response.status_code == 200
                assert await asyncio.wait_for(read_task, 5) == (1,)
                assert await asyncio.wait_for(disable_task, 5) == "disabled"
                async with admin.connection() as conn:
                    state = await (await conn.execute(
                        "SELECT is_enabled, auth_version FROM accounts WHERE id=%s",
                        (account_b.principal.account_id,),
                    )).fetchone()
                assert state == (False, account_b.principal.auth_version + 1)
                refused = await _post_import(client, bundle)
                assert refused.status_code == 303
                assert refused.headers["location"] == "/login"
            finally:
                release.set()
                for task in (import_task, read_task, disable_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (import_task, read_task, disable_task) if task is not None),
                    return_exceptions=True,
                )
                await client.aclose()

    asyncio.run(run())


@pytest.mark.db
def test_import_dry_run_and_failure_roll_back_and_release_admission(monkeypatch):
    async def run():
        async with _fixture() as (admin, account_a, _account_b):
            owner = account_a.principal.account_id
            original_names = await _vehicle_names(admin, owner)
            bundle = await _current_bundle(account_a)
            original_apply = portable_routes._apply_import
            client = await _import_client(account_a)
            try:
                dry_response = await _post_import(client, bundle, dry_run=True)
                assert dry_response.status_code == 200
                assert dry_response.json()["dry_run"] is True
                assert await _vehicle_names(admin, owner) == original_names

                async def fail_after_apply(conn, normalized):
                    await original_apply(conn, normalized)
                    raise PortableImportError("injected_failure", "rollback after applied writes")

                monkeypatch.setattr(portable_routes, "_apply_import", fail_after_apply)
                failed_response = await _post_import(client, bundle)
                assert failed_response.status_code == 409
                assert failed_response.json()["error"] == "injected_failure"
                assert await _vehicle_names(admin, owner) == original_names

                monkeypatch.setattr(portable_routes, "_apply_import", original_apply)
                successful_response = await _post_import(client, bundle)
                assert successful_response.status_code == 200
                assert await _vehicle_names(admin, owner) == ["Imported Car"]
            finally:
                await client.aclose()

    asyncio.run(run())


@pytest.mark.db
def test_cancelled_import_rolls_back_and_releases_admission(monkeypatch):
    async def run():
        async with _fixture() as (admin, account_a, _account_b):
            owner = account_a.principal.account_id
            original_names = await _vehicle_names(admin, owner)
            bundle = await _current_bundle(account_a)
            entered = asyncio.Event()
            original_apply = portable_routes._apply_import

            async def pause_after_apply(conn, normalized):
                summary = await original_apply(conn, normalized)
                entered.set()
                await asyncio.Event().wait()
                return summary

            monkeypatch.setattr(portable_routes, "_apply_import", pause_after_apply)
            client = await _import_client(account_a)
            request = None
            try:
                request = asyncio.create_task(_post_import(client, bundle))
                await asyncio.wait_for(entered.wait(), 5)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert await _vehicle_names(admin, owner) == original_names

                monkeypatch.setattr(portable_routes, "_apply_import", original_apply)
                response = await _post_import(client, bundle)
                assert response.status_code == 200
                assert await _vehicle_names(admin, owner) == ["Imported Car"]
            finally:
                if request is not None and not request.done():
                    request.cancel()
                if request is not None:
                    await asyncio.gather(request, return_exceptions=True)
                await client.aclose()

    asyncio.run(run())
