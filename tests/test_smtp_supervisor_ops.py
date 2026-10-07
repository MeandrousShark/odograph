"""Real SMTP helper exit/EOF, deadline, parent death and isolation checks."""
from __future__ import annotations

import asyncio
import fcntl
import os
from email import policy
from email.parser import BytesParser
import signal
import ssl
import subprocess
import sys
import time

import pytest

from app.mailer import Mailer
from app import smtp_supervisor as supervisor

pytestmark = pytest.mark.ops


def mailer(port, **overrides):
    values = dict(host="127.0.0.1", port=port, username="", password="",
                  security="none", tls_insecure=False,
                  from_addr="sender@example.test", to_addr="recipient@example.test")
    values.update(overrides)
    return Mailer(**values)


class Relay:
    def __init__(self, stall=None, tls=None):
        self.stall = stall
        self.tls = tls
        self.accepted = asyncio.Event()
        self.data_accepted = asyncio.Event()
        self.stalled = asyncio.Event()
        self.eof = asyncio.Event()
        self.messages = []
        self.recipients = []
        self.tasks = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.accepted.set()
        try:
            if self.stall == "greeting":
                self.stalled.set()
                await reader.read()
                return
            if self.stall == "trickle":
                self.stalled.set()
                while not reader.at_eof():
                    writer.write(b"2")
                    await writer.drain()
                    await asyncio.sleep(.05)
                return
            writer.write(b"220 fixture\r\n")
            await writer.drain()
            while line := await reader.readline():
                command = line.split()[0].upper()
                if command.decode().lower() == self.stall:
                    self.stalled.set()
                    await reader.read()
                    return
                if command in (b"EHLO", b"HELO"):
                    writer.write(b"250-fixture\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n")
                elif command == b"STARTTLS":
                    writer.write(b"220 TLS\r\n")
                    await writer.drain()
                    if self.stall == "tls":
                        self.stalled.set()
                        await reader.read()
                        return
                    await writer.start_tls(self.tls)
                    continue
                elif command == b"AUTH":
                    writer.write(b"235 authenticated\r\n")
                elif command in (b"MAIL", b"RCPT"):
                    if command == b"RCPT":
                        self.recipients.append(line.strip())
                    writer.write(b"250 accepted\r\n")
                elif command == b"DATA":
                    writer.write(b"354 data\r\n")
                    await writer.drain()
                    content = bytearray()
                    while chunk := await reader.readline():
                        if chunk == b".\r\n":
                            break
                        content.extend(chunk)
                    self.messages.append(bytes(content))
                    if self.stall == "data_response":
                        self.stalled.set()
                        await reader.read()
                        return
                    self.data_accepted.set()
                    writer.write(b"250 delivered\r\n")
                elif command == b"QUIT":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    return
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
            self.eof.set()
            self.tasks.discard(task)

    async def start(self, implicit=False):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0,
                                                ssl=self.tls if implicit else None)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def observe_spawn(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes, calls


def assert_reaped(processes):
    assert processes
    for process in processes:
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)


@pytest.mark.parametrize("phase", ["greeting", "ehlo", "auth", "rcpt", "data", "data_response", "quit", "trickle", "starttls", "tls"])
def test_deadline_stops_real_protocol_work_and_reaps_before_return(monkeypatch, phase):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, "TRANSPORT_TIMEOUT_S", .45)

    async def scenario():
        relay = Relay(phase)
        port = await relay.start()
        send = mailer(port, security="starttls" if phase in ("starttls", "tls") else "none",
                      username="user" if phase == "auth" else "")
        began = time.monotonic()
        try:
            with pytest.raises(supervisor.SMTPTransportTimeout):
                await send.send(send.compose("complete subject", "complete body"))
            assert .35 <= time.monotonic() - began < 2.45
            assert_reaped(processes)
            await asyncio.wait_for(relay.eof.wait(), 1)
            if phase == "quit":
                assert relay.data_accepted.is_set()
        finally:
            await relay.close()
    asyncio.run(scenario())


