"""Isolated preparation entrypoint; memory authority precedes application imports."""
from __future__ import annotations

import os
import sys
import resource

MEMORY_BYTES = 256 * 1024 * 1024
if __name__ == '__main__' and sys.platform == 'linux':
    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    _ceiling = MEMORY_BYTES if _hard == resource.RLIM_INFINITY else min(MEMORY_BYTES, _hard)
    resource.setrlimit(resource.RLIMIT_AS, (_ceiling, _ceiling))

try:
    import ctypes
    import json
    from pathlib import Path
    import select
    import signal
    import struct
    import threading
    import time
except MemoryError:
    os._exit(73)

FRAME_BYTES = 65520
READY = b'PREP1 READY\n'
_HEADER = struct.Struct('!cI')


def _guard_parent(parent):
    if os.getppid() != parent:
        os._exit(71)
    if sys.platform == 'linux':
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(72)
    if os.getppid() != parent:
        os._exit(71)


def _watch(lifetime, deadline):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os._exit(70)
        # Native macOS is a sampled stop with overshoot, not a hard RSS ceiling.
        if sys.platform == 'darwin' and resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > MEMORY_BYTES:
            os._exit(73)
        ready, _, _ = select.select([lifetime], [], [], min(remaining, .01))
        if ready and not os.read(lifetime, 1):
            os._exit(71)


def _exact(fd, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = os.read(fd, size - len(chunks))
        if not data:
            raise EOFError('incomplete preparation frame')
        chunks.extend(data)
    return bytes(chunks)


class Channel:
    def _recv(self, expected):
        header = _HEADER.unpack(_exact(0, _HEADER.size))
        kind, length = header
        if length > FRAME_BYTES:
            raise ValueError('preparation frame exceeds budget')
        if kind == b'D' and length == 0:
            return None
        if kind != expected:
            raise ValueError('unexpected preparation frame')
        return _exact(0, length)

    def recv_command(self):
        raw = self._recv(b'C')
        if raw is None:
            return None
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('invalid preparation command')
        return result

    def recv_text(self):
        result = self._recv(b'T')
        if result is None:
            raise EOFError('incomplete preparation text')
        return result

    def send_response(self, result):
        raw = json.dumps(result, separators=(',', ':'), allow_nan=False).encode('utf8')
        if len(raw) > FRAME_BYTES:
            raise ValueError('preparation response exceeds budget')
        packet = _HEADER.pack(b'R', len(raw)) + raw
        while packet:
            packet = packet[os.write(1, packet):]

    def result(self, result):
        self.send_response(result)


def main():
    deadline, parent, lifetime, directory_fd, guard = (
        float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
    _guard_parent(parent)
    threading.Thread(target=_watch, args=(lifetime, deadline), daemon=True).start()
    os.fchdir(directory_fd)
    os.write(1, READY)
    channel = Channel()
    try:
        dispatch = channel.recv_command()
        if dispatch is None or dispatch.get('mode') not in ('report', 'notification', 'security', 'ntfy', 'export'):
            raise ValueError('unknown preparation mode')
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.preparation_resources import ResourceBudget, PreparationResourceError
        if dispatch['mode'] == 'notification':
            from app.notification_renderer import render
        elif dispatch['mode'] == 'security':
            from app.security_mail_renderer import render
        elif dispatch['mode'] == 'ntfy':
            from app.ntfy_renderer import render
        elif dispatch['mode'] == 'export':
            from app.export_renderer import render
        else:
            from app.report_renderer import render
        render(channel, ResourceBudget('.', directory_fd, relative_paths=True))
    except MemoryError:
        # Use a preallocated constant frame when the address-space limit fires.
        os.write(1, b'R\x00\x00\x00\x14{"error":"resource"}')
        return 73
    except Exception as exc:
        resource_failure = type(exc).__name__ in ('PreparationResourceError', 'PreparationBusy') or (
            isinstance(exc, OSError) and exc.errno in (28, 122))
        category = type(exc).__name__
        if category not in ('ValueError', 'TypeError', 'DataError', 'OverflowError', 'UnicodeEncodeError',
                            'UnicodeDecodeError', 'ZoneInfoNotFoundError', 'HeaderParseError',
                            'IllegalCharacterError'):
            category = None
        channel.send_response({'error': 'resource' if resource_failure else 'preparation',
                               'category': category})
        return 73 if resource_failure else 74
    # The inherited guard shares the parent's open file description until exit.
    os.close(guard)
    return 0


if __name__ == '__main__':
    sys.exit(main())
