"""Own report preparation, isolated helpers and response files through cleanup."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import struct
import sys
import time
from urllib.parse import quote

from psycopg import InterfaceError, OperationalError
from starlette.responses import Response

from app.capacity import CapacityContractError, current_owner, owned_thread
from app.preparation_resources import (
    PreparationBusy, PreparationCleanupUnconfirmed, PreparationResourceError, ResourceBudget, SpoolReservation,
)
from app.smtp_supervisor import _reap

FRAME_BYTES = 65520
PREPARATION_SECONDS = 60.0
READY = b'PREP1 READY\n'
_HEADER = struct.Struct('!cI')
_HELPER = Path(__file__).with_name('preparation_helper.py')
log = logging.getLogger(__name__)


class PreparationError(RuntimeError):
    """Preparation failed without publishing a partial response."""


MEMORY_BYTES = 256 * 1024 * 1024


def _resident_bytes(pid):
    import ctypes
    class TaskInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            'virtual', 'resident', 'user', 'system', 'threads_user', 'threads_system')] + [
            (name, ctypes.c_int32) for name in (
                'policy', 'faults', 'pageins', 'cow', 'sent', 'received', 'sysmach',
                'sysunix', 'csw', 'threads', 'running', 'priority')]
    info = TaskInfo()
    library = ctypes.CDLL('/usr/lib/libproc.dylib')
    size = ctypes.sizeof(info)
    result = library.proc_pidinfo(pid, 4, 0, ctypes.byref(info), size)
    return info.resident if result == size else None


async def _settle(task):
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


async def _retain():
    while True:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            continue


def _json_frame(value):
    # Commands contain scalar projections/references; long text uses T frames.
    nodes = 0
    def validate(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > FRAME_BYTES or depth > 8:
            raise PreparationResourceError('preparation command exceeds frame budget')
        if isinstance(item, str):
            if len(item) > FRAME_BYTES:
                raise PreparationResourceError('preparation text requires streaming')
        elif isinstance(item, dict):
            if len(item) > 256:
                raise PreparationResourceError('preparation command exceeds row budget')
            for key, val in item.items(): validate(key, depth + 1); validate(val, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 256:
                raise PreparationResourceError('preparation command exceeds row budget')
            for val in item: validate(val, depth + 1)
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError('preparation commands require scalar values')
    validate(value)
    result = bytearray()
    for piece in json.JSONEncoder(separators=(',', ':'), allow_nan=False, ensure_ascii=False).iterencode(value):
        encoded = piece.encode('utf8')
        if len(result) + len(encoded) > FRAME_BYTES:
            raise PreparationResourceError('preparation command exceeds frame budget')
        result.extend(encoded)
    return bytes(result)


class PreparationSession:
    def __init__(self, operation, process):
        self.operation, self.process = operation, process
        self.finished = False

    async def _send(self, kind, payload):
        self.operation.check()
        if len(payload) > FRAME_BYTES:
            raise PreparationResourceError('preparation frame exceeds budget')
        self.process.stdin.write(_HEADER.pack(kind, len(payload)))
        self.process.stdin.write(payload)
        try:
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._failure()

    async def send_command(self, command):
        await self._send(b'C', _json_frame(command))

    async def send_text(self, text):
        if not isinstance(text, bytes):
            raise TypeError('preparation text frames require UTF-8 bytes')
        await self._send(b'T', text)

    async def _failure(self):
        code = await self.process.wait()
        if code in (70, 73) or self.operation._resource_stop:
            raise PreparationResourceError('preparation helper resource stop')
        raise PreparationError('preparation helper failed')

    async def response(self):
        self.operation.check()
        try:
            kind, size = _HEADER.unpack(await self.process.stdout.readexactly(_HEADER.size))
            if kind != b'R' or size > FRAME_BYTES:
                raise PreparationError('invalid preparation result')
            raw = await self.process.stdout.readexactly(size)
        except asyncio.IncompleteReadError:
            await self._failure()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise PreparationError('invalid preparation result')
        if result.get('error') == 'resource':
            raise PreparationResourceError('preparation helper resource stop')
        if 'error' in result:
            category = result.get('category')
            if category == 'ValueError':
                raise ValueError('preparation input is invalid')
            if category == 'TypeError':
                raise TypeError('preparation input has an invalid type')
            if category == 'DataError':
                from psycopg import DataError
                raise DataError('preparation input is invalid')
            if category == 'OverflowError':
                raise OverflowError('preparation input overflow')
            if category == 'UnicodeEncodeError':
                raise UnicodeEncodeError('utf8', '', 0, 0, 'preparation conversion failed')
            if category == 'UnicodeDecodeError':
                raise UnicodeDecodeError('utf8', b'', 0, 0, 'preparation conversion failed')
            if category == 'ZoneInfoNotFoundError':
                from zoneinfo import ZoneInfoNotFoundError
                raise ZoneInfoNotFoundError('preparation timezone is invalid')
            if category == 'HeaderParseError':
                from email.errors import HeaderParseError
                raise HeaderParseError('preparation header is invalid')
            if category == 'IllegalCharacterError':
                from openpyxl.utils.exceptions import IllegalCharacterError
                raise IllegalCharacterError('preparation cell is invalid')
            raise PreparationError('preparation helper failed')
        return result

    async def request(self, command):
        await self.send_command(command)
        return await self.response()

    async def finish_input(self):
        if self.finished:
            return
        self.process.stdin.close()
        try:
            await self.process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if await self.process.stdout.read(1):
            raise PreparationError('unexpected preparation result data')
        code = await self.process.wait()
        if code != 0:
            await self._failure()
        self.finished = True


class PreparationOperation:
    def __init__(self, *, spool_root=None, timeout_s=PREPARATION_SECONDS):
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= PREPARATION_SECONDS:
            raise ValueError('preparation deadline must be positive and at most 60 seconds')
        self.spool_root, self.timeout_s = spool_root, timeout_s
        self.reservation = self.budget = None
        self.deadline = None
        self._timer = self._work_task = self._spawn = self.process = None
        self._lifetime_read = self._lifetime_write = None
        self._cleanup = self._monitor = None
        self._sampled_rss = sys.platform == 'darwin'
        self._resource_stop = False
        self._stop_reason = None
        self.session = None
        self.stopped = self.finished = self.closed = False
        self._cancel_sent = False

    @property
    def directory(self):
        return self.reservation.directory

    async def __aenter__(self):
        owner = current_owner()
        if owner is None or owner.lane not in ('foreground', 'background', 'mail'):
            raise CapacityContractError('preparation requires foreground, background or mail ownership')
        self.deadline = time.monotonic() + self.timeout_s
        self._timer = asyncio.get_running_loop().call_later(self.timeout_s, self.stop, 'deadline')
        reserve = asyncio.create_task(owned_thread(SpoolReservation.acquire, self.spool_root, self.deadline))
        try:
            self.reservation = await asyncio.shield(reserve)
        except asyncio.CancelledError:
            try:
                self.reservation, _ = await _settle(reserve)
            except PreparationCleanupUnconfirmed as exc:
                self.reservation = exc.reservation
                self._timer.cancel()
                log.error('preparation reservation cleanup unconfirmed')
                await _retain()
            finally:
                await self.close()
            raise
        except PreparationCleanupUnconfirmed as exc:
            self.reservation = exc.reservation
            self._timer.cancel()
            log.error('preparation reservation cleanup unconfirmed')
            await _retain()
        except BaseException:
            self._timer.cancel()
            raise
        self.budget = ResourceBudget(self.directory, self.reservation.directory_fd)
        try:
            self.check()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if isinstance(exc, (OperationalError, InterfaceError)) and exc.sqlstate is None:
            log.error('preparation backend cleanup unconfirmed (%s)', type(exc).__name__)
            await _retain()
        await self.close()

    def check(self):
        if not self.finished and time.monotonic() >= self.deadline:
            self.stop('deadline')
        if self.stopped:
            if self._stop_reason == 'cancel':
                raise asyncio.CancelledError
            raise PreparationResourceError('preparation resource stop')

    def remaining_ms(self):
        self.check()
        return max(1, math.floor((self.deadline - time.monotonic()) * 1000))

    def stop(self, reason='cancel'):
        if self._stop_reason is None:
            self._stop_reason = reason
        self.stopped = True
        if self._work_task is not None and not self._work_task.done() and not self._cancel_sent:
            self._cancel_sent = True
            self._work_task.cancel()

    async def backend_failure(self, exc):
        """Call inside the lifecycle lease before an uncertain client error unwinds."""
        if isinstance(exc, (OperationalError, InterfaceError)) and exc.sqlstate is None:
            log.error('preparation backend cleanup unconfirmed (%s)', type(exc).__name__)
            await _retain()

    async def perform(self, function, *args, **kwargs):
        self.check()
        if self._work_task is not None:
            raise CapacityContractError('preparation already owns active work')
        async def run():
            try:
                return await function(*args, **kwargs)
            except OSError as exc:
                if exc.errno in (28, 122):
                    raise PreparationResourceError('preparation spool is unavailable') from None
                raise
            except (OperationalError, InterfaceError) as exc:
                if exc.sqlstate is not None:
                    raise
                log.error('preparation backend cleanup unconfirmed (%s)', type(exc).__name__)
                await _retain()
        task = self._work_task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            expired = self._stop_reason in ('deadline', 'memory')
            self.stop()
            try:
                await _settle(task)
            except BaseException:
                pass
            if expired:
                raise PreparationResourceError('preparation deadline expired') from None
            raise
        finally:
            self._work_task = None

    async def start_helper(self, mode='report', metadata=None):
        self.check()
        if self._spawn is not None:
            raise CapacityContractError('preparation helper already started')
        if mode not in ('report', 'notification', 'security', 'ntfy', 'export'):
            raise ValueError('unknown preparation mode')
        self._lifetime_read, self._lifetime_write = os.pipe()
        self.reservation.validate()
        directory_fd = os.dup(self.reservation.directory_fd)
        try:
            self._spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, '-I', str(_HELPER), str(self.deadline), str(os.getpid()),
                str(self._lifetime_read), str(directory_fd), str(self.reservation.guard),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={}, close_fds=True,
                pass_fds=(self._lifetime_read, directory_fd, self.reservation.guard),
                limit=FRAME_BYTES + _HEADER.size))
            self.process = await asyncio.shield(self._spawn)
        finally:
            # A cancelled spawn may still need its directory descriptor.
            if self._spawn is not None and not self._spawn.done():
                _, _ = await _settle(self._spawn)
            os.close(directory_fd)
        os.close(self._lifetime_read)
        self._lifetime_read = None
        if self._sampled_rss:
            self._monitor = asyncio.create_task(self._watch_memory())
        try:
            ready = await self.process.stdout.readexactly(len(READY))
        except asyncio.IncompleteReadError:
            code = await self.process.wait()
            if code in (70, 73):
                raise PreparationResourceError('preparation helper resource stop') from None
            raise PreparationError('preparation helper readiness failed') from None
        if ready != READY:
            raise PreparationError('preparation helper readiness failed')
        self.session = PreparationSession(self, self.process)
        await self.session.send_command({'mode': mode})
        if metadata is not None:
            await self.session.send_command(metadata)
        return self.session

    async def _watch_memory(self):
        # This runs in the parent: child C code cannot hold this interpreter's GIL.
        # Sampling stops attempts with possible overshoot; it is not an RSS quota.
        while self.process.returncode is None:
            try:
                resident = _resident_bytes(self.process.pid)
            except Exception:
                resident = None
            if resident is None:
                try:
                    await asyncio.wait_for(asyncio.shield(self.process.wait()), .02)
                    return
                except TimeoutError:
                    pass
            if resident is None or resident > MEMORY_BYTES:
                if self.process.returncode is not None:
                    return
                self._resource_stop = True
                self.stop('memory')
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                return
            await asyncio.sleep(.01)

    async def finish_preparation(self):
        if self.session is not None:
            await self.session.finish_input()
        self.check()
        self.finished = True
        self._timer.cancel()

    def prepared(self, name, *, media_type, filename=None):
        if not self.finished:
            raise CapacityContractError('prepared response requires closed snapshot and reaped helper')
        path, size = self.budget.verify(name)
        return PreparedFileResponse(self, path, size, media_type=media_type, filename=filename)

    async def close(self):
        if self.closed:
            return
        if self._timer is not None:
            self._timer.cancel()
        if self._cleanup is None:
            async def cleanup():
                if self._lifetime_write is not None:
                    os.close(self._lifetime_write)
                    self._lifetime_write = None
                if self._spawn is not None:
                    await _reap(self._spawn, self.process)
                if self._monitor is not None:
                    await self._monitor
                if self._lifetime_read is not None:
                    os.close(self._lifetime_read)
                    self._lifetime_read = None
                if self.reservation is not None:
                    try:
                        await owned_thread(self.reservation.release)
                        if self.budget is not None:
                            self.budget.close()
                    except Exception as exc:
                        log.error('preparation file cleanup unconfirmed (%s)', type(exc).__name__)
                        await _retain()
                self.closed = True
            self._cleanup = asyncio.create_task(cleanup())
        _, cancelled = await _settle(self._cleanup)
        if cancelled:
            raise asyncio.CancelledError


class PreparedFileResponse(Response):
    def __init__(self, operation, path, size, *, media_type, filename=None):
        headers = {'content-length': str(size)}
        if filename is not None:
            encoded = quote(filename)
            headers['content-disposition'] = (f"attachment; filename*=utf-8''{encoded}" if encoded != filename
                else f'attachment; filename="{filename}"')
        super().__init__(b'', media_type=media_type, headers=headers)
        self.operation, self.path = operation, path

    async def __call__(self, scope, receive, send):
        state = scope.setdefault('state', {})
        owner = current_owner()
        config = owner.manager.config if owner is not None else None
        timeout = getattr(config, 'capacity_response_timeout_s', 60.0)
        deadline = state.setdefault('_capacity_response_deadline', time.monotonic() + timeout)
        stream = None
        opening = None
        cancelled = False

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('response deadline expired')
            return value

        try:
            opening = asyncio.create_task(owned_thread(self.operation.budget.open, self.path.name, 'rb'))
            async with asyncio.timeout(remaining()):
                stream = await asyncio.shield(opening)
                await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})
            while True:
                async with asyncio.timeout(remaining()):
                    chunk = await owned_thread(stream.read, 65536)
                    if not chunk:
                        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                        break
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        finally:
            if stream is None and opening is not None:
                try:
                    stream, was_cancelled = await _settle(opening)
                    cancelled |= was_cancelled
                except Exception:
                    pass
            if stream is not None:
                closing = asyncio.create_task(owned_thread(stream.close))
                try:
                    _, was_cancelled = await _settle(closing)
                    cancelled |= was_cancelled
                except Exception as exc:
                    log.error('prepared file close unconfirmed (%s)', type(exc).__name__)
                    await _retain()
            await self.operation.close()
            if cancelled:
                raise asyncio.CancelledError
