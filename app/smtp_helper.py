"""One SMTP attempt, with independent deadline and parent-lifetime enforcement.

This file is executed directly by an isolated Python interpreter. Keep imports
stdlib-only: a helper must never load serving configuration or database state.
"""
from __future__ import annotations

import os
import sys
import resource

MEMORY_BYTES = 256 * 1024 * 1024
_PREPARED_ENTRY = __name__ == '__main__' and len(sys.argv) > 4 and sys.argv[4] == 'fd2'
if _PREPARED_ENTRY and sys.platform == 'linux':
    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    _ceiling = MEMORY_BYTES if _hard == resource.RLIM_INFINITY else min(MEMORY_BYTES, _hard)
    resource.setrlimit(resource.RLIMIT_AS, (_ceiling, _ceiling))

try:
    import errno
    if _PREPARED_ENTRY:
        import encodings.idna
    import base64
    import ctypes
    import json
    import select
    import signal
    import smtplib
    import ssl
    import struct
    import threading
    import time
    from email import policy
    from email.parser import BytesParser
    from types import SimpleNamespace
except MemoryError:
    if _PREPARED_ENTRY:
        os._exit(73)
    raise
except OSError as exc:
    if _PREPARED_ENTRY and exc.errno == errno.ENOMEM:
        os._exit(73)
    raise

CHUNK_SIZE = 64 * 1024
READY = b"SMTP1 READY\n"
PREPARED_READY = b"SMTPFD2 READY\n"
SMTP_TIMEOUT_S = 15.0
_RESOURCE_RESULTS = {phase: b'FAIL resource ' + phase.encode('ascii') + b'\n'
                     for phase in ('input', 'tls', 'connect', 'starttls', 'auth', 'data', 'quit')}


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


def _watch_parent(lifetime_fd, deadline, memory_stop=False):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os._exit(70)
        if memory_stop and sys.platform == 'darwin' and resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > MEMORY_BYTES:
            os._exit(73)
        readable, _, _ = select.select([lifetime_fd], [], [], min(remaining, .01) if memory_stop else remaining)
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



def _descriptor_json(fd):
    import fcntl
    import stat
    if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError('prepared SMTP inputs require read-only descriptors')
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError('prepared SMTP input is not a regular file')
    # Complete parsing is isolated under the selected address-space authority.
    with os.fdopen(os.dup(fd), 'r', encoding='utf8') as source:
        return json.load(source)


def prepared_transport(config, envelope, mime_fd, mime_utf8_fd, state):
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.mime_preparation import send_spool
    state['phase'] = 'tls'
    context = (ssl._create_unverified_context() if config.tls_insecure
               else ssl.create_default_context())
    state['phase'] = 'connect'
    if config.security == 'ssl':
        session = smtplib.SMTP_SSL(config.host, config.port, context=context,
                                  timeout=SMTP_TIMEOUT_S)
    elif config.security in ('starttls', 'none'):
        session = smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT_S)
    else:
        raise ValueError('unknown SMTP security mode')
    with session as smtp:
        if config.security == 'starttls':
            state['phase'] = 'starttls'
            smtp.starttls(context=context)
        if config.username:
            state['phase'] = 'auth'
            smtp.login(config.username, config.password)
        state['phase'] = 'data'
        selected = mime_utf8_fd if envelope['international'] else mime_fd
        with os.fdopen(os.dup(selected), 'rb') as source:
            send_spool(smtp, envelope['sender'], envelope['recipients'], source,
                       envelope['international'])
        state['phase'] = 'quit'

def main():
    deadline, parent_pid, lifetime_fd = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    prepared = len(sys.argv) > 4 and sys.argv[4] == 'fd2'
    _guard_parent(parent_pid)
    threading.Thread(target=_watch_parent, args=(lifetime_fd, deadline, prepared), daemon=True).start()
    if time.monotonic() >= deadline or os.getppid() != parent_pid:
        os._exit(71)
    os.write(1, PREPARED_READY if prepared else READY)
    state = {"phase": "input"}
    try:
        if prepared:
            # Descriptor numbers follow readiness on the private control pipe.
            config_fd, envelope_fd, mime_fd, mime_utf8_fd = struct.unpack('!4i', _read_exact(16))
            config = _descriptor_json(config_fd)
            envelope = _descriptor_json(envelope_fd)
            if time.monotonic() >= deadline or os.getppid() != parent_pid:
                os._exit(70)
            prepared_transport(SimpleNamespace(**config), envelope, mime_fd, mime_utf8_fd, state)
        else:
            size = struct.unpack("!Q", _read_exact(8))[0]
            payload = json.loads(_read_exact(size))
            message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload.pop("message")))
            if time.monotonic() >= deadline or os.getppid() != parent_pid:
                os._exit(70)
            smtp_transport(SimpleNamespace(**payload), message, state)
    except MemoryError:
        os.write(1, _RESOURCE_RESULTS[state['phase']])
        return 73
    except Exception as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
            os.write(1, _RESOURCE_RESULTS[state['phase']])
            return 73
        # No server text, message, recipient, hostname or credential escapes.
        os.write(1, b"FAIL " + _failure(exc).encode("ascii") + b" "
                 + state["phase"].encode("ascii") + b"\n")
        return 1
    os.write(1, b"OK\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MemoryError:
        if _PREPARED_ENTRY:
            os._exit(73)
        raise
    except OSError as exc:
        if _PREPARED_ENTRY and exc.errno == errno.ENOMEM:
            os._exit(73)
        raise
