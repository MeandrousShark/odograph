"""Production SMTP deadlines preserve delivery and account lifecycle contracts."""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from psycopg import errors

from app import admin, auth
from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_lifecycle import AccountLifecycleUnavailable, purge_account, request_account_deletion
from app.account_work import external_account_work
from app.account_workers import AccountWorker
from app.capacity import AdmissionManager, ManagedPool
from app.email_challenges import PURPOSE_CURRENT, consume_email_challenge
from app.invitations import issue_invitation_record
from app.mailer import Mailer
from app.password_reset import RecoveryQueue, SecurityMailAdmission, password_reset_usable
from tests.auth_db_fixtures import auth_config
from tests.mail_relay import LocalRelay
from tests.test_admin_deletion_db import _activate, _elapsed
from tests.test_admin_routes_db import _app, _client, _scenario
from tests.test_email_digest_db import (
    APP_URL, FILING_NOW, TZ, WINDOW_END, _FixedNowEmailDigestWorker,
    _create_vehicle, _insert_trip, _with_pool, _worker,
)

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


def _mailer(relay, recipient="you@example.com"):
    return Mailer("127.0.0.1", relay.port, "", "", "none", False,
                  "odograph@example.invalid", recipient)


def _deadline(monkeypatch, seconds=3):
    from app import smtp_supervisor

    monkeypatch.setattr(smtp_supervisor, "TRANSPORT_TIMEOUT_S", seconds)
    children = []
    original = smtp_supervisor.asyncio.create_subprocess_exec

    async def capture_child(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(smtp_supervisor.asyncio, "create_subprocess_exec", capture_child)
    return children


async def _reaped(relay, children):
    await asyncio.wait_for(relay.disconnected.wait(), 2)
    assert children
    assert all(child.returncode is not None for child in children)


@pytest.mark.parametrize("phase", ["greeting", "quit"])
def test_transport_failure_rolls_back_digest_ledger_then_other_kind_retry_and_restart_progress(monkeypatch, phase):
    children = _deadline(monkeypatch)

    async def check(pool):
        async with pool.connection() as conn:
            await _insert_trip(conn, WINDOW_END - timedelta(days=1))
            await _create_vehicle(conn, "Truck")
        async with LocalRelay(phase) as relay:
            mailer = _mailer(relay)
            now = WINDOW_END + timedelta(hours=1)
            await _worker(pool, mailer, weekly=True, odometer=True).run_once(now)
            await _reaped(relay, children)
            async with pool.connection() as conn:
                rows = await (await conn.execute("SELECT kind,sent FROM email_deliveries ORDER BY kind")).fetchall()
                assert rows == [("quarterly_odometer", True)]
            # The next hour retries only the failed kind, including ambiguous DATA.
            await _worker(pool, mailer, weekly=True, odometer=True).run_once(now + timedelta(hours=1))
            sessions = relay.sessions
            await _worker(pool, mailer, weekly=True, odometer=True).run_once(now + timedelta(hours=2))
            assert relay.sessions == sessions == 3
            assert len(relay.messages) == (3 if phase == "quit" else 2)
            async with pool.connection() as conn:
                rows = await (await conn.execute("SELECT kind,sent FROM email_deliveries ORDER BY kind")).fetchall()
                assert rows == [("quarterly_odometer", True), ("weekly_nudge", True)]
            assert all(child.returncode is not None for child in children)

    asyncio.run(_with_pool(check))


def test_ambiguous_digest_turn_rotates_accounts_and_kinds_without_immediate_retry(monkeypatch):
    children = _deadline(monkeypatch)

    async def check(owner, pools, actor):
        target = await _activate(owner)
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE account_settings SET display_tz=%s,email_to=CASE account_id WHEN %s "
                "THEN 'admin@example.invalid' ELSE 'member@example.invalid' END,"
                "email_monthly_summary=true,email_filing_reminder=true", (str(TZ), actor["id"]),
            )
        async with LocalRelay() as relay:
            def factory(pool, config):
                return _FixedNowEmailDigestWorker(
                    pool, _mailer(relay, config.email_to), APP_URL, TZ, 18, 9, 9,
                    "01-15", False, True, True, False, now=FILING_NOW,
                )

            worker = AccountWorker(pools, auth_config(TEST_DB), factory,
                                   label="email-digest-worker", debounce_s=1, sweep_s=3600)
            first = await worker.run_turn()
            assert first.batch.retriable_failures == 1
            await _reaped(relay, children)
            for _ in range(3):
                assert (await worker.run_turn()).batch.retriable_failures == 0
            assert [str(message["To"]) for message in relay.messages] == [
                "admin@example.invalid", "member@example.invalid", "member@example.invalid", "admin@example.invalid",
            ]
            async with owner.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT account_id,kind FROM email_deliveries ORDER BY account_id,kind",
                )).fetchall()
                assert rows == [(actor["id"], "filing_reminder"),
                                (target, "filing_reminder"), (target, "monthly_summary")]

    asyncio.run(_scenario(check))