def test_plain_success_preserves_complete_message_and_session_exit(monkeypatch):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        relay = Relay()
        port = await relay.start()
        send = mailer(port, username="user", password="fixture-secret")
        try:
            body = "complete body, caf\u00e9, \u65e5\u672c\u8a9e\n" + "x" * 180000 + "\n"
            message = send.compose("complete subject", body)
            message["Cc"] = "second@example.test"
            message["Bcc"] = "hidden@example.test"
            await send.send(message)
            assert_reaped(processes)
            assert len(relay.messages) == 1
            assert b"complete subject" in relay.messages[0]
            assert b"second@example.test" in relay.messages[0]
            assert b"Bcc:" not in relay.messages[0]
            assert b"complete body" in relay.messages[0]
            received = BytesParser(policy=policy.default).parsebytes(relay.messages[0])
            assert received.get_content().replace("\r\n", "\n") == body
            assert {line.split(b"<", 1)[1].split(b">", 1)[0] for line in relay.recipients} == {
                b"recipient@example.test", b"second@example.test", b"hidden@example.test"}
        finally:
            await relay.close()
    asyncio.run(scenario())


def test_repeated_cancellation_retains_actual_child_until_reap(monkeypatch):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        relay = Relay("quit")
        port = await relay.start()
        send = mailer(port)
        try:
            task = asyncio.create_task(send.send(send.compose("subject", "body")))
            await asyncio.wait_for(relay.data_accepted.wait(), 2)
            task.cancel()
            for _ in range(20):
                await asyncio.sleep(0)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(processes)
            await asyncio.wait_for(relay.eof.wait(), 1)
        finally:
            await relay.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_pending_launch_is_collected_and_reaped(monkeypatch, cancel):
    original = asyncio.create_subprocess_exec
    processes = []
    monkeypatch.setattr(supervisor, "TRANSPORT_TIMEOUT_S", .1)

    async def slow_spawn(*args, **kwargs):
        # A launch that settles after cancellation/deadline still belongs to us.
        await asyncio.sleep(.25)
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)

    async def scenario():
        payload = supervisor.serialize_payload(mailer(1), mailer(1).compose("subject", "body"))
        task = asyncio.create_task(supervisor.send_payload(payload))
        if cancel:
            await asyncio.sleep(.03)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(.01)
                task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else supervisor.SMTPTransportTimeout):
            await task
        assert_reaped(processes)
    asyncio.run(scenario())


def wrapper(tmp_path, content):
    path = tmp_path / "helper.py"
    path.write_text(content)
    return path


@pytest.mark.parametrize("mode", ["no_result", "malformed", "oversize", "crash", "backpressure"])
def test_missing_bad_crashed_or_blocked_helper_cannot_report_success(monkeypatch, tmp_path, mode):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, "TRANSPORT_TIMEOUT_S", .35)
    scripts = {
        "no_result": "sys.stdin.buffer.read(); sys.exit(0)",
        "malformed": "sys.stdin.buffer.read(); os.write(1, b'OK junk\\n')",
        "oversize": "sys.stdin.buffer.read(); os.write(1, b'x' * 2048)",
        "crash": "sys.exit(9)",
        "backpressure": "time.sleep(60)",
    }
    path = wrapper(tmp_path, "import os, sys, time\nos.write(1, " + repr(supervisor.READY) + ")\n" + scripts[mode])
    monkeypatch.setattr(supervisor, "HELPER_PATH", path)

    async def scenario():
        with pytest.raises(supervisor.SMTPTransportError):
            await supervisor.send_payload(b"x" * (2 * 1024 * 1024))
        assert_reaped(processes)
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["dns", "connect"])
def test_helper_watchdog_independently_stops_blocked_work(monkeypatch, tmp_path, blocked):
    # Bypass the parent supervisor's deadline to prove the child's own watchdog.
    target = str(supervisor.HELPER_PATH)
    patch = ("socket.getaddrinfo = lambda *a, **k: time.sleep(60)" if blocked == "dns"
             else "ns['smtplib'].SMTP.connect = lambda *a, **k: time.sleep(60)")
    path = wrapper(tmp_path, f"import runpy, socket, time\nns = runpy.run_path({target!r})\n{patch}\nns['main']()\n")

    async def scenario():
        read, write = os.pipe()
        process = None
        try:
            deadline = time.monotonic() + .4
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(path), str(deadline), str(os.getpid()), str(read),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={}, close_fds=True, pass_fds=(read,))
            assert await process.stdout.readexactly(len(supervisor.READY)) == supervisor.READY
            payload = supervisor.serialize_payload(mailer(1), mailer(1).compose("subject", "body"))
            import struct
            process.stdin.write(struct.pack("!Q", len(payload)) + payload)
            await process.stdin.drain()
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 2.4) == 70
            assert_reaped([process])
        finally:
            os.close(write)
            os.close(read)
            if process and process.returncode is None:
                process.kill()
                await process.wait()
    asyncio.run(scenario())


