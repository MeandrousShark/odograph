"""Unit tests for app/mailer.py: compose(),
the injectable transport (capture + raise-propagates), and the default
smtp_transport's SMTP_SECURITY mode selection / TLS context verification.
"""
from __future__ import annotations

import asyncio
import ssl
from unittest.mock import MagicMock, patch

import pytest

from app.mailer import Mailer, smtp_transport


def _mailer(**overrides) -> Mailer:
    defaults = dict(
        host="smtp.example.com", port=587, username="", password="",
        security="starttls", tls_insecure=False,
        from_addr="odograph@example.com", to_addr="you@example.com",
    )
    defaults.update(overrides)
    return Mailer(**defaults)


def test_compose_sets_from_to_subject_and_body():
    mailer = _mailer()
    message = mailer.compose("Odograph: June summary", "Business miles: 120.0")
    assert message["From"] == "odograph@example.com"
    assert message["To"] == "you@example.com"
    assert message["Subject"] == "Odograph: June summary"
    assert message.get_content().strip() == "Business miles: 120.0"


def test_send_runs_the_injected_transport_with_the_composed_message():
    captured = {}

    def fake_transport(mailer, message):
        captured["mailer"] = mailer
        captured["message"] = message

    mailer = _mailer(transport=fake_transport)
    message = mailer.compose("subject", "body")
    asyncio.run(mailer.send(message))
    assert captured["mailer"] is mailer
    assert captured["message"] is message


def test_send_propagates_a_raising_transport():
    def failing_transport(mailer, message):
        raise RuntimeError("relay unreachable")

    mailer = _mailer(transport=failing_transport)
    with pytest.raises(RuntimeError, match="relay unreachable"):
        asyncio.run(mailer.send(mailer.compose("subject", "body")))


def _fake_smtp_cm():
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.__exit__.return_value = False
    return smtp


def test_starttls_security_uses_smtp_and_calls_starttls_with_verified_context():
    mailer = _mailer(security="starttls", username="user", password="secret")
    message = mailer.compose("subject", "body")
    smtp = _fake_smtp_cm()
    with patch("app.mailer.smtplib.SMTP", return_value=smtp) as smtp_cls, \
         patch("app.mailer.smtplib.SMTP_SSL") as smtp_ssl_cls:
        smtp_transport(mailer, message)
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15.0)
    smtp_ssl_cls.assert_not_called()
    assert smtp.starttls.call_count == 1
    context = smtp.starttls.call_args.kwargs["context"]
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    smtp.login.assert_called_once_with("user", "secret")
    smtp.send_message.assert_called_once_with(message)


def test_ssl_security_uses_smtp_ssl_and_never_calls_starttls():
    mailer = _mailer(security="ssl", port=465)
    message = mailer.compose("subject", "body")
    smtp = _fake_smtp_cm()
    with patch("app.mailer.smtplib.SMTP") as smtp_cls, \
         patch("app.mailer.smtplib.SMTP_SSL", return_value=smtp) as smtp_ssl_cls:
        smtp_transport(mailer, message)
    smtp_cls.assert_not_called()
    smtp_ssl_cls.assert_called_once()
    args, kwargs = smtp_ssl_cls.call_args
    assert args == ("smtp.example.com", 465)
    assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    smtp.login.assert_not_called()  # no username configured
    smtp.send_message.assert_called_once_with(message)


def test_none_security_uses_bare_smtp_with_no_starttls():
    mailer = _mailer(security="none")
    message = mailer.compose("subject", "body")
    smtp = _fake_smtp_cm()
    with patch("app.mailer.smtplib.SMTP", return_value=smtp) as smtp_cls:
        smtp_transport(mailer, message)
    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15.0)
    smtp.starttls.assert_not_called()
    smtp.send_message.assert_called_once_with(message)


def test_tls_insecure_swaps_in_an_unverified_context():
    mailer = _mailer(security="starttls", tls_insecure=True)
    message = mailer.compose("subject", "body")
    smtp = _fake_smtp_cm()
    with patch("app.mailer.smtplib.SMTP", return_value=smtp):
        smtp_transport(mailer, message)
    context = smtp.starttls.call_args.kwargs["context"]
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_unknown_security_mode_raises():
    mailer = _mailer(security="bogus")
    with pytest.raises(ValueError, match="bogus"):
        smtp_transport(mailer, mailer.compose("subject", "body"))


def test_cancelled_serialization_drains_actual_thread_before_return(monkeypatch):
    import threading
    from app import mailer as mailer_module

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    launched = []

    class SlowMessage:
        def as_bytes(self):
            entered.set()
            release.wait(3)
            finished.set()
            return b"From: a@example.test\nTo: b@example.test\n\nbody\n"

    async def capture(payload):
        launched.append(payload)

    monkeypatch.setattr(mailer_module, "send_payload", capture)

    async def scenario():
        task = asyncio.create_task(_mailer().send(SlowMessage()))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
                task.cancel()
            assert not task.done()
            assert not finished.is_set()
            assert not launched
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
            assert not launched
        finally:
            release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("parents", [(41, 42), (42,)])
def test_linux_parent_identity_is_checked_around_pdeathsig(monkeypatch, parents):
    from app import smtp_helper

    class GuardExit(BaseException):
        pass

    seen = []
    identities = iter(parents)
    libc = MagicMock()
    libc.prctl.return_value = 0
    monkeypatch.setattr(smtp_helper.sys, "platform", "linux")
    monkeypatch.setattr(smtp_helper.os, "getppid", lambda: next(identities))
    monkeypatch.setattr(smtp_helper.ctypes, "CDLL", lambda *args, **kwargs: libc)

    def exit_guard(code):
        seen.append(code)
        raise GuardExit

    monkeypatch.setattr(smtp_helper.os, "_exit", exit_guard)
    with pytest.raises(GuardExit):
        smtp_helper._guard_parent(41)
    assert seen == [71]
    assert libc.prctl.call_count == (1 if len(parents) == 2 else 0)


def test_linux_pdeathsig_installation_failure_is_fail_closed(monkeypatch):
    from app import smtp_helper

    class GuardExit(BaseException):
        pass

    libc = MagicMock()
    libc.prctl.return_value = -1
    monkeypatch.setattr(smtp_helper.sys, "platform", "linux")
    monkeypatch.setattr(smtp_helper.os, "getppid", lambda: 41)
    monkeypatch.setattr(smtp_helper.ctypes, "CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr(smtp_helper.os, "_exit", lambda code: (_ for _ in ()).throw(GuardExit(code)))
    with pytest.raises(GuardExit) as failure:
        smtp_helper._guard_parent(41)
    assert failure.value.args == (72,)