def test_recovery_shutdown_drains_ambiguous_send_before_revocation_and_admission_release(monkeypatch):
    children = _deadline(monkeypatch)

    async def check(owner, pools, actor):
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET email_verified_at=now() WHERE id=%s", (actor["id"],))
        async with LocalRelay() as relay:
            admission = SecurityMailAdmission(limit=1)
            queue = RecoveryQueue(pools.control, APP_URL, lambda address: _mailer(relay, address), admission)
            await queue.start()
            assert queue.submit_public(actor["email"])
            await asyncio.wait_for(relay.entered.wait(), 5)
            shutdown = asyncio.create_task(queue.stop())
            await asyncio.sleep(0)
            assert not shutdown.done()
            assert admission._slots.locked() and children[0].returncode is None
            await asyncio.wait_for(shutdown, 6)
            await _reaped(relay, children)
            assert len(relay.messages) == 1
            token = re.search(r"reset-password#token=([A-Za-z0-9_-]{43})", relay.messages[0].get_content())[1]
            async with pools.control.connection() as conn:
                assert not await password_reset_usable(conn, token)
            assert not admission._tasks and not admission._slots.locked()

    asyncio.run(_scenario(check))


def test_ambiguously_accepted_challenge_is_revoked_and_response_keeps_token_private(monkeypatch):
    children = _deadline(monkeypatch)

    async def check(owner, pools, actor):
        async with LocalRelay() as relay:
            config = auth_config(TEST_DB, smtp_host="127.0.0.1", smtp_port=relay.port,
                                 smtp_security="none", email_from="odograph@example.invalid", app_url=APP_URL)
            app = _app(pools, config=config)
            app.include_router(auth.make_router())

            async def verified(*_args, **_kwargs):
                return dict(actor, auth_version=1)

            monkeypatch.setattr(auth, "_verified_account", verified)
            async with await _client(app) as client:
                await client.post(f"/test/session/{actor['id']}/1")
                response = await client.post("/settings/account/email/verify/request", data={
                    "csrf_token": "route-csrf", "current_password": "fixture-password",
                })
                assert response.status_code == 503
                await app.state.security_mail.drain()
            await _reaped(relay, children)
            assert len(relay.messages) == 1
            token = re.search(r"&token=([A-Za-z0-9_-]{43})", relay.messages[0].get_content())[1]
            assert token not in response.text
            async with pools.control.connection() as conn:
                assert await consume_email_challenge(conn, actor["id"], 1, PURPOSE_CURRENT, token) is None

    asyncio.run(_scenario(check))


