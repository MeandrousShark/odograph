"""Reset admission, queueing and delivery without a database or SMTP."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import app.password_reset as reset
from app.config import security_link_base
from app.mailer import Mailer

LINK_BASE = "https://odograph.example.com"


def _mailer(address: str, transport) -> Mailer:
    return Mailer("smtp.example.com", 587, "", "", "starttls", False,
                  "odograph@example.com", address, transport=transport)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_attempt_limiter_counts_every_attempt_in_its_window():
    clock = _Clock()
    limiter = reset.AttemptLimiter("test", 3, 60, clock=clock)
    assert [limiter.allow("client") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("other")
    clock.now += 61
    assert limiter.allow("client")


def test_attempt_limiter_refuses_new_keys_at_capacity_without_evicting(caplog):
    clock = _Clock()
    limiter = reset.AttemptLimiter("test", 2, 60, max_keys=2, clock=clock)
    assert limiter.allow("secret-one@example.com")
    assert limiter.allow("secret-two@example.com")
    with caplog.at_level(logging.WARNING, logger="app.password_reset"):
        assert not limiter.allow("secret-three@example.com")
        assert not limiter.allow("secret-four@example.com")
    assert len(caplog.records) == 1
    assert "secret" not in caplog.text and "example.com" not in caplog.text
    # Existing keys keep their limits and remaining allowance.
    assert limiter.allow("secret-one@example.com")
    assert not limiter.allow("secret-one@example.com")
    clock.now += 61
    assert limiter.allow("secret-three@example.com")


@pytest.mark.parametrize("value,expected", [
    ("https://odograph.example.com", "https://odograph.example.com"),
    ("https://odograph.example.com/", "https://odograph.example.com"),
    ("http://127.0.0.1:8078", "http://127.0.0.1:8078"),
    ("https://example.com/odograph", "https://example.com/odograph"),
    ("", ""),
    ("odograph.example.com", ""),
    ("ftp://odograph.example.com", ""),
    ("https://user:pw@odograph.example.com", ""),
    ("https://@odograph.example.com", ""),
    ("https://odograph.example.com?next=/", ""),
    ("https://odograph.example.com#top", ""),
    ("https://odograph.example.com:notaport", ""),
    ("https://odo graph.example.com", ""),
    ("https:///path-only", ""),
])
def test_security_link_base_accepts_only_absolute_http_bases(value, expected):
    assert security_link_base(value) == expected


def test_admission_holds_its_slot_until_the_transport_thread_finishes():
    release = threading.Event()
    started = threading.Event()
    sent = []

    def blocking(mailer, message):
        started.set()
        release.wait(5)
        sent.append(message["To"])

    async def run():
        admission = reset.SecurityMailAdmission(limit=1)
        mailer = _mailer("a@example.com", blocking)
        caller = asyncio.create_task(admission.send(mailer, mailer.compose("s", "b")))
        await asyncio.to_thread(started.wait, 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        second = asyncio.create_task(admission.send(mailer, mailer.compose("s", "b")))
        await asyncio.sleep(0.05)
        assert not second.done()
        release.set()
        await second
        await admission.drain()
        assert sent == ["a@example.com", "a@example.com"]

    asyncio.run(run())


class _FakeDb:
    def __init__(self, monkeypatch, *, address="verified@example.com", usable=True):
        self.address = address
        self.usable = usable
        self.issued = []
        self.revoked = []

        @asynccontextmanager
        async def connection(pool):
            yield _FakeConn()

        async def issue(conn, token, *, initiator, email=None, account_id=None):
            self.issued.append((token, initiator, email, account_id))
            return self.address

        async def usable_check(conn, token, *, lock_account=False):
            assert lock_account
            return self.usable

        async def revoke(conn, token):
            self.revoked.append(token)

        monkeypatch.setattr(reset, "control_connection", connection)
        monkeypatch.setattr(reset, "issue_password_reset", issue)
        monkeypatch.setattr(reset, "password_reset_usable", usable_check)
        monkeypatch.setattr(reset, "revoke_password_reset", revoke)


class _FakeConn:
    @asynccontextmanager
    async def transaction(self):
        yield


def _queue(transport, **kwargs):
    admission = reset.SecurityMailAdmission()
    return reset.RecoveryQueue(object(), LINK_BASE, lambda address: _mailer(address, transport),
                               admission, **kwargs), admission


def test_public_request_delivers_only_to_the_stored_verified_address(monkeypatch):
    db = _FakeDb(monkeypatch)
    messages = []

    async def run():
        queue, _ = _queue(lambda mailer, message: messages.append(message))
        await queue.start()
        assert queue.submit_public("typed@example.com")
        for _ in range(100):
            if messages:
                break
            await asyncio.sleep(0.01)
        await queue.stop()

    asyncio.run(run())
    token, initiator, email, account_id = db.issued[0]
    assert (initiator, email, account_id) == ("public", "typed@example.com", None)
    assert len(messages) == 1
    message = messages[0]
    assert message["To"] == "verified@example.com"
    body = message.get_content()
    assert f"{LINK_BASE}/reset-password#token={token}" in body
    assert f"?token={token}" not in body and f"/{token}" not in body
    assert db.revoked == []


def test_queue_coalesces_pending_identifiers_and_refuses_when_full_or_closed(monkeypatch):
    _FakeDb(monkeypatch)

    async def run():
        queue, _ = _queue(lambda mailer, message: None, max_pending=2)
        assert not queue.submit_public("a@example.com")  # not started
        queue._closed = False  # accept without running workers
        assert queue.submit_public("a@example.com")
        assert queue.submit_public("a@example.com")
        assert queue._queue.qsize() == 1
        assert queue.submit_admin(7)
        assert not queue.submit_public("b@example.com")
        queue._closed = True
        assert not queue.submit_admin(8)

    asyncio.run(run())


def test_send_failure_revokes_the_exact_token_without_logging_secrets(monkeypatch, caplog):
    db = _FakeDb(monkeypatch)
    attempted = []

    def failing(mailer, message):
        attempted.append(message)
        raise OSError("smtp down for verified@example.com")

    async def run():
        queue, _ = _queue(failing)
        await queue.start()
        queue.submit_public("typed@example.com")
        for _ in range(100):
            if db.revoked:
                break
            await asyncio.sleep(0.01)
        await queue.stop()

    with caplog.at_level(logging.WARNING, logger="app.password_reset"):
        asyncio.run(run())
    token = db.issued[0][0]
    assert attempted and db.revoked == [token]
    assert token not in caplog.text
    assert "example.com" not in caplog.text
    assert "OSError" in caplog.text


def test_unusable_or_unsafe_reset_is_not_sent(monkeypatch):
    sent = []
    db = _FakeDb(monkeypatch, usable=False)

    async def run(queue):
        await queue.start()
        queue.submit_admin(7)
        for _ in range(50):
            if db.issued:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await queue.stop()

    asyncio.run(run(_queue(lambda mailer, message: sent.append(message))[0]))
    assert db.issued[0][1:] == ("admin", None, 7) and sent == []

    db = _FakeDb(monkeypatch, address="victim@example.com\nBcc: other@example.com")
    asyncio.run(run(_queue(lambda mailer, message: sent.append(message))[0]))
    assert sent == [] and db.revoked == [db.issued[0][0]]


def test_stop_finishes_an_in_flight_send_before_returning(monkeypatch):
    db = _FakeDb(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    finished = []

    def blocking(mailer, message):
        started.set()
        release.wait(5)
        finished.append(True)
        raise OSError("late failure")

    async def run():
        queue, admission = _queue(blocking)
        await queue.start()
        queue.submit_public("typed@example.com")
        await asyncio.to_thread(started.wait, 5)
        stopping = asyncio.create_task(queue.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        release.set()
        await stopping
        assert finished == [True]
        assert admission._tasks == set()
        assert not queue.submit_public("again@example.com")

    asyncio.run(run())
    assert db.revoked == [db.issued[0][0]]


def test_blocked_delivery_and_full_queue_never_delay_submission(monkeypatch):
    _FakeDb(monkeypatch)
    release = threading.Event()
    both_sending = threading.Barrier(3)

    def blocking(mailer, message):
        both_sending.wait(5)
        release.wait(5)

    async def run():
        queue, _ = _queue(blocking, max_pending=2)
        await queue.start()
        assert queue.submit_public("one@example.com")
        assert queue.submit_public("two@example.com")
        await asyncio.to_thread(both_sending.wait, 5)
        loop = asyncio.get_running_loop()
        started = loop.time()
        results = [queue.submit_public(f"{index}@example.com") for index in range(5)]
        assert loop.time() - started < 0.05
        assert results == [True, True, False, False, False]
        release.set()
        await queue.stop()
        # A restarted process starts with an empty queue and no tokens.
        restarted, _ = _queue(lambda mailer, message: None)
        assert restarted._queue.qsize() == 0

    asyncio.run(run())


def test_proofless_host_reset_is_reachable_only_from_the_host_cli():
    # The role contract names the SQL function only to grant and validate it.
    reference = re.compile(r"(?<!public\.)\bhost_reset_password\b")
    app_dir = Path(reset.__file__).parent
    callers = sorted(
        str(path.relative_to(app_dir)) for path in app_dir.rglob("*.py")
        if reference.search(path.read_text())
    )
    assert callers == ["manage_account.py", "password_reset.py"]
