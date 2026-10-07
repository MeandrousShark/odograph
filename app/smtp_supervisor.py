"""Own one SMTP helper until its confirmed exit, including cancelled launches."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import smtplib
import struct
import sys
import time

from app.smtp_helper import CHUNK_SIZE, READY

TRANSPORT_TIMEOUT_S = 30.0
TERMINATE_GRACE_S = 1.0
RESULT_LIMIT = 1024
HELPER_PATH = Path(__file__).with_name("smtp_helper.py")
_logger = logging.getLogger(__name__)


class SMTPTransportError(smtplib.SMTPException):
    """A bounded, secret-free helper failure, with safe supervision evidence."""

    def __init__(self, message, *, failure="protocol", phase="transport"):
        super().__init__(message)
        self.failure = failure
        self.phase = phase
        self.duration = 0.0
        self.cleanup_confirmed = False


class SMTPTransportTimeout(SMTPTransportError, TimeoutError):
    """The whole transport deadline expired; delivery may be ambiguous."""


def serialize_payload(mailer, message):
    """Serialization is owned blocking preparation, before the transport clock."""
    payload = {name: getattr(mailer, name) for name in (
        "host", "port", "username", "password", "security", "tls_insecure")}
    payload["message"] = base64.b64encode(message.as_bytes()).decode("ascii")
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _confirmed_exit(process):
    warned = False
    while True:
        try:
            await process.wait()
            if process.returncode is not None:
                return
        except Exception:
            if not warned:
                _logger.error("SMTP helper reap unconfirmed; send remains owned")
                warned = True
        await asyncio.sleep(.1)


async def _reap(spawn, process):
    if process is None:
        try:
            process = await spawn
        except Exception:
            return
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    wait = asyncio.create_task(_confirmed_exit(process))
    if process.returncode is None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(wait), TERMINATE_GRACE_S)
        except TimeoutError:
            try:
                process.kill()
            except OSError:
                pass
    # No elapsed-time shortcut: an unconfirmed exit continues to own the send.
    await wait


async def _drain_cleanup(cleanup):
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True
            if cleanup.done():
                cleanup.result()
                return cancelled


async def send_payload(payload):
    started = time.monotonic()
    deadline = started + TRANSPORT_TIMEOUT_S
    lifetime_read, lifetime_write = os.pipe()
    process = None
    spawn = None
    failure = None
    phase = "launch"
    try:
        # create_subprocess_exec runs on the event-loop thread; no preexec_fn,
        # shell, inherited environment, extra descriptors or unrestricted logs.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-I", str(HELPER_PATH), str(deadline), str(os.getpid()),
            str(lifetime_read), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={}, close_fds=True, pass_fds=(lifetime_read,), limit=RESULT_LIMIT))
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            process = await asyncio.shield(spawn)
            os.close(lifetime_read)
            lifetime_read = None
            phase = "readiness"
            if await process.stdout.readexactly(len(READY)) != READY:
                raise SMTPTransportError("SMTP helper readiness failed", failure="readiness", phase=phase)
            phase = "payload"
            process.stdin.write(struct.pack("!Q", len(payload)))
            await process.stdin.drain()
            for offset in range(0, len(payload), CHUNK_SIZE):
                process.stdin.write(payload[offset:offset + CHUNK_SIZE])
                await process.stdin.drain()
            process.stdin.close()
            # Read only a bounded result, and require EOF as well as confirmed
            # clean exit. DATA alone is never sufficient delivery evidence.
            phase = "transport"
            result = bytearray()
            while len(result) <= RESULT_LIMIT:
                chunk = await process.stdout.read(RESULT_LIMIT + 1 - len(result))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > RESULT_LIMIT:
                raise SMTPTransportError("SMTP helper result exceeded limit", failure="result", phase=phase)
            phase = "exit"
            returncode = await process.wait()
            if result == b"OK\n" and returncode == 0 and time.monotonic() < deadline:
                return
            if returncode == 70:
                raise SMTPTransportTimeout("SMTP transport deadline expired", failure="deadline", phase=phase)
            safe_failures = {
                b"FAIL " + code + b" " + step + b"\n": (code.decode("ascii"), step.decode("ascii"))
                for code in (b"tls", b"auth", b"recipients", b"smtp", b"io", b"protocol")
                for step in (b"input", b"tls", b"connect", b"starttls", b"auth", b"data", b"quit")}
            if bytes(result) in safe_failures:
                code, step = safe_failures[bytes(result)]
                raise SMTPTransportError("SMTP helper failed: " + code, failure=code, phase=step)
            raise SMTPTransportError("SMTP helper failed without a valid result", failure="result", phase=phase)
    except SMTPTransportError as exc:
        failure = exc
        raise
    except TimeoutError:
        failure = SMTPTransportTimeout("SMTP transport deadline expired", failure="deadline", phase=phase)
        raise failure from None
    except Exception:
        failure = SMTPTransportError("SMTP helper communication failed", failure="communication", phase=phase)
        raise failure from None
    finally:
        # Closing this independent pipe also stops a late-starting helper. Keep
        # the read descriptor until the spawn settles so it cannot be reused.
        os.close(lifetime_write)
        if spawn is not None:
            cleanup = asyncio.create_task(_reap(spawn, process))
            cleanup_cancelled = await _drain_cleanup(cleanup)
        else:
            cleanup_cancelled = False
        if lifetime_read is not None:
            os.close(lifetime_read)
        if failure is not None:
            failure.duration = time.monotonic() - started
            failure.cleanup_confirmed = True
        if cleanup_cancelled:
            raise asyncio.CancelledError