def test_ambiguously_accepted_invitation_keeps_unknown_send_outcome_and_live_token(monkeypatch):
    children = _deadline(monkeypatch)

    async def check(owner, pools, actor):
        await _activate(owner)
        async with pools.control.connection() as conn:
            invitation_id, token = await issue_invitation_record(conn, dict(actor, auth_version=1), "invitee@example.invalid")
        async with LocalRelay() as relay:
            config = auth_config(TEST_DB, smtp_host="127.0.0.1", smtp_port=relay.port,
                                 smtp_security="none", email_from="odograph@example.invalid", app_url=APP_URL)
            app = _app(pools, config=config)
            request = SimpleNamespace(app=app, state=SimpleNamespace())
            outcome = await admin._send_invitation_email(
                request, dict(actor, auth_version=1), invitation_id, "invitee@example.invalid", token,
            )
            await app.state.security_mail.drain()
            await _reaped(relay, children)
            assert outcome == "unknown"
            assert token in relay.messages[0].get_content()
            async with owner.connection() as conn:
                assert await (await conn.execute(
                    "SELECT consumed_at,revoked_at FROM invitations WHERE id=%s", (invitation_id,),
                )).fetchone() == (None, None)

    asyncio.run(_scenario(check))


@pytest.mark.capacity_contract
def test_cancelled_security_waiter_keeps_helper_slot_and_purge_lease_until_actual_send_cancelled(monkeypatch):
    children = _deadline(monkeypatch, 10)

    async def check(owner, pools, actor):
        target = await _activate(owner)
        capacity = AdmissionManager()
        control = ManagedPool(pools.control, capacity, "control")
        admission = SecurityMailAdmission(limit=1, capacity=capacity)
        async with LocalRelay() as relay:
            mailer = _mailer(relay)

            @asynccontextmanager
            async def admit():
                async with control_connection(control, lane="mail") as conn:
                    async with conn.transaction():
                        row = await (await conn.execute("SELECT 1 FROM accounts WHERE id=%s AND is_enabled FOR SHARE", (target,))).fetchone()
                        yield row is not None

            waiter = asyncio.create_task(admission.send(
                mailer, mailer.compose("Test", "Body"), admit=admit,
                lease=lambda: external_account_work(control, target),
            ))
            try:
                await asyncio.wait_for(relay.entered.wait(), 5)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert children[0].returncode is None
                assert admission._slots.locked()
                assert capacity.snapshot()["leases"] == 1
                async with control_connection(control, lane="lifecycle") as conn:
                    await request_account_deletion(conn, actor, target, email="member@example.invalid", acknowledge=True)
                await _elapsed(owner, target)
                async with control_connection(control, lane="lifecycle") as conn:
                    with pytest.raises(AccountLifecycleUnavailable):
                        await purge_account(conn, actor, target, email="member@example.invalid", confirm=True, verified_password_hash=actor["password_hash"])
                # Cancel actual admitted work, as shutdown can, instead of its HTTP waiter.
                for task in tuple(admission._tasks):
                    task.cancel()
                await admission.drain()
                await _reaped(relay, children)
                assert not admission._tasks and not admission._slots.locked()
                assert capacity.snapshot()["leases"] == 0
                assert capacity.snapshot()["mail"]["active"] == 0
                async with control_connection(control, lane="lifecycle") as conn:
                    assert await purge_account(conn, actor, target, email="member@example.invalid", confirm=True, verified_password_hash=actor["password_hash"]) == "purged"
            finally:
                for task in tuple(admission._tasks):
                    task.cancel()
                await admission.drain()

    asyncio.run(_scenario(check))


