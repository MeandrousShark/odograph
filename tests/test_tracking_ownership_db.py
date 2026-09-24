"""Query-scoping, stream identity and transaction evidence with RLS enforced.

Application code runs on the real restricted control and runtime roles. The
privileged disposable pool only seeds, observes and resets.
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from psycopg.errors import InsufficientPrivilege

import app.tracking as tracking_module
from app.db import DETECTOR_ADVISORY_LOCK_KEY, make_pool
from app.detector.core import Params
from app.detector.runner import DETECTOR_VERSION, DetectorRunner, load_trip_points
from app.ingest import FailedAuthLimiter, make_router
from app.local_auth import hash_password
from app.tracking import (
    TrackingNotFound, admit_ingest, authenticate_ingest, convert_legacy_device, create_device,
    resolve_ingest_stream, revoke_credential, rotate_credential,
)
from conftest import account_pool, reset_db
from tests.synth import Drive, Stationary, build_track

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL for disposable DB tests")
SQL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "sql"


@asynccontextmanager
async def _fixture():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    installed = []
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            for name, signature in (
                ("account_admission", "public.assert_account_active(bigint,bigint)"),
                ("tracking_admission", "public.assert_tracking_credential(text,bigint,bigint,bigint,text)"),
            ):
                cur = await conn.execute("SELECT to_regprocedure(%s)", (signature,))
                if (await cur.fetchone())[0] is None:
                    await conn.execute((SQL_DIR / f"{name}.sql").read_text())
                    installed.append(signature)
            await conn.execute("DROP INDEX accounts_singleton_idx")
            for owner in (42, 84):
                await conn.execute(
                    "INSERT INTO accounts (id, email, password_hash) VALUES (%s, %s, 'test-only')",
                    (owner, f"account-{owner}@example.test"),
                )
                await conn.execute("INSERT INTO account_settings (account_id) VALUES (%s)", (owner,))
        yield pool, await account_pool(pool, 42), await account_pool(pool, 84)
    finally:
        async with pool.connection() as conn:
            for signature in reversed(installed):
                await conn.execute(f"DROP FUNCTION {signature}")
        await reset_db(pool)
        async with pool.connection() as conn:
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS accounts_singleton_idx ON accounts ((true))")
        await pool.close()


def _app(bound):
    app = FastAPI()
    app.state.control_pool = bound.control_pool
    app.state.runtime_pool = bound.runtime_pool
    app.state.config = SimpleNamespace(
        ingest_username="legacy", ingest_password="legacy-test-secret", ingest_max_body_bytes=10000,
    )
    app.state.ingest_limiter = FailedAuthLimiter(100, 60)
    app.state.wakes = []
    app.state.detector_scheduler = SimpleNamespace(poke=lambda: app.state.wakes.append(None))
    app.include_router(make_router())
    return app


def _payload(label="same"):
    return {"_type": "location", "tid": label, "tst": 1700000000, "lat": 47.6, "lon": -122.3,
            "account_id": 84, "tracking_device_id": 999999}


async def _authenticate(bound, username, password):
    return await authenticate_ingest(bound.control_pool, username, password,
                                     legacy_username="legacy", legacy_password="legacy-test-secret")


def test_identical_labels_and_timestamps_are_separate_authenticated_streams():
    async def run():
        async with _fixture() as (pool, a, b):
            async with a.connection() as conn:
                first = await create_device(conn, "Same phone")
                second = await create_device(conn, "Same phone")
            async with b.connection() as conn:
                third = await create_device(conn, "Same phone")
            app = _app(a)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                for issued in (first, second, third, first):
                    response = await client.post("/ingest", json=_payload(), auth=(issued.username, issued.secret))
                    assert response.status_code == 200
                response = await client.post("/ingest", json={"_type": "status"}, auth=(first.username, first.secret))
                assert response.status_code == 200
                response = await client.post("/ingest", json={"_type": "location", "lat": "invalid"},
                                             auth=(third.username, third.secret))
                assert response.status_code == 200
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT account_id, tracking_device_id, count(*) FROM points GROUP BY 1,2 ORDER BY 1,2"
                )).fetchall()
                assert rows == [(42, first.tracking_device_id, 1), (42, second.tracking_device_id, 1),
                                (84, third.tracking_device_id, 1)]
                assert (await (await conn.execute("SELECT count(*) FROM raw_messages WHERE account_id IS NULL")).fetchone())[0] == 0
                # 4 location posts + the invalid-location post (still _type
                # "location") are stored; the "status" post is not (see
                # STORED_MESSAGE_TYPES in app/ingest.py).
                assert (await (await conn.execute("SELECT count(*) FROM raw_messages")).fetchone())[0] == 5
            # poke() carries no per-account/device target (AccountWorker's
            # wake-key bookkeeping was unused dead weight -- every sweep
            # already revalidates every enabled principal); just confirm the
            # 4 accepted location posts each woke the detector once, even
            # the repeat that landed on an existing point via ON CONFLICT.
            assert len(app.state.wakes) == 4
    asyncio.run(run())


def test_legacy_conversion_rotation_and_revocation_preserve_stream_and_do_not_revive():
    async def run():
        async with _fixture() as (pool, a, _b):
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO ingest_credentials (public_id,basic_username,secret_hash,account_id,kind) "
                    "VALUES ('legacy-public','legacy',%s,42,'legacy')", (hash_password("legacy-test-secret"),),
                )
            app = _app(a)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                for label in ("one", "two"):
                    assert (await client.post("/ingest", json=_payload(label), auth=("legacy", "legacy-test-secret"))).status_code == 200
                async with a.connection() as conn:
                    device = (await (await conn.execute(
                        "SELECT tracking_device_id FROM tracking_device_aliases WHERE account_id=42 AND original_label='one'"
                    )).fetchone())[0]
                    issued = await convert_legacy_device(conn, device)
                assert issued.tracking_device_id == device
                assert (await client.post("/ingest", json=_payload("one"), auth=("legacy", "legacy-test-secret"))).status_code == 401
                assert (await client.post("/ingest", json=_payload("two"), auth=("legacy", "legacy-test-secret"))).status_code == 200
                stale = await _authenticate(a, issued.username, issued.secret)
                async with a.connection() as conn:
                    replacement = await rotate_credential(conn, issued.public_id)
                assert replacement.tracking_device_id == device
                assert await _authenticate(a, issued.username, issued.secret) is None
                async with a.connection() as conn:
                    stream = await resolve_ingest_stream(conn, stale, "irrelevant")
                    with pytest.raises(InsufficientPrivilege):
                        async with conn.transaction():
                            await admit_ingest(conn, stale, stream)
                assert (await client.post("/ingest", json=_payload("renamed"), auth=(replacement.username, replacement.secret))).status_code == 200
                async with a.connection() as conn:
                    await revoke_credential(conn, "legacy-public")
                assert await _authenticate(a, "legacy", "legacy-test-secret") is None
                assert (await client.post("/ingest", json=_payload("new"), auth=("legacy", "legacy-test-secret"))).status_code == 401
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT count(*) FROM tracking_devices")).fetchone())[0] == 2
                assert (await (await conn.execute("SELECT count(*) FROM points WHERE tracking_device_id=%s", (device,))).fetchone())[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize("device_state", ["disabled", "revoked"])
def test_rotation_rejects_unavailable_device_without_changing_credential(device_state):
    async def run():
        async with _fixture() as (pool, a, _b):
            async with a.connection() as conn:
                issued = await create_device(conn, "Phone")
                await revoke_credential(conn, issued.public_id)
                await conn.execute(
                    "UPDATE tracking_devices SET enabled = %s, revoked_at = CASE WHEN %s THEN now() END "
                    "WHERE account_id=42 AND id=%s",
                    (device_state != "disabled", device_state == "revoked", issued.tracking_device_id),
                )
            async with pool.connection() as conn:
                before = await (await conn.execute(
                    "SELECT secret_hash, generation, revoked_at FROM ingest_credentials WHERE public_id=%s",
                    (issued.public_id,),
                )).fetchone()
            with pytest.raises(TrackingNotFound):
                async with a.connection() as conn:
                    await rotate_credential(conn, issued.public_id)
            async with pool.connection() as conn:
                after = await (await conn.execute(
                    "SELECT secret_hash, generation, revoked_at FROM ingest_credentials WHERE public_id=%s",
                    (issued.public_id,),
                )).fetchone()
            assert after == before
    asyncio.run(run())


def test_create_device_issues_a_readable_username_matching_the_device_label():
    async def run():
        async with _fixture() as (pool, a, _b):
            async with a.connection() as conn:
                issued = await create_device(conn, "Work iPhone")
            assert re.match(r"^[a-z0-9-]+-[a-z0-9]{4}$", issued.username)
            assert issued.username.startswith("work-iphone-")
    asyncio.run(run())


def test_forced_basic_username_collision_retries_and_succeeds_across_accounts(monkeypatch):
    async def run():
        async with _fixture() as (pool, a, b):
            monkeypatch.setattr(tracking_module, "_random_suffix", lambda: "aaaa")
            async with a.connection() as conn:
                first = await create_device(conn, "Phone")
            assert first.username == "phone-aaaa"

            # The forced suffix repeats "aaaa" (colliding with account a's
            # username, since basic_username is unique instance-wide, not
            # per account) before "bbbb" succeeds, proving the retry looks
            # past its own account's rows rather than pre-checking with a
            # SELECT that RLS would otherwise hide.
            suffixes = iter(["aaaa", "bbbb"])
            monkeypatch.setattr(tracking_module, "_random_suffix", lambda: next(suffixes))
            async with b.connection() as conn:
                second = await create_device(conn, "Phone")
            assert second.username == "phone-bbbb"

            assert await _authenticate(a, first.username, first.secret) is not None
            assert await _authenticate(a, second.username, second.secret) is not None
            async with pool.connection() as conn:
                count = (await (await conn.execute(
                    "SELECT count(*) FROM ingest_credentials WHERE basic_username LIKE 'phone-%'"
                )).fetchone())[0]
                assert count == 2
    asyncio.run(run())


def test_exhausted_username_retries_raise_and_leave_no_partial_rows(monkeypatch):
    async def run():
        async with _fixture() as (pool, a, _b):
            monkeypatch.setattr(tracking_module, "_random_suffix", lambda: "aaaa")
            async with a.connection() as conn:
                await create_device(conn, "Phone")
            # Every further attempt for the same slug now collides on every
            # try, exhausting all 5 attempts.
            with pytest.raises(ValueError):
                async with a.connection() as conn:
                    await create_device(conn, "Phone")
            async with pool.connection() as conn:
                devices = (await (await conn.execute(
                    "SELECT count(*) FROM tracking_devices WHERE account_id = 42 AND label = 'Phone'"
                )).fetchone())[0]
                credentials = (await (await conn.execute(
                    "SELECT count(*) FROM ingest_credentials"
                )).fetchone())[0]
                detector_rows = (await (await conn.execute(
                    "SELECT count(*) FROM detector_state WHERE account_id = 42"
                )).fetchone())[0]
                assert devices == 1
                assert credentials == 1
                assert detector_rows == 1
    asyncio.run(run())


def test_rotation_keeps_the_readable_username():
    async def run():
        async with _fixture() as (pool, a, _b):
            async with a.connection() as conn:
                issued = await create_device(conn, "Phone")
            async with a.connection() as conn:
                replacement = await rotate_credential(conn, issued.public_id)
            assert replacement.username == issued.username
            assert replacement.secret != issued.secret
            assert await _authenticate(a, issued.username, issued.secret) is None
            assert await _authenticate(a, issued.username, replacement.secret) is not None
    asyncio.run(run())


def test_pre_change_odograph_prefixed_username_still_authenticates():
    async def run():
        async with _fixture() as (pool, a, _b):
            async with a.connection() as conn:
                cur = await conn.execute(
                    "INSERT INTO tracking_devices (account_id, label) VALUES (42, 'Old phone') RETURNING id"
                )
                device_id = (await cur.fetchone())[0]
                await conn.execute(
                    "INSERT INTO detector_state (account_id, tracking_device_id) VALUES (42, %s)", (device_id,)
                )
            legacy_username = "odograph_AbC123-_XyZ9"
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO ingest_credentials "
                    "(public_id,basic_username,secret_hash,account_id,tracking_device_id,kind) "
                    "VALUES (%s,%s,%s,42,%s,'device')",
                    (legacy_username, legacy_username, hash_password("legacy-device-secret"), device_id),
                )
            assert await _authenticate(a, legacy_username, "legacy-device-secret") is not None
            assert await _authenticate(a, legacy_username, "wrong-secret") is None
            assert await _authenticate(a, "unknown-" + legacy_username, "legacy-device-secret") is None
    asyncio.run(run())


def test_wrong_secret_and_unknown_username_both_get_the_same_401():
    async def run():
        async with _fixture() as (pool, a, _b):
            async with a.connection() as conn:
                issued = await create_device(conn, "Phone")
            app = _app(a)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                wrong_secret = await client.post("/ingest", json=_payload(), auth=(issued.username, "wrong-secret"))
                unknown_username = await client.post(
                    "/ingest", json=_payload(), auth=("no-such-user-zzzz", issued.secret),
                )
                assert wrong_secret.status_code == 401
                assert unknown_username.status_code == 401
    asyncio.run(run())


async def _points(conn, device, points):
    owner = conn.principal.account_id
    for point in points:
        await conn.execute(
            "INSERT INTO points (account_id,tracking_device_id,device,recorded_at,received_at,geom,accuracy_m,velocity_kmh) "
            "VALUES (%s,%s,'same',%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s)",
            (owner,device,point.t,point.t,point.lon,point.lat,point.accuracy_m,point.velocity_kmh),
        )


@pytest.mark.parametrize("other_setting", ["off", "inactive", "no_default"])
def test_detector_scopes_same_label_streams_and_default_vehicle_settings(other_setting):
    async def run():
        async with _fixture() as (pool, a, b):
            track = build_track([Stationary(900), Drive(km=3), Stationary(900)])
            devices = []
            vehicles = []
            for bound, enabled in ((a, True), (b, other_setting != "off")):
                async with bound.connection() as conn:
                    issued = await create_device(conn, "same")
                    devices.append(issued.tracking_device_id)
                    await _points(conn, issued.tracking_device_id, track)
                    cur = await conn.execute(
                        "INSERT INTO vehicles (account_id,name,is_default) VALUES (%s,'Own car',true) RETURNING id",
                        (bound.principal.account_id,),
                    )
                    vehicles.append((await cur.fetchone())[0])
                    if bound is b and other_setting != "off":
                        await conn.execute(
                            "UPDATE vehicles SET active=%s,is_default=%s WHERE account_id=%s",
                            (other_setting != "inactive", other_setting != "no_default", bound.principal.account_id),
                        )
                    await conn.execute(
                        "UPDATE account_settings SET auto_assign_default_vehicle=%s WHERE account_id=%s",
                        (enabled,bound.principal.account_id),
                    )
            for bound in (a,b):
                assert await DetectorRunner(bound, Params()).run_once()
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT account_id,tracking_device_id,id,vehicle_id FROM trips ORDER BY account_id"
                )).fetchall()
            assert len(rows) == 2
            assert rows[0][:2] == (42,devices[0]) and rows[0][3] == vehicles[0]
            assert rows[1][:2] == (84,devices[1]) and rows[1][3] is None
            async with a.connection() as conn:
                assert await load_trip_points(conn,rows[1][2]) == []
                points = await load_trip_points(conn,rows[0][2])
                assert points
                await conn.execute("UPDATE trips SET vehicle_id=NULL WHERE account_id=42 AND id=%s",(rows[0][2],))
            await DetectorRunner(a,Params()).reprocess_device_now(devices[0])
            async with pool.connection() as conn:
                assert (await (await conn.execute("SELECT vehicle_id FROM trips WHERE id=%s",(rows[0][2],))).fetchone())[0] is None
    asyncio.run(run())


def test_detector_failure_rolls_back_only_its_stream_and_lock_skip_keeps_checkpoint():
    async def run():
        async with _fixture() as (pool,a,_b):
            track = build_track([Stationary(900),Drive(km=3),Stationary(900)])
            async with a.connection() as conn:
                first = await create_device(conn,"same")
                second = await create_device(conn,"same")
                for issued in (first,second):
                    await _points(conn,issued.tracking_device_id,track)
            runner = DetectorRunner(a,Params())
            process = runner._process_device
            async def fail_first(conn,device,dirty_from,full):
                await process(conn,device,dirty_from,full)
                if device == first.tracking_device_id:
                    raise RuntimeError("injected stream failure")
            runner._process_device = fail_first
            with pytest.raises(RuntimeError,match="injected stream failure"):
                await runner.run_once()
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT tracking_device_id,last_run_at,detector_version FROM detector_state WHERE account_id=42 ORDER BY tracking_device_id"
                )).fetchall()
                assert rows[0] == (first.tracking_device_id,None,0)
                assert rows[1][1] is not None and rows[1][2] == DETECTOR_VERSION
                trips = await (await conn.execute("SELECT DISTINCT tracking_device_id FROM trips")).fetchall()
                assert trips == [(second.tracking_device_id,)]
            runner._process_device = process
            async with pool.connection() as blocker:
                await blocker.execute("SELECT pg_advisory_xact_lock(%s)",(DETECTOR_ADVISORY_LOCK_KEY,))
                assert await runner.run_once() is False
                async with pool.connection() as conn:
                    assert (await (await conn.execute("SELECT last_run_at FROM detector_state WHERE tracking_device_id=%s",(first.tracking_device_id,))).fetchone())[0] is None
            assert await runner.run_once()
    asyncio.run(run())


@pytest.mark.parametrize("rollback", [False, True])
def test_credential_revocation_waits_for_admitted_transaction_and_then_blocks_new_work(rollback):
    async def run():
        async with _fixture() as (pool, a, _b):
            async with a.connection() as conn:
                issued = await create_device(conn, "phone")
            credential = await _authenticate(a, issued.username, issued.secret)
            started = asyncio.Event()
            revoker_pid = None

            async def revoke():
                nonlocal revoker_pid
                async with a.connection() as conn:
                    revoker_pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                    started.set()
                    await revoke_credential(conn, issued.public_id)

            class InjectedRollback(Exception):
                pass

            task = None
            try:
                async with a.connection() as admitted:
                    stream = await resolve_ingest_stream(admitted, credential, "phone")
                    await admit_ingest(admitted, credential, stream)
                    admitted_pid = (await (await admitted.execute("SELECT pg_backend_pid()")).fetchone())[0]
                    task = asyncio.create_task(revoke())
                    await asyncio.wait_for(started.wait(), 5)
                    # Observe an actual database blocker, not a timing guess.
                    async with asyncio.timeout(5):
                        while True:
                            async with pool.connection() as observer:
                                blockers = (await (await observer.execute(
                                    "SELECT pg_blocking_pids(%s)", (revoker_pid,),
                                )).fetchone())[0]
                            if admitted_pid in blockers:
                                break
                            assert not task.done(), "revocation bypassed admitted credential lock"
                            await asyncio.sleep(0)
                    if rollback:
                        raise InjectedRollback()
            except InjectedRollback:
                pass
            finally:
                if task is not None:
                    await asyncio.wait_for(task, 5)
            assert await _authenticate(a, issued.username, issued.secret) is None
            async with a.connection() as conn:
                with pytest.raises(InsufficientPrivilege):
                    async with conn.transaction():
                        await admit_ingest(conn, credential, stream)
    asyncio.run(run())


def test_tracking_setup_forms_use_normal_account_auth_and_show_secret_only_once(monkeypatch):
    import base64
    import json
    import re
    from unittest.mock import patch

    from fastapi import APIRouter
    from itsdangerous import TimestampSigner
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.responses import RedirectResponse

    from app.auth import AuthRedirect
    from app.config import Config
    from app.main import make_templates
    from app.ui import tracking as tracking_ui

    async def run():
        async with _fixture() as (pool, _a, b):
            async with b.connection() as conn:
                foreign = await create_device(conn, "Other account")
            app = _app(b)
            with patch.dict(os.environ, {"DATABASE_URL": TEST_DB, "SESSION_SECRET": "test-cookie-secret"}, clear=True):
                app.state.config = Config.from_env()
            app.state.templates = make_templates(app.state.config)
            app.state.make_detector_runner = lambda bound: DetectorRunner(bound, Params())
            app.add_middleware(SessionMiddleware, secret_key="test-cookie-secret")

            @app.exception_handler(AuthRedirect)
            async def redirect_login(request, exc):
                return RedirectResponse("/login", status_code=303)

            router = APIRouter()
            tracking_ui.register(router)
            app.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                assert (await client.get("/settings/tracking")).status_code == 303
                session = base64.b64encode(json.dumps({"account_id":42,"auth_version":1,"csrf":"fixture-csrf"}).encode())
                cookie = TimestampSigner("test-cookie-secret").sign(session).decode()
                client.cookies.set("session", cookie)
                page = await client.get("/settings/tracking")
                assert page.status_code == 200
                assert "Other account" not in page.text
                denied = await client.post("/settings/tracking/devices", data={"label":"Phone", "csrf_token":"wrong"})
                assert denied.status_code == 403
                created = await client.post("/settings/tracking/devices", data={"label":"Phone", "csrf_token":"fixture-csrf"})
                assert created.status_code == 200
                assert created.headers["cache-control"] == "no-store"
                username = re.search(r'User <input readonly value="([^"]+)"', created.text).group(1)
                secret = re.search(r'Password <input readonly value="([^"]+)"', created.text).group(1)
                assert re.match(r"^[a-z0-9-]+-[a-z0-9]{4}$", username) and username.startswith("phone-")
                # A copy button beside each of URL/User/Password, wired up by
                # a static, non-inline script tag under the CSP-allowed path.
                assert created.text.count('data-copy-target="tracking-setup-') == 3
                assert re.search(
                    r'<button type="button"[^>]*data-copy-target="tracking-setup-url"[^>]*>Copy</button>',
                    created.text,
                )
                assert 'aria-live="polite"' in created.text
                # The tag closes immediately with no body, so this also
                # confirms the copy logic is not an inline script.
                assert '<script src="/static/tracking_copy.js"></script>' in created.text
                assert created.text.count(secret) == 1
                credential = await _authenticate(b, username, secret)
                assert credential is not None and credential.account.account_id == 42
                page = await client.get("/settings/tracking")
                assert secret not in page.text
                foreign_rotation = await client.post(
                    f"/settings/tracking/credentials/{foreign.public_id}/rotate", data={"csrf_token":"fixture-csrf"},
                )
                assert foreign_rotation.status_code == 404
                assert await _authenticate(b,foreign.username,foreign.secret) is not None
                rotated = await client.post(
                    f"/settings/tracking/credentials/{credential.public_id}/rotate", data={"csrf_token":"fixture-csrf"},
                )
                assert rotated.status_code == 200
                replacement = re.search(r'Password <input readonly value="([^"]+)"', rotated.text).group(1)
                assert replacement != secret
                assert await _authenticate(b,username,secret) is None
                assert await _authenticate(b,username,replacement) is not None
                revoked = await client.post(
                    f"/settings/tracking/credentials/{credential.public_id}/revoke", data={"csrf_token":"fixture-csrf"},
                )
                assert revoked.status_code == 303
                assert await _authenticate(b,username,replacement) is None
                renewed = await client.post(
                    f"/settings/tracking/credentials/{credential.public_id}/rotate", data={"csrf_token":"fixture-csrf"},
                )
                assert renewed.status_code == 200
                assert await _authenticate(b,username,replacement) is None
    asyncio.run(run())
