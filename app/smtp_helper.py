"""One SMTP attempt, with independent deadline and parent-lifetime enforcement.

This file is executed directly by an isolated Python interpreter. Keep imports
stdlib-only: a helper must never load serving configuration or database state.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import select
import signal
import smtplib
import ssl
import struct
import sys
import threading
import time
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

CHUNK_SIZE = 64 * 1024
READY = b"SMTP1 READY\n"
SMTP_TIMEOUT_S = 15.0


def smtp_transport(mailer, message, state=None):
    """Complete a verified SMTP session, including QUIT, or raise."""
    if state is None:
        state = {}
    state["phase"] = "tls"
    context = (ssl._create_unverified_context() if mailer.tls_insecure
               else ssl.create_default_context())
    state["phase"] = "connect"
    if mailer.security == "ssl":
        session = smtplib.SMTP_SSL(mailer.host, mailer.port, context=context,
                                  timeout=SMTP_TIMEOUT_S)
    elif mailer.security in ("starttls", "none"):
        session = smtplib.SMTP(mailer.host, mailer.port, timeout=SMTP_TIMEOUT_S)
    else:
        raise ValueError(f"unknown SMTP_SECURITY {mailer.security!r}")
    with session as smtp:
        if mailer.security == "starttls":
            state["phase"] = "starttls"
            smtp.starttls(context=context)
        if mailer.username:
            state["phase"] = "auth"
            smtp.login(mailer.username, mailer.password)
        state["phase"] = "data"
        smtp.send_message(message)
        state["phase"] = "quit"


def _watch_parent(lifetime_fd, deadline):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os._exit(70)
        readable, _, _ = select.select([lifetime_fd], [], [], remaining)
        if readable and not os.read(lifetime_fd, 1):
            os._exit(71)


def _guard_parent(parent_pid):
    if os.getppid() != parent_pid:
        os._exit(71)
    if sys.platform == "linux":
        # PDEATHSIG belongs to the thread that created this process. The parent
        # launches on its event-loop thread, never from its executor.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(72)
    if os.getppid() != parent_pid:
        os._exit(71)


def _read_exact(size):
    chunks = []
    while size:
        chunk = os.read(0, min(size, CHUNK_SIZE))
        if not chunk:
            raise ValueError("incomplete payload")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _failure(exc):
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipients"
    if isinstance(exc, smtplib.SMTPException):
        return "smtp"
    if isinstance(exc, OSError):
        return "io"
    return "protocol"


def main():
    deadline, parent_pid, lifetime_fd = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    _guard_parent(parent_pid)
    threading.Thread(target=_watch_parent, args=(lifetime_fd, deadline), daemon=True).start()
    if time.monotonic() >= deadline or os.getppid() != parent_pid:
        os._exit(71)
    os.write(1, READY)
    state = {"phase": "input"}
    try:
        size = struct.unpack("!Q", _read_exact(8))[0]
        payload = json.loads(_read_exact(size))
        message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload.pop("message")))
        if time.monotonic() >= deadline or os.getppid() != parent_pid:
            os._exit(70)
        smtp_transport(SimpleNamespace(**payload), message, state)
    except Exception as exc:
        # No server text, message, recipient, hostname or credential escapes.
        os.write(1, b"FAIL " + _failure(exc).encode("ascii") + b" "
                 + state["phase"].encode("ascii") + b"\n")
        return 1
    os.write(1, b"OK\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