@pytest.mark.parametrize("barrier", ["preference", "recipient", "disable"])
def test_actual_digest_cancellation_reaps_before_barrier_and_prevents_stale_restart(monkeypatch, barrier):
    children = _deadline(monkeypatch, 10)

    async def check(pool):
        async with LocalRelay() as relay:
            mailer = _mailer(relay)
            worker = _worker(pool, mailer, monthly=True)
            task = asyncio.create_task(worker.run_once(FILING_NOW))
            await asyncio.wait_for(relay.entered.wait(), 5)
            principal = pool.principal
            raw_pool = pool.admin_pool
            async with raw_pool.connection() as conn:
                table = "accounts" if barrier == "disable" else "account_settings"
                column = "id" if barrier == "disable" else "account_id"
                with pytest.raises(errors.LockNotAvailable):
                    async with conn.transaction():
                        await conn.execute(f"SELECT 1 FROM {table} WHERE {column}=%s FOR UPDATE NOWAIT", (principal.account_id,))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await _reaped(relay, children)
            async with raw_pool.connection() as conn:
                if barrier == "disable":
                    await conn.execute("UPDATE accounts SET is_enabled=false WHERE id=%s", (principal.account_id,))
                else:
                    change = "email_monthly_summary=false" if barrier == "preference" else "email_to='changed@example.invalid'"
                    await conn.execute(f"UPDATE account_settings SET {change} WHERE account_id=%s", (principal.account_id,))
            await _worker(pool, mailer, monthly=True).run_once(FILING_NOW + timedelta(hours=1))
            assert relay.sessions == 1
            async with raw_pool.connection() as conn:
                assert await (await conn.execute("SELECT count(*) FROM email_deliveries")).fetchone() == (0,)

    asyncio.run(_with_pool(check))


@pytest.mark.capacity_contract
def test_one_background_and_two_security_sends_share_three_helpers_and_release_all_owners(monkeypatch):
    children = _deadline(monkeypatch, 10)

    async def check(owner, pools, actor):
        target = await _activate(owner)
        capacity = AdmissionManager()
        control = ManagedPool(pools.control, capacity, "control")
        runtime = ManagedPool(pools.runtime, capacity, "runtime")
        admission = SecurityMailAdmission(capacity=capacity)
        async with LocalRelay("all_quit") as relay:
            mailer = _mailer(relay)

            async def background():
                principal = AccountPrincipal(target, True, 1)
                async with capacity.operation("background", principal=principal):
                    async with external_account_work(control, target):
                        async with AccountPool(runtime, principal).connection():
                            await mailer.send(mailer.compose("Background", "Body"))

            async def security():
                return await admission.send(
                    mailer, mailer.compose("Security", "Body"),
                    lease=lambda: external_account_work(control, actor["id"]),
                )

            background_task = asyncio.create_task(background())
            security_tasks = [asyncio.create_task(security()) for _ in range(2)]
            try:
                async with asyncio.timeout(5):
                    while len(relay.messages) < 3:
                        await asyncio.sleep(.01)
                assert len(children) == 3
                assert all(child.returncode is None for child in children)
                assert capacity.snapshot()["background"]["active"] == 1
                assert capacity.snapshot()["mail"]["active"] == 2
                assert capacity.snapshot()["leases"] == 3
                assert not await admission.send(mailer, mailer.compose("Refused", "Body"))
                assert len(children) == relay.sessions == 3
                # Cancelling shielded HTTP waiters leaves both security owners alive.
                for task in security_tasks:
                    task.cancel()
                await asyncio.gather(*security_tasks, return_exceptions=True)
                assert len(admission._tasks) == 2
                background_task.cancel()
                for task in tuple(admission._tasks):
                    task.cancel()
                await asyncio.gather(background_task, return_exceptions=True)
                await admission.drain()
                assert all(child.returncode is not None for child in children)
                async with asyncio.timeout(2):
                    while relay._tasks:
                        await asyncio.sleep(.01)
                snapshot = capacity.snapshot()
                assert snapshot["background"]["active"] == snapshot["mail"]["active"] == snapshot["leases"] == 0
                assert not admission._tasks
            finally:
                background_task.cancel()
                for task in security_tasks + list(admission._tasks):
                    task.cancel()
                await asyncio.gather(background_task, *security_tasks, return_exceptions=True)
                await admission.drain()

    asyncio.run(_scenario(check))
