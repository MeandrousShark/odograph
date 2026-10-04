"""Authentication finalizers retain admission; completed phases release it."""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

import app.auth as auth
from app.capacity import AdmissionManager, CapacityBusy, current_owner, owned_thread
from app.password_reset import SecurityMailAdmission
from tests.test_email_challenge_routes import ACCOUNT, _app
from tests.test_oidc_methods_routes import _connection, _protected_session, _request

pytestmark = [pytest.mark.unit, pytest.mark.capacity_contract]


def test_held_smtp_does_not_block_unrelated_local_login(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    lanes = []

    async def issue(*args):
        assert current_owner().lane == "auth_interactive"
        return "challenge-token"

    def smtp():
        lanes.append(current_owner().lane)
        started.set()
        release.wait(5)

    class Mailer:
        def __init__(self, *args):
            pass

        def compose(self, *args):
            return "message"

        async def send(self, message):
            await owned_thread(smtp)

    async def account(*args):
        return ACCOUNT.copy()

    def verify(*args):
        lanes.append(current_owner().lane)
        return True

    app = _app(monkeypatch)
    app.state.capacity = AdmissionManager(app.state.config)
    app.state.security_mail = SecurityMailAdmission(capacity=app.state.capacity)
    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "get_account_by_email", account)
    monkeypatch.setattr(auth, "verify_password", verify)
    monkeypatch.setattr(auth, "Mailer", Mailer)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.get("/seed")
            mail = asyncio.create_task(client.post("/settings/account/email/verify/request", data={
                "current_password": "correct", "csrf_token": "csrf-test",
            }))
            try:
                async with asyncio.timeout(2):
                    while not started.is_set():
                        await asyncio.sleep(.001)
                assert app.state.capacity.snapshot()["mail"]["active"] == 1
                assert app.state.capacity.snapshot()["auth_interactive"]["active"] == 0
                login = await client.post("/login/local", data={
                    "email": ACCOUNT["email"], "password": "correct", "csrf_token": "csrf-test",
                })
                assert login.status_code == 303
                assert not mail.done()
                assert lanes == ["mail", "auth_interactive"]
            finally:
                release.set()
                result = await mail
                await app.state.security_mail.drain()
            assert result.status_code == 200
            assert app.state.capacity.snapshot()["auth_interactive"]["active"] == 0
    asyncio.run(run())


def test_cancelled_protected_finalizer_retains_auth_owner_until_real_completion(monkeypatch):
    request, _ = _request(session=_protected_session())
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    manager = AdmissionManager()

    async def consume(*args, **kwargs):
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(auth, "control_connection", _connection)
    monkeypatch.setattr(auth, "consume_oidc_attempt", consume)

    async def run():
        async def finish():
            async with manager.operation("auth_interactive"):
                await auth._finish_protected_attempt(request, "reauth", "reauth.valid", {
                    "nonce": "nonce", "browser_nonce": "browser", "account_id": 7, "auth_version": 2,
                })
        caller = asyncio.create_task(finish())
        await asyncio.wait_for(entered.wait(), 2)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        assert manager.snapshot()["auth_interactive"]["active"] == 1
        with pytest.raises(CapacityBusy):
            async with manager.operation("auth_interactive"):
                pass
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished.is_set()
        assert manager.snapshot()["auth_interactive"]["active"] == 0
        async with manager.operation("auth_interactive"):
            pass
    asyncio.run(run())


def test_saved_password_reports_success_when_followup_identity_read_is_busy(monkeypatch):
    app = _app(monkeypatch)
    app.state.capacity = AdmissionManager(app.state.config)
    committed = []

    async def replace(*args, **kwargs):
        committed.append(True)
        return dict(ACCOUNT, auth_version=4)

    async def busy(*args):
        raise CapacityBusy("identity budget exhausted during refresh")

    monkeypatch.setattr(auth, "replace_password", replace)
    monkeypatch.setattr(auth, "hash_password", lambda value: "new-hash")
    monkeypatch.setattr(auth, "is_current_email_verified", busy)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.get("/seed")
            response = await client.post("/settings/account/password", data={
                "current_password": "correct", "password": "valid-new-password",
                "password_confirm": "valid-new-password", "csrf_token": "csrf-test",
            })
        assert committed == [True]
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/account"
        assert app.state.capacity.snapshot()["auth_interactive"]["active"] == 0
    asyncio.run(run())


def test_cancelled_challenge_preparation_retains_auth_then_transfers_to_mail(monkeypatch):
    issue_started, release_issue = asyncio.Event(), asyncio.Event()
    smtp_started, release_smtp = asyncio.Event(), asyncio.Event()
    app = _app(monkeypatch)
    app.state.capacity = AdmissionManager(app.state.config)
    app.state.security_mail = SecurityMailAdmission(capacity=app.state.capacity)

    async def issue(*args):
        issue_started.set()
        await release_issue.wait()
        return "challenge-token"

    class Mailer:
        def __init__(self, *args):
            pass

        def compose(self, *args):
            return "message"

        async def send(self, message):
            assert current_owner().lane == "mail"
            smtp_started.set()
            await release_smtp.wait()

    monkeypatch.setattr(auth, "issue_email_challenge", issue)
    monkeypatch.setattr(auth, "Mailer", Mailer)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.get("/seed")
            caller = asyncio.create_task(client.post("/settings/account/email/verify/request", data={
                "current_password": "correct", "csrf_token": "csrf-test",
            }))
            try:
                await asyncio.wait_for(issue_started.wait(), 2)
                caller.cancel()
                await asyncio.sleep(0)
                assert not caller.done()
                assert app.state.capacity.snapshot()["auth_interactive"]["active"] == 1
                release_issue.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(caller, 2)
                await asyncio.wait_for(smtp_started.wait(), 2)
                assert app.state.capacity.snapshot()["auth_interactive"]["active"] == 0
                assert app.state.capacity.snapshot()["mail"]["active"] == 1
            finally:
                release_issue.set()
                release_smtp.set()
                await asyncio.gather(caller, return_exceptions=True)
                await app.state.security_mail.drain()
            assert app.state.capacity.snapshot()["mail"]["active"] == 0
    asyncio.run(run())
