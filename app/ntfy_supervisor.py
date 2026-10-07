"""Own prepared ntfy transport through deadline, cancellation and confirmed reap."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import struct
import sys
import time

import httpx

from app.capacity import owned_thread
from app.ntfy_helper import READY
from app.ntfy_cookies import HEADER, FRAME_BYTES, COOKIE_RECORD_BYTES, apply_cookie
from app.provider_http import ProviderResponseTooLarge
from app.smtp_supervisor import _reap, _drain_cleanup

TRANSPORT_SECONDS = 15.0
RESULT_BYTES = 1024
HELPER = Path(__file__).with_name('ntfy_helper.py')


class NtfyTransportError(httpx.TransportError):
    def __init__(self, message, *, failure='protocol', phase='transport'):
        super().__init__(message)
        self.failure = failure
        self.phase = phase
        self.cleanup_confirmed = False


def _failure(result, code):
    phases = ('input', 'client', 'request')
    for phase in phases:
        if code == 73 and result == b'FAIL resource ' + phase.encode() + b'\n':
            return NtfyTransportError('ntfy helper resource stop', failure='resource', phase=phase)
        names = ('HTTPStatusError', 'ProviderResponseTooLarge', 'TimeoutException',
                 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
                 'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError',
                 'LocalProtocolError', 'UnsupportedProtocol', 'ProxyError', 'DecodingError',
                 'InvalidURL', 'ValueError', 'UnicodeEncodeError')
        for name in names:
            if result != b'FAIL ' + name.encode() + b' ' + phase.encode() + b'\n':
                continue
            if name == 'HTTPStatusError':
                request = httpx.Request('POST', 'http://helper.invalid/')
                return httpx.HTTPStatusError('ntfy provider refused delivery', request=request,
                    response=httpx.Response(500, request=request))
            if name == 'ProviderResponseTooLarge':
                return ProviderResponseTooLarge('provider response exceeds byte limit')
            if name == 'ValueError':
                return ValueError('ntfy preparation failed')
            if name == 'UnicodeEncodeError':
                return UnicodeEncodeError('ascii', '', 0, 0, 'ntfy preparation failed')
            return getattr(httpx, name)('ntfy transport failed')
    return NtfyTransportError('ntfy helper failed without a valid result', failure='result')


async def send_prepared(prepared, *, http_client, before_transport=None):
    retained = {}
    def open_files():
        retained['files'] = prepared.open_descriptors()
    try:
        await owned_thread(open_files)
        _, descriptors, guard = retained['files']
        if before_transport is not None:
            await before_transport()
        await _send(descriptors, guard, http_client.cookies.jar)
    finally:
        if 'files' in retained:
            await owned_thread(retained['files'][0].close)


async def _events(stream, jar):
    result = None
    while True:
        try:
            header = await stream.readexactly(HEADER.size)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return result
            raise NtfyTransportError('incomplete ntfy result frame', failure='result') from None
        kind, size = HEADER.unpack(header)
        if kind == b'R':
            if result is not None or size > RESULT_BYTES:
                raise NtfyTransportError('invalid ntfy result frame', failure='result')
            result = await stream.readexactly(size)
            continue
        if kind not in (b'S', b'X') or size > COOKIE_RECORD_BYTES or result is not None:
            raise NtfyTransportError('invalid ntfy cookie frame', failure='result')
        record = bytearray()
        while len(record) < size:
            part_kind, part_size = HEADER.unpack(await stream.readexactly(HEADER.size))
            if part_kind != b'T' or not 0 < part_size <= min(FRAME_BYTES, size - len(record)):
                raise NtfyTransportError('invalid ntfy cookie chunk', failure='result')
            record.extend(await stream.readexactly(part_size))
        # One complete, intrinsically bounded network cookie at a time. The
        # existing jar is the only persistent state holder; no jar/list copy.
        await owned_thread(apply_cookie, jar, kind, record)


async def _send(descriptors, guard, jar):
    deadline = time.monotonic() + TRANSPORT_SECONDS
    lifetime_read, lifetime_write = os.pipe()
    spawn = process = events = None
    failure = None
    try:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, '-I', str(HELPER), str(deadline), str(os.getpid()), str(lifetime_read),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={}, close_fds=True,
            pass_fds=(lifetime_read, *descriptors, guard), limit=FRAME_BYTES))
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            process = await asyncio.shield(spawn)
            os.close(lifetime_read)
            lifetime_read = None
            try:
                ready = await process.stdout.readexactly(len(READY))
            except asyncio.IncompleteReadError:
                code = await process.wait()
                raise NtfyTransportError('ntfy helper readiness failed',
                    failure='resource' if code == 73 else 'readiness', phase='readiness') from None
            if ready != READY:
                raise NtfyTransportError('ntfy helper readiness failed', failure='readiness')
            events = asyncio.create_task(_events(process.stdout, jar))
            process.stdin.write(struct.pack('!3i', *descriptors))
            await process.stdin.drain()
            process.stdin.close()
            result = await asyncio.shield(events)
            code = await process.wait()
            if result == b'OK\n' and code == 0 and time.monotonic() < deadline:
                return
            if code == 70:
                raise httpx.TimeoutException('ntfy transport deadline expired')
            raise _failure(result or b'', code)
    except TimeoutError:
        failure = httpx.TimeoutException('ntfy transport deadline expired')
        raise failure from None
    except BaseException as exc:
        failure = exc
        raise
    finally:
        os.close(lifetime_write)
        async def cleanup():
            if spawn is not None:
                await _reap(spawn, process)
            if events is not None:
                try:
                    await events
                except (Exception, asyncio.CancelledError):
                    # A hard stop may interrupt one record. Completed records
                    # were already applied; a partial record is never applied.
                    if failure is None:
                        raise
        cancelled = await _drain_cleanup(asyncio.create_task(cleanup()))
        if lifetime_read is not None:
            os.close(lifetime_read)
        if isinstance(failure, NtfyTransportError):
            failure.cleanup_confirmed = True
        if cancelled:
            raise asyncio.CancelledError