def test_minimal_environment_and_closed_unrelated_descriptors(monkeypatch, tmp_path):
    processes, calls = observe_spawn(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "must-not-inherit")
    monkeypatch.setenv("SMTP_PASSWORD", "must-not-inherit")
    base = os.open(os.devnull, os.O_RDONLY)
    unrelated = fcntl.fcntl(base, fcntl.F_DUPFD, 100)
    os.set_inheritable(unrelated, True)
    target = str(supervisor.HELPER_PATH)
    path = wrapper(tmp_path, f'''import os, runpy
assert not os.environ.get('DATABASE_URL')
assert not os.environ.get('SMTP_PASSWORD')
try:
    os.fstat({unrelated})
except OSError:
    pass
else:
    raise RuntimeError('unrelated fd inherited')
ns = runpy.run_path({target!r})
raise SystemExit(ns['main']())
''')
    monkeypatch.setattr(supervisor, "HELPER_PATH", path)

    async def scenario():
        relay = Relay()
        port = await relay.start()
        send = mailer(port, password="payload-secret")
        try:
            await send.send(send.compose("private subject", "private body"))
            assert_reaped(processes)
            args, options = calls[0]
            assert options["env"] == {}
            assert options["close_fds"] is True
            assert len(options["pass_fds"]) == 1
            assert "payload-secret" not in repr(args)
            assert "private subject" not in repr(args)
        finally:
            await relay.close()
    try:
        asyncio.run(scenario())
    finally:
        os.close(unrelated)
        os.close(base)


@pytest.fixture
def tls_context(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=localhost"], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


@pytest.mark.parametrize("security", ["ssl", "starttls"])
@pytest.mark.parametrize("insecure", [False, True])
def test_real_tls_verifies_certificates_and_retains_explicit_bridge_exception(monkeypatch, tls_context, security, insecure):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        relay = Relay(tls=tls_context)
        port = await relay.start(implicit=security == "ssl")
        send = mailer(port, security=security, tls_insecure=insecure)
        try:
            if insecure:
                await send.send(send.compose("subject", "body"))
                assert relay.data_accepted.is_set()
            else:
                with pytest.raises(supervisor.SMTPTransportError, match="tls"):
                    await send.send(send.compose("subject", "body"))
                assert not relay.data_accepted.is_set()
            assert_reaped(processes)
        finally:
            await relay.close()
    asyncio.run(scenario())


def test_repeated_cancellation_waits_through_kill_grace_and_unconfirmed_reap(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, "TERMINATE_GRACE_S", .15)
    path = wrapper(tmp_path, "import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nos.write(1, " + repr(supervisor.READY) + ")\ntime.sleep(60)\n")
    monkeypatch.setattr(supervisor, "HELPER_PATH", path)

    async def scenario():
        task = asyncio.create_task(supervisor.send_payload(b"small payload"))
        try:
            while not processes:
                await asyncio.sleep(.005)
            await asyncio.sleep(.08)
            process = processes[0]
            original_wait = process.wait
            permit_reap = asyncio.Event()
            failures = 0

            async def unreliable_wait():
                nonlocal failures
                if not permit_reap.is_set():
                    failures += 1
                    raise OSError("secret error text must not be logged")
                return await original_wait()

            monkeypatch.setattr(process, "wait", unreliable_wait)
            task.cancel()
            for _ in range(30):
                await asyncio.sleep(.01)
                task.cancel()
            assert not task.done()
            assert failures > 0
            permit_reap.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(processes)
            assert process.returncode == -signal.SIGKILL
        finally:
            if not task.done():
                permit_reap.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_no_secret_payload_is_sent_before_readiness(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, "TRANSPORT_TIMEOUT_S", .25)
    marker = tmp_path / "unexpected-payload"
    # This fixture never becomes ready, but monitors the input pipe.
    path = wrapper(tmp_path, f'''import os, select, time
readable, _, _ = select.select([0], [], [], 1)
if readable and os.read(0, 65536):
    open({str(marker)!r}, 'w').write('payload-before-readiness')
time.sleep(60)
''')
    monkeypatch.setattr(supervisor, "HELPER_PATH", path)

    async def scenario():
        with pytest.raises(supervisor.SMTPTransportTimeout):
            await supervisor.send_payload(b"credentials and private message")
        assert_reaped(processes)
        assert not marker.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["dns", "greeting", "quit"])
