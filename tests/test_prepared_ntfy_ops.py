"""Actual isolated NTFY wire and incremental cookie-state oracles."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import time

import httpx
import pytest

from app import ntfy_supervisor as supervisor
from app.notifications import publish_ntfy
from app.ntfy_cookies import cookie_chunks, cookies_in_place
from app.ntfy_helper import ENVIRONMENT_KEYS
from app.prepared_ntfy import PreparedNtfy
from app.preparation_resources import ResourceBudget, SpoolReservation
from app.provider_http import ProviderResponseTooLarge
from test_smtp_supervisor_ops import observe_spawn, assert_reaped, tls_context

pytestmark = pytest.mark.ops


class Receiver:
    def __init__(self, response=None, *, stall=False):
        self.response = response or b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
        self.stall = stall
        self.requests = []
        self.entered, self.eof = asyncio.Event(), asyncio.Event()
        self.handlers = []

    async def start(self, tls=None):
        async def handle(reader, writer):
            try:
                raw = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b': ', 1) for line in raw.split(b'\r\n')[1:-2])
                body = await reader.readexactly(int(headers[b'Content-Length']))
                self.requests.append((raw, body))
                writer.write(self.response)
                await writer.drain()
                self.entered.set()
                if self.stall:
                    await reader.read()
                    self.eof.set()
            except (asyncio.IncompleteReadError, ConnectionError):
                self.eof.set()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
        def accept(reader, writer):
            task = asyncio.create_task(handle(reader, writer))
            self.handlers.append(task)
        self.server = await asyncio.start_server(accept, '127.0.0.1', 0, ssl=tls)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in self.handlers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)


def prepare(root, url, client, *, body='complete é界\n' * 40000, **options):
    reservation = SpoolReservation.acquire(root, time.monotonic() + 5)
    budget = ResourceBudget(reservation.directory, reservation.directory_fd)
    raw = body.encode('utf8')
    config = dict(url=url, topic='topic', token='', username='', password='', body_bytes=len(raw),
        environment={key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ})
    config.update(options)
    for name, value in (('ntfy-body', raw), ('ntfy-config', json.dumps(config, ensure_ascii=False).encode('utf8', 'surrogatepass'))):
        with budget.open(name, 'wb') as sink:
            sink.write(value)
    with budget.open('ntfy-cookies', 'wb') as sink:
        for cookie in cookies_in_place(client.cookies.jar):
            for chunk in cookie_chunks(cookie):
                sink.write(chunk)
            sink.write(b'\n')
    budget.close()
    return PreparedNtfy(reservation, dict(config='ntfy-config', body='ntfy-body', cookies='ntfy-cookies'))


def state(client):
    return [b''.join(cookie_chunks(cookie)) for cookie in client.cookies.jar]


@pytest.mark.parametrize('auth', ['none', 'bearer', 'basic', 'url'])
def test_complete_wire_default_headers_auth_and_initial_cookies_match_legacy(monkeypatch, tmp_path, auth):
    processes, calls = observe_spawn(monkeypatch)
    async def scenario():
        receiver = Receiver()
        port = await receiver.start()
        url = f'http://127.0.0.1:{port}'
        if auth == 'url':
            url = f'http://url-user:url-password@127.0.0.1:{port}'
        values = dict(token='ignored-bearer' if auth == 'basic' else 'fixture-token' if auth == 'bearer' else '',
                      username='user' if auth == 'basic' else '', password='password' if auth == 'basic' else '')
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as original, httpx.AsyncClient() as held:
            for client in (original, held):
                client.cookies.set('initial', 'value', domain='127.0.0.1', path='/')
            body = 'é界 full message\n' * 40000
            prepared = prepare(tmp_path / 'spool', url, held, body=body, **values)
            try:
                await publish_ntfy(original, url, 'topic', values['token'], values['username'], values['password'], body)
                await supervisor.send_prepared(prepared, http_client=held)
                assert receiver.requests[0] == receiver.requests[1]
                assert state(held) == state(original)
                assert_reaped(processes)
                _, kwargs = calls[0]
                assert kwargs['env'] == {} and kwargs['close_fds']
                assert len(kwargs['pass_fds']) == 5
                assert prepared.reservation.guard in kwargs['pass_fds']
                assert prepared.reservation.directory_fd not in kwargs['pass_fds']
            finally:
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['success', 'status', 'body', 'read', 'deadline', 'cancel'])
def test_cookie_additions_deletions_and_expiry_match_legacy_on_failures(monkeypatch, tmp_path, outcome):
    processes, _ = observe_spawn(monkeypatch)
    if outcome in ('deadline', 'cancel'):
        monkeypatch.setattr(supervisor, 'TRANSPORT_SECONDS', .5 if outcome == 'deadline' else 15)
    async def scenario():
        cookie_headers = (b'Set-Cookie: old=gone; Max-Age=0; Path=/\r\n'
                          b'Set-Cookie: new=accepted; HttpOnly; SameSite=Lax; Path=/\r\n'
                          b'Set-Cookie: other=next; Path=/\r\n')
        status = b'500 Failed' if outcome == 'status' else b'200 OK'
        body = gzip.compress(b'x' * 65537) if outcome == 'body' else b''
        if outcome in ('read', 'deadline', 'cancel'):
            content = b'Content-Length: 10\r\n'
        else:
            content = f'Content-Length: {len(body)}\r\n'.encode()
        if outcome == 'body':
            content += b'Content-Encoding: gzip\r\n'
        response = b'HTTP/1.1 ' + status + b'\r\n' + cookie_headers + content + b'Connection: close\r\n\r\n' + body
        receiver = Receiver(response, stall=outcome in ('deadline', 'cancel'))
        port = await receiver.start()
        url = f'http://127.0.0.1:{port}'
        async with httpx.AsyncClient() as oracle, httpx.AsyncClient() as held:
            for client in (oracle, held):
                client.cookies.set('old', 'existing', domain='127.0.0.1', path='/')
                client.cookies.set('expired', 'still-held', domain='127.0.0.1', path='/')
                next(cookie for cookie in client.cookies.jar if cookie.name == 'expired').expires = 1
            # Feed the exact network response into the old synchronous cookie
            # extraction hook. The remaining body/status failure cannot undo it.
            request = oracle.build_request('POST', url + '/topic', content='body')
            header_pairs = [tuple(line.split(b': ', 1)) for line in cookie_headers.split(b'\r\n') if line]
            oracle.cookies.extract_cookies(httpx.Response(200, headers=header_pairs, request=request))
            prepared = prepare(tmp_path / 'spool', url, held, body='body')
            try:
                task = asyncio.create_task(supervisor.send_prepared(prepared, http_client=held))
                if outcome == 'cancel':
                    await asyncio.wait_for(receiver.entered.wait(), 2)
                    await asyncio.sleep(.05)
                    task.cancel()
                    for _ in range(5):
                        await asyncio.sleep(0)
                        task.cancel()
                expected = {'status': httpx.HTTPStatusError, 'body': ProviderResponseTooLarge,
                    'read': httpx.RemoteProtocolError, 'deadline': httpx.TimeoutException,
                    'cancel': asyncio.CancelledError}.get(outcome)
                if expected:
                    with pytest.raises(expected):
                        await task
                else:
                    await task
                assert state(held) == state(oracle)
                assert any(cookie.name == 'expired' for cookie in held.cookies.jar)
                assert_reaped(processes)
                if outcome in ('cancel', 'deadline'):
                    await asyncio.wait_for(receiver.eof.wait(), 1)
            finally:
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


def test_tls_uses_operator_ca_environment_without_database_environment(monkeypatch, tmp_path, tls_context):
    processes, calls = observe_spawn(monkeypatch)
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'cert.pem'))
    monkeypatch.setenv('DATABASE_URL', 'postgresql://excluded.invalid/private')
    async def scenario():
        receiver = Receiver()
        port = await receiver.start(tls=tls_context)
        async with httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', f'https://localhost:{port}', held, body='tls')
            try:
                await supervisor.send_prepared(prepared, http_client=held)
                assert receiver.requests[0][1] == b'tls'
                assert_reaped(processes)
                assert calls[0][1]['env'] == {}
            finally:
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


def test_full_valid_configuration_has_no_aggregate_text_cap(tmp_path):
    async def scenario():
        receiver = Receiver()
        port = await receiver.start()
        async with httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', f'http://127.0.0.1:{port}', held,
                               body='complete', password='界' * 100000)
            try:
                await supervisor.send_prepared(prepared, http_client=held)
                assert receiver.requests[0][1] == b'complete'
            finally:
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


def test_operator_proxy_environment_preserves_absolute_request_wire(monkeypatch, tmp_path):
    async def scenario():
        proxy = Receiver()
        port = await proxy.start()
        for key in ENVIRONMENT_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv('HTTP_PROXY', f'http://proxy-user:proxy-password@127.0.0.1:{port}')
        monkeypatch.setenv('NO_PROXY', '')
        url = 'http://external.example.test'
        async with httpx.AsyncClient() as original, httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', url, held, body='through proxy')
            try:
                await publish_ntfy(original, url, 'topic', '', '', '', 'through proxy')
                await supervisor.send_prepared(prepared, http_client=held)
                assert proxy.requests[0] == proxy.requests[1]
                assert b'POST http://external.example.test/topic HTTP/1.1' in proxy.requests[1][0]
                assert b'Proxy-Authorization: Basic ' in proxy.requests[1][0]
            finally:
                prepared.reservation.release()
                await proxy.close()
    asyncio.run(scenario())


def test_preparation_callback_runs_after_verified_fds_before_transport_clock(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, 'TRANSPORT_SECONDS', .5)
    async def scenario():
        receiver = Receiver()
        port = await receiver.start()
        async with httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', f'http://127.0.0.1:{port}', held, body='callback')
            opened = []
            original = PreparedNtfy.open_descriptors
            def capture(self):
                result = original(self)
                opened.extend(result[1])
                return result
            monkeypatch.setattr(PreparedNtfy, 'open_descriptors', capture)
            async def callback():
                assert len(opened) == 3 and not processes
                for descriptor in opened:
                    assert os.fstat(descriptor).st_size >= 0
                await asyncio.sleep(.6)
            try:
                await supervisor.send_prepared(prepared, http_client=held, before_transport=callback)
                assert_reaped(processes)
                for descriptor in opened:
                    with pytest.raises(OSError):
                        os.fstat(descriptor)
            finally:
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


def test_hard_stop_drains_completed_cookie_delta_and_never_applies_partial_record(monkeypatch, tmp_path):
    from app.ntfy_cookies import HEADER, cookie_chunks
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, 'TRANSPORT_SECONDS', .4)
    source = httpx.Cookies()
    source.set('complete', 'applied', domain='example.test')
    cookie = next(iter(source.jar))
    record = b''.join(cookie_chunks(cookie))
    packet = HEADER.pack(b'S', len(record)) + HEADER.pack(b'T', len(record)) + record
    partial = HEADER.pack(b'S', len(record)) + HEADER.pack(b'T', len(record)) + record[:3]
    helper = tmp_path / 'hard-cookie-stop.py'
    helper.write_text('import os, sys, time\nos.write(1, ' + repr(supervisor.READY) + ')\n'
        'sys.stdin.buffer.read()\nos.write(1, ' + repr(packet + partial) + ')\ntime.sleep(60)\n')
    monkeypatch.setattr(supervisor, 'HELPER', helper)
    async def scenario():
        async with httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', 'http://example.test', held, body='never success')
            try:
                with pytest.raises(httpx.TimeoutException):
                    await supervisor.send_prepared(prepared, http_client=held)
                assert held.cookies.get('complete') == 'applied'
                assert len(held.cookies) == 1
                assert_reaped(processes)
                prepared.reservation.validate()
            finally:
                prepared.reservation.release()
    asyncio.run(scenario())


def test_repeated_cancel_retains_descriptors_and_owner_through_cookie_apply_thread(monkeypatch, tmp_path):
    import threading
    from app.account_context import AccountPrincipal
    from app.capacity import AdmissionManager
    processes, _ = observe_spawn(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = supervisor.apply_cookie
    def blocked(jar, kind, record):
        entered.set()
        assert release.wait(5)
        original(jar, kind, record)
    monkeypatch.setattr(supervisor, 'apply_cookie', blocked)
    opened = []
    open_descriptors = PreparedNtfy.open_descriptors
    def capture(self):
        value = open_descriptors(self)
        opened.extend(value[1])
        return value
    monkeypatch.setattr(PreparedNtfy, 'open_descriptors', capture)
    async def scenario():
        receiver = Receiver(b'HTTP/1.1 200 OK\r\nSet-Cookie: applied=after-thread; Path=/\r\n'
                            b'Content-Length: 0\r\nConnection: close\r\n\r\n')
        port = await receiver.start()
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        async with httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', f'http://127.0.0.1:{port}', held, body='thread')
            async def send():
                async with manager.operation('background', principal):
                    await supervisor.send_prepared(prepared, http_client=held)
            task = asyncio.create_task(send())
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(.005)
                    task.cancel()
                assert not task.done()
                assert manager.snapshot()['background']['active'] == 1
                assert held.cookies.get('applied') is None
                for descriptor in opened:
                    assert os.fstat(descriptor).st_size >= 0
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert held.cookies.get('applied') == 'after-thread'
                assert manager.snapshot()['background']['active'] == 0
                assert_reaped(processes)
                for descriptor in opened:
                    with pytest.raises(OSError):
                        os.fstat(descriptor)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


def test_actual_prepared_parent_death_stops_http_and_reclaims_helper_only_orphan(tmp_path):
    import ctypes
    from pathlib import Path
    import signal
    import subprocess
    import sys
    from app.preparation_resources import PreparationBusy
    libc = None
    previous = ctypes.c_int()
    if sys.platform == 'linux':
        libc = ctypes.CDLL(None)
        assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
        assert libc.prctl(36, 1, 0, 0, 0) == 0
    launcher = tmp_path / 'ntfy-parent.py'
    launcher.write_text('''import json, os, struct, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from app.ntfy_supervisor import HELPER
from app.ntfy_helper import READY
from app.prepared_ntfy import PreparedNtfy
from app.preparation_resources import ResourceBudget, SpoolReservation
reservation = SpoolReservation.acquire(sys.argv[2], time.monotonic() + 5)
budget = ResourceBudget(reservation.directory, reservation.directory_fd)
config = dict(url='http://127.0.0.1:' + sys.argv[3], topic='topic', token='', username='', password='', environment={}, body_bytes=4)
for name, value in [('body', b'body'), ('config', json.dumps(config).encode()), ('cookies', b'')]:
    with budget.open(name, 'wb') as sink:
        sink.write(value)
budget.close()
prepared = PreparedNtfy(reservation, dict(body='body', config='config', cookies='cookies'))
stack, descriptors, guard = prepared.open_descriptors()
read, write = os.pipe()
child = subprocess.Popen([sys.executable, '-I', str(HELPER), str(time.monotonic()+15), str(os.getpid()), str(read)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True, pass_fds=(read, *descriptors, guard))
os.close(read)
assert child.stdout.readline() == READY
child.stdin.write(struct.pack('!3i', *descriptors))
child.stdin.flush()
child.stdin.close()
stack.close()
os.close(reservation.guard)
os.close(reservation.directory_fd)
os.close(reservation.root_fd)
print(json.dumps(dict(pid=child.pid, name=reservation.name)), flush=True)
time.sleep(60)
''')
    async def scenario():
        root = tmp_path / 'spool'
        observers = [SpoolReservation.acquire(root, time.monotonic() + 5) for _ in range(3)]
        receiver = Receiver(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n', stall=True)
        port = await receiver.start()
        source = Path(__file__).resolve().parents[1]
        parent = subprocess.Popen([sys.executable, '-I', str(launcher), str(source), str(root), str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            identity = json.loads(await asyncio.to_thread(parent.stdout.readline))
            child_pid = identity['pid']
            await asyncio.wait_for(receiver.entered.wait(), 3)
            with pytest.raises(PreparationBusy, match='reservation exhausted'):
                SpoolReservation.acquire(root, time.monotonic() + 5)
            began = time.monotonic()
            parent.kill()
            await asyncio.to_thread(parent.wait)
            while True:
                if libc is not None:
                    pid, _ = os.waitpid(child_pid, os.WNOHANG)
                    if pid == child_pid:
                        break
                else:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                assert time.monotonic() - began < 2
                await asyncio.sleep(.01)
            await asyncio.wait_for(receiver.eof.wait(), 1)
            recovered = SpoolReservation.acquire(root, time.monotonic() + 5)
            assert not (root / identity['name']).exists()
            assert len(list(root.glob('op-*'))) == 4
            recovered.release()
        finally:
            if parent.poll() is None:
                parent.kill()
                await asyncio.to_thread(parent.wait)
            parent.stdout.close()
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if libc is not None:
                    try:
                        os.waitpid(child_pid, 0)
                    except ChildProcessError:
                        pass
            await receiver.close()
            for reservation in observers:
                reservation.release()
    try:
        asyncio.run(scenario())
    finally:
        if libc is not None:
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def test_cookie_parser_memory_error_is_resource_failure_not_partial_success(monkeypatch, tmp_path):
    from pathlib import Path
    target = Path(supervisor.HELPER)
    helper = tmp_path / 'cookie-memory-stage.py'
    helper.write_text('import http.cookiejar, runpy\n'
        'def memory_stop(*args, **kwargs):\n    raise MemoryError\n'
        'http.cookiejar.parse_ns_headers = memory_stop\n'
        'namespace = runpy.run_path(' + repr(str(target)) + ')\n'
        "raise SystemExit(namespace['main']())\n")
    # This is a construction-stage injection, not a native AS-limit proof.
    monkeypatch.setattr(supervisor, 'HELPER', helper)
    processes, _ = observe_spawn(monkeypatch)
    async def scenario():
        receiver = Receiver(b'HTTP/1.1 200 OK\r\nSet-Cookie: attempted=no-success; Path=/\r\n'
                            b'Content-Length: 0\r\nConnection: close\r\n\r\n')
        port = await receiver.start()
        async with httpx.AsyncClient() as held:
            prepared = prepare(tmp_path / 'spool', f'http://127.0.0.1:{port}', held, body='resource')
            try:
                with pytest.raises(supervisor.NtfyTransportError) as failure:
                    await supervisor.send_prepared(prepared, http_client=held)
                assert failure.value.failure == 'resource'
                assert failure.value.phase == 'request'
                assert failure.value.cleanup_confirmed
                assert held.cookies.get('attempted') is None
                assert_reaped(processes)
            finally:
                prepared.reservation.release()
                await receiver.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [('username', '\ud800\udc80'), ('password', '\ud800\udc80'),
                                         ('username', '\U00010080'), ('password', '\U00010080')])
def test_real_renderer_and_actual_auth_preserve_adjacent_surrogates(monkeypatch, tmp_path, field, value):
    from app.account_context import AccountPrincipal
    from app.capacity import AdmissionManager
    from app.notification_preparation import Projection
    from app.preparation import PreparationOperation
    processes, _ = observe_spawn(monkeypatch)
    async def scenario():
        receiver = Receiver()
        port = await receiver.start()
        url = f'http://127.0.0.1:{port}'
        credentials = {'username': 'fixture-user', 'password': 'fixture-password'}
        credentials[field] = value
        try:
            expected_auth = httpx.BasicAuth(credentials['username'], credentials['password'])
        except UnicodeEncodeError:
            expected_failure = UnicodeEncodeError
        else:
            expected_failure = None
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        async with httpx.AsyncClient() as oracle, httpx.AsyncClient() as held:
            try:
                async with manager.operation('background', principal):
                    async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                        session = await operation.start_helper('ntfy')
                        projection = Projection(operation, session)
                        for key, text in (('display_tz', 'UTC'), ('ntfy_topic', 'topic'),
                            ('app_url', ''), ('url', url), ('token', ''),
                            ('username', credentials['username']), ('password', credentials['password'])):
                            await projection.literal(key, text)
                        await projection.timezone_paths()
                        await session.request({'type': 'initialize', 'kind': 'weekly', 'hour': 18,
                                               'now': '2026-11-02T19:00:00+00:00'})
                        await projection.literal('environment', '{}')
                        result = await session.request({'type': 'render', 'count': 1})
                        await session.finish_input()
                        prepared = PreparedNtfy(operation.reservation, result['artifacts'])
                        stack, fds, guard = prepared.open_descriptors()
                        try:
                            with os.fdopen(os.dup(fds[0]), 'rb') as source:
                                config = json.load(source)
                            assert config[field] == value
                        finally:
                            stack.close()
                        if expected_failure:
                            with pytest.raises(expected_failure):
                                await supervisor.send_prepared(prepared, http_client=held,
                                    before_transport=operation.finish_preparation)
                            assert not receiver.requests
                        else:
                            await publish_ntfy(oracle, url, 'topic', '', credentials['username'],
                                               credentials['password'], 'Odograph: 1 unclassified trip in the past week.')
                            await supervisor.send_prepared(prepared, http_client=held,
                                before_transport=operation.finish_preparation)
                            assert receiver.requests[0] == receiver.requests[1]
                        assert_reaped(processes)
                assert not list((tmp_path / 'spool').glob('op-*'))
            finally:
                await receiver.close()
    asyncio.run(scenario())