def test_parent_death_stops_helper_network_work_within_cleanup_target(tmp_path, phase):
    import ctypes
    helper = str(supervisor.HELPER_PATH)
    if phase == "dns":
        helper = str(wrapper(tmp_path, f"import runpy, socket, time\nns = runpy.run_path({helper!r})\nsocket.getaddrinfo = lambda *a, **k: time.sleep(60)\nns['main']()\n"))
    # Linux containers need the test to reap the orphan rather than relying on
    # PID 1. macOS launchd already reaps orphans.
    libc = None
    previous_subreaper = ctypes.c_int()
    if sys.platform == "linux":
        libc = ctypes.CDLL(None)
        assert libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0) == 0
        assert libc.prctl(36, 1, 0, 0, 0) == 0
    parent_code = '''import base64, json, os, struct, subprocess, sys, time
read, write = os.pipe()
child = subprocess.Popen([sys.executable, '-I', sys.argv[1], str(time.monotonic() + 30), str(os.getpid()), str(read)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True, pass_fds=(read,))
os.close(read)
assert child.stdout.readline() == b'SMTP1 READY\\n'
payload = json.dumps(dict(host='127.0.0.1', port=int(sys.argv[2]), username='', password='', security='none', tls_insecure=False, message=base64.b64encode(b'From: a@example.test\\nTo: b@example.test\\n\\nbody\\n').decode())).encode()
child.stdin.write(struct.pack('!Q', len(payload)) + payload)
child.stdin.flush()
child.stdin.close()
print(child.pid, flush=True)
time.sleep(60)
'''
    parent_path = tmp_path / "parent.py"
    parent_path.write_text(parent_code)

    async def scenario():
        relay = Relay(None if phase == "dns" else phase)
        port = await relay.start()
        parent = subprocess.Popen([sys.executable, "-I", str(parent_path), helper, str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        helper_pid = None
        try:
            # Reading this small readiness line in a thread keeps the relay free.
            line = await asyncio.to_thread(parent.stdout.readline)
            helper_pid = int(line)
            if phase == "quit":
                await asyncio.wait_for(relay.data_accepted.wait(), 3)
            elif phase == "greeting":
                await asyncio.wait_for(relay.accepted.wait(), 3)
            else:
                await asyncio.sleep(.05)
            began = time.monotonic()
            parent.kill()
            await asyncio.to_thread(parent.wait)
            while True:
                if libc is not None:
                    pid, _ = os.waitpid(helper_pid, os.WNOHANG)
                    if pid == helper_pid:
                        break
                else:
                    try:
                        os.kill(helper_pid, 0)
                    except ProcessLookupError:
                        break
                assert time.monotonic() - began < 2
                await asyncio.sleep(.01)
            if phase != "dns":
                await asyncio.wait_for(relay.eof.wait(), 1)
                assert time.monotonic() - began < 2
            else:
                assert not relay.accepted.is_set()
        finally:
            if parent.poll() is None:
                parent.kill()
                await asyncio.to_thread(parent.wait)
            parent.stdout.close()
            if helper_pid is not None:
                try:
                    os.kill(helper_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if libc is not None:
                    try:
                        os.waitpid(helper_pid, 0)
                    except ChildProcessError:
                        pass
            await relay.close()
    try:
        asyncio.run(scenario())
    finally:
        if libc is not None:
            assert libc.prctl(36, previous_subreaper.value, 0, 0, 0) == 0


@pytest.mark.parametrize("phase", ["greeting", "ehlo", "auth", "rcpt", "data", "data_response", "quit", "starttls", "tls", "trickle"])
def test_actual_send_cancellation_in_each_protocol_phase_closes_socket_and_reaps(monkeypatch, phase):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        relay = Relay(phase)
        port = await relay.start()
        send = mailer(port, security="starttls" if phase in ("starttls", "tls") else "none",
                      username="user" if phase == "auth" else "")
        task = asyncio.create_task(send.send(send.compose("subject", "body")))
        try:
            await asyncio.wait_for(relay.stalled.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(processes)
            await asyncio.wait_for(relay.eof.wait(), 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await relay.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("security", ["ssl", "starttls"])
def test_verified_tls_success_with_fixture_trust_root(monkeypatch, tmp_path, tls_context, security):
    processes, _ = observe_spawn(monkeypatch)
    target = str(supervisor.HELPER_PATH)
    cert = str(tmp_path / "cert.pem")
    path = wrapper(tmp_path, f'''import runpy, ssl
original = ssl.create_default_context
def fixture_context(*args, **kwargs):
    context = original(cafile={cert!r})
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    return context
ssl.create_default_context = fixture_context
ns = runpy.run_path({target!r})
raise SystemExit(ns['main']())
''')
    monkeypatch.setattr(supervisor, "HELPER_PATH", path)

    async def scenario():
        relay = Relay(tls=tls_context)
        port = await relay.start(implicit=security == "ssl")
        send = mailer(port, host="localhost", security=security,
                      username="fixture-user", password="fixture-secret")
        try:
            await send.send(send.compose("subject", "body"))
            assert_reaped(processes)
            assert relay.data_accepted.is_set()
        finally:
            await relay.close()
    asyncio.run(scenario())


def test_implicit_tls_handshake_uses_the_same_whole_deadline(monkeypatch):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, "TRANSPORT_TIMEOUT_S", .45)

    async def scenario():
        relay = Relay("greeting")
        port = await relay.start()
        send = mailer(port, security="ssl")
        began = time.monotonic()
        try:
            with pytest.raises(supervisor.SMTPTransportTimeout):
                await send.send(send.compose("subject", "body"))
            assert .35 <= time.monotonic() - began < 2.45
            assert_reaped(processes)
            await asyncio.wait_for(relay.eof.wait(), 1)
        finally:
            await relay.close()
    asyncio.run(scenario())


def test_repeated_failures_leave_no_helpers_or_file_descriptors(monkeypatch):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        send = mailer(port)
        before = len(os.listdir("/dev/fd"))
        for _ in range(12):
            with pytest.raises(supervisor.SMTPTransportError):
                await send.send(send.compose("subject", "body"))
        await asyncio.sleep(.05)
        assert_reaped(processes)
        assert len(processes) == 12
        assert len(os.listdir("/dev/fd")) == before
    asyncio.run(scenario())


def test_cancellation_during_final_cleanup_is_not_reported_as_delivery_success(monkeypatch):
    processes, _ = observe_spawn(monkeypatch)
    original_reap = supervisor._reap

    async def scenario():
        cleaning, release = asyncio.Event(), asyncio.Event()

        async def delayed_reap(spawn, process):
            cleaning.set()
            await release.wait()
            await original_reap(spawn, process)

        monkeypatch.setattr(supervisor, "_reap", delayed_reap)
        relay = Relay()
        port = await relay.start()
        send = mailer(port)
        task = asyncio.create_task(send.send(send.compose("subject", "body")))
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(processes)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await relay.close()
    asyncio.run(scenario())


def test_safe_diagnostics_include_bounded_phase_duration_and_confirmed_cleanup(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)
    path = wrapper(tmp_path, "import os, sys\nos.write(1, " + repr(supervisor.READY) + ")\nsys.stdin.buffer.read()\nos.write(1, b'FAIL auth auth\\n')\nsys.exit(1)\n")
    monkeypatch.setattr(supervisor, "HELPER_PATH", path)

    async def scenario():
        with pytest.raises(supervisor.SMTPTransportError) as failure:
            await supervisor.send_payload(b"password, recipient and message stay private")
        error = failure.value
        assert error.failure == "auth"
        assert error.phase == "auth"
        assert 0 < error.duration < 2
        assert error.cleanup_confirmed is True
        assert str(error) == "SMTP helper failed: auth"
        assert_reaped(processes)
    asyncio.run(scenario())
