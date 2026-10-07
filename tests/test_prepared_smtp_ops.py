"""Prepared MIME uses the real isolated SMTP helper and owned spool files."""
from __future__ import annotations

import asyncio
import json
import os
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import socket
import threading
import time

import pytest

from app.mime_preparation import prepare_quarterly_mail
from app.mailer import Mailer
from app.prepared_mail import PreparedMail
from app.preparation_resources import ResourceBudget, SpoolReservation
from app import smtp_supervisor as supervisor
from test_smtp_supervisor_ops import Relay, assert_reaped, observe_spawn, tls_context
from test_mime_preparation import SMTP

pytestmark = pytest.mark.ops


def prepare(root, port, *, body='.one\n..two\né\n', sender='sender@example.test',
            recipient='recipient@example.test', **options):
    reservation = SpoolReservation.acquire(root, time.monotonic() + 5)
    budget = ResourceBudget(reservation.directory, reservation.directory_fd)
    config = dict(host='127.0.0.1', port=port, username='', password='',
                  security='none', tls_insecure=False)
    config.update(options)
    for name, value in [('body', body), ('from', sender), ('to', recipient),
                        ('config', json.dumps(config))]:
        with budget.open(name, 'wb') as sink:
            sink.write(value.encode('utf8'))
    artifacts = prepare_quarterly_mail(budget, 'body', 'from', 'to', 'config')
    budget.close()
    return PreparedMail(reservation, artifacts)


@pytest.mark.parametrize('security', ['none', 'ssl', 'starttls'])
def test_descriptor_complete_wire_matches_legacy(monkeypatch, tmp_path, tls_context, security):
    processes, calls = observe_spawn(monkeypatch)

    async def scenario():
        body = '.dot\n..two\rsolo\r\n' + 'é界' * 50000
        relay = Relay(tls=tls_context)
        port = await relay.start(implicit=security == 'ssl')
        prepared = prepare(tmp_path / 'spool', port, body=body, security=security,
                           tls_insecure=True, username='fixture-user', password='fixture-secret')
        try:
            await supervisor.send_prepared(prepared)
            original = EmailMessage()
            original['From'] = 'sender@example.test'
            original['To'] = 'recipient@example.test'
            original['Subject'] = 'Odograph: log an odometer reading'
            original.set_content(body)
            baseline = BytesParser(policy=policy.default).parsebytes(original.as_bytes())
            oracle = SMTP()
            oracle.send_message(baseline)
            # Relay stores DATA without its terminator.
            assert relay.messages == [bytes(oracle.wire[:-3])]
            assert_reaped(processes)
            args, kwargs = calls[0]
            assert 'fd2' in args
            assert kwargs['env'] == {} and kwargs['close_fds'] is True
            assert len(kwargs['pass_fds']) == 6
            assert 'fixture-secret' not in repr(args)
            assert prepared.reservation.guard in kwargs['pass_fds']
            assert prepared.reservation.directory_fd not in kwargs['pass_fds']
        finally:
            await relay.close()
            prepared.reservation.release()
    asyncio.run(scenario())


def test_configuration_has_no_aggregate_64k_cap(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        relay = Relay()
        port = await relay.start()
        prepared = prepare(tmp_path / 'spool', port, password='界' * 100000)
        try:
            await supervisor.send_prepared(prepared)
            assert relay.data_accepted.is_set()
            assert_reaped(processes)
        finally:
            await relay.close()
            prepared.reservation.release()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['greeting', 'ehlo', 'auth', 'rcpt', 'data',
                                  'data_response', 'quit', 'starttls', 'tls', 'trickle'])
def test_descriptor_repeated_cancellation_reaps_before_spool_release(monkeypatch, tmp_path, phase):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        relay = Relay(phase)
        port = await relay.start()
        prepared = prepare(tmp_path / 'spool', port,
                           security='starttls' if phase in ('starttls', 'tls') else 'none',
                           username='user' if phase == 'auth' else '')
        task = asyncio.create_task(supervisor.send_prepared(prepared))
        try:
            await asyncio.wait_for(relay.stalled.wait(), 2)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(processes)
            await asyncio.wait_for(relay.eof.wait(), 1)
            prepared.reservation.validate()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await relay.close()
            prepared.reservation.release()
    asyncio.run(scenario())


def test_descriptor_deadline_bounds_trickle_and_confirms_cleanup(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, 'TRANSPORT_TIMEOUT_S', .4)

    async def scenario():
        relay = Relay('trickle')
        port = await relay.start()
        prepared = prepare(tmp_path / 'spool', port)
        try:
            with pytest.raises(supervisor.SMTPTransportTimeout) as failure:
                await supervisor.send_prepared(prepared)
            assert failure.value.cleanup_confirmed
            assert .3 < failure.value.duration < 2
            assert_reaped(processes)
        finally:
            await relay.close()
            prepared.reservation.release()
    asyncio.run(scenario())


def test_pre_transport_callback_runs_with_verified_files_before_transport_clock(monkeypatch, tmp_path):
    processes, _ = observe_spawn(monkeypatch)
    monkeypatch.setattr(supervisor, 'TRANSPORT_TIMEOUT_S', .3)

    async def scenario():
        relay = Relay()
        port = await relay.start()
        prepared = prepare(tmp_path / 'spool', port)
        called = False
        async def finish():
            nonlocal called
            assert not processes
            prepared.reservation.validate()
            await asyncio.sleep(.4)
            called = True
        try:
            await supervisor.send_prepared(prepared, before_transport=finish)
            assert called and relay.data_accepted.is_set()
            assert_reaped(processes)
        finally:
            await relay.close()
            prepared.reservation.release()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_pre_transport_callback_failure_closes_files_without_launch(monkeypatch, tmp_path, cancel):
    processes, _ = observe_spawn(monkeypatch)

    async def scenario():
        prepared = prepare(tmp_path / 'spool', 1)
        baseline = len(os.listdir('/dev/fd'))
        entered = asyncio.Event()
        async def finish():
            entered.set()
            if cancel:
                await asyncio.Future()
            raise LookupError('preparation failure')
        task = asyncio.create_task(supervisor.send_prepared(prepared, before_transport=finish))
        try:
            await entered.wait()
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else LookupError):
                await task
            assert not processes
            assert len(os.listdir('/dev/fd')) == baseline
        finally:
            prepared.reservation.release()
    asyncio.run(scenario())


def test_cancelled_descriptor_opening_waits_for_thread_and_closes_returned_files(monkeypatch, tmp_path):
    import threading
    processes, _ = observe_spawn(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = PreparedMail.open_descriptors
    def delayed_open(prepared):
        files = original(prepared)
        entered.set()
        release.wait(5)
        return files
    monkeypatch.setattr(PreparedMail, 'open_descriptors', delayed_open)

    async def scenario():
        prepared = prepare(tmp_path / 'spool', 1)
        baseline = len(os.listdir('/dev/fd'))
        task = asyncio.create_task(supervisor.send_prepared(prepared))
        try:
            while not entered.is_set():
                await asyncio.sleep(.005)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
                task.cancel()
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not processes
            assert len(os.listdir('/dev/fd')) == baseline
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            prepared.reservation.release()
    asyncio.run(scenario())


class LocalRelay:
    def __init__(self, *, security='none', support=True, refusal='none', stall=False, context=None):
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen();self.listener.settimeout(40)
        self.port=self.listener.getsockname()[1];self.security=security;self.support=support;self.refusal=refusal;self.stall=stall;self.context=context
        self.commands=[];self.data=bytearray();self.errors=[]
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        try:
            conn,_=self.listener.accept();conn.settimeout(40)
            if self.security=='ssl':conn=self.context.wrap_socket(conn,server_side=True)
            def write(value):conn.sendall(value)
            if self.stall:
                for _ in range(33):write(b'x');time.sleep(1)
                return
            write(b'220 local.test ready\r\n');stream=conn.makefile('rb')
            while line:=stream.readline():
                command=line.rstrip(b'\r\n');self.commands.append(command)
                verb=command.split(b' ',1)[0].upper()
                if verb in (b'EHLO',b'HELO'):
                    write(b'250-local.test\r\n250-SIZE 999999999\r\n250-AUTH PLAIN LOGIN\r\n')
                    if self.support:write(b'250-SMTPUTF8\r\n')
                    write(b'250 STARTTLS\r\n')
                elif verb==b'STARTTLS':
                    write(b'220 begin TLS\r\n');stream.close();conn=self.context.wrap_socket(conn,server_side=True);stream=conn.makefile('rb')
                elif verb==b'AUTH':write(b'235 authenticated\r\n')
                elif verb==b'MAIL':write(b'550 refused\r\n' if self.refusal=='mail' else b'250 sender\r\n')
                elif verb==b'RCPT':
                    reject=self.refusal=='all' or(self.refusal=='partial' and b'first@' in command)
                    write(b'550 refused\r\n' if reject else b'250 recipient\r\n')
                elif verb==b'DATA':
                    if self.refusal=='data':write(b'550 refused\r\n');continue
                    write(b'354 send data\r\n')
                    while chunk:=stream.readline():
                        self.data.extend(chunk)
                        if chunk==b'.\r\n':break
                    write(b'250 accepted\r\n')
                elif verb==b'RSET':write(b'250 reset\r\n')
                elif verb==b'QUIT':write(b'550 quit refused\r\n' if self.refusal=='quit' else b'221 bye\r\n');break
                else:write(b'500 unknown\r\n')
            stream.close();conn.close()
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:self.errors.append(type(exc).__name__)
        finally:self.listener.close()
    def join(self):self.thread.join(40);assert not self.thread.is_alive();assert not self.errors,self.errors


@pytest.mark.parametrize('support,refusal', [(True, kind) for kind in
                         ('none', 'partial', 'all', 'mail', 'data', 'quit')]
                         + [(False, 'none')])
def test_real_descriptor_envelope_refusal_and_data_match_legacy(monkeypatch, tmp_path, support, refusal):
    async def outcome(send):
        try:
            await send
            return 'OK'
        except supervisor.SMTPTransportError as exc:
            return exc.failure, exc.phase

    async def scenario():
        body = '.dot\n..two\n' + 'é' * 100 + '\n'
        sender, recipient = 'sender@example.test', 'first@example.test, 用户@example.test'
        baseline_relay = LocalRelay(support=support, refusal=refusal)
        mailer = Mailer('127.0.0.1', baseline_relay.port, '', '', 'none', False,
                        sender, recipient)
        expected = await outcome(mailer.send(mailer.compose('Odograph: log an odometer reading', body)))
        baseline_relay.join()
        prepared_relay = LocalRelay(support=support, refusal=refusal)
        prepared = prepare(tmp_path / 'spool', prepared_relay.port, body=body,
                           sender=sender, recipient=recipient)
        try:
            actual = await outcome(supervisor.send_prepared(prepared))
            prepared_relay.join()
            assert actual == expected
            assert prepared_relay.commands == baseline_relay.commands
            assert prepared_relay.data == baseline_relay.data
        finally:
            prepared.reservation.release()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['readiness', 'auth'])
def test_resource_stop_is_safe_and_confirmed_before_files_close(monkeypatch, tmp_path, phase):
    processes, _ = observe_spawn(monkeypatch)
    path = tmp_path / 'helper.py'
    content = 'import os, sys\n'
    if phase != 'readiness':
        content += "os.write(1, b'SMTPFD2 READY\\n')\nsys.stdin.buffer.read()\nos.write(1, b'FAIL resource auth\\n')\n"
    content += 'sys.exit(73)\n'
    path.write_text(content)
    monkeypatch.setattr(supervisor, 'HELPER_PATH', path)

    async def scenario():
        prepared = prepare(tmp_path / 'spool', 1)
        try:
            with pytest.raises(supervisor.SMTPTransportError) as failure:
                await supervisor.send_prepared(prepared)
            assert failure.value.failure == 'resource'
            assert failure.value.phase == phase
            assert failure.value.cleanup_confirmed
            assert_reaped(processes)
        finally:
            prepared.reservation.release()
    asyncio.run(scenario())


def test_cancelled_late_prepared_spawn_retains_descriptors_until_reap(monkeypatch, tmp_path):
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    async def delayed_spawn(*args, **kwargs):
        await asyncio.sleep(.2)
        # These handles must still exist when the delayed real spawn begins.
        for fd in kwargs['pass_fds']:
            os.fstat(fd)
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)

    async def scenario():
        prepared = prepare(tmp_path / 'spool', 1)
        task = asyncio.create_task(supervisor.send_prepared(prepared))
        try:
            await asyncio.sleep(.05)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(.01)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(processes)
        finally:
            prepared.reservation.release()
    asyncio.run(scenario())


def test_real_child_has_only_readonly_artifacts_lifetime_and_guard(monkeypatch, tmp_path):
    original_spawn = asyncio.create_subprocess_exec
    helper = supervisor.HELPER_PATH
    sentinel = os.open(os.devnull, os.O_RDONLY)
    os.set_inheritable(sentinel, True)
    monkeypatch.setenv('DATABASE_URL', 'must-not-enter-child')
    monkeypatch.setenv('SMTP_PASSWORD', 'must-not-enter-child')

    async def inspected_spawn(*args, **kwargs):
        expected = {0, 1, 2, *kwargs['pass_fds']}
        artifacts = kwargs['pass_fds'][1:5]
        path = tmp_path / 'inspect-helper.py'
        path.write_text(f'''import fcntl, os, runpy
assert not os.environ.get('DATABASE_URL')
assert not os.environ.get('SMTP_PASSWORD')
expected = {expected!r}
for fd in range(256):
    try:
        os.fstat(fd)
    except OSError:
        continue
    assert fd in expected, fd
for fd in {artifacts!r}:
    assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
runpy.run_path({str(helper)!r}, run_name='__main__')
''')
        arguments = list(args)
        arguments[2] = str(path)
        return await original_spawn(*arguments, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', inspected_spawn)

    async def scenario():
        relay = Relay()
        port = await relay.start()
        prepared = prepare(tmp_path / 'spool', port)
        try:
            await supervisor.send_prepared(prepared)
            assert relay.data_accepted.is_set()
        finally:
            await relay.close()
            prepared.reservation.release()
    try:
        asyncio.run(scenario())
    finally:
        os.close(sentinel)


@pytest.mark.parametrize('sender,recipient', [
    ('sender@example.test\nBcc: injected@example.test', 'one@example.test'),
    ('sender@example.test\r\nSubject: injected', 'one@example.test'),
    ('sender@example.test', 'one@example.test\nBcc: injected@example.test'),
    ('sender@example.test', 'one@example.test\rCc: injected@example.test'),
    ('Malformed <sender@example.test', 'missing-at-sign, duplicate@example.test, duplicate@example.test'),
    ('"unterminated', 'Group: one@example.test, second@example.test;'),
])
def test_malformed_header_construction_and_address_parsing_match_baseline(tmp_path, sender, recipient):
    def outcome(function):
        try:
            return 'result', function()
        except Exception as exc:
            return 'error', type(exc), exc.args

    def baseline():
        message = Mailer('', 0, '', '', 'none', False, sender, recipient).compose(
            'Odograph: log an odometer reading', '.body\né\n')
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        smtp = SMTP()
        smtp.send_message(parsed)
        return smtp.commands, bytes(smtp.wire)

    reservation = SpoolReservation.acquire(tmp_path / 'spool', time.monotonic() + 5)
    budget = ResourceBudget(reservation.directory, reservation.directory_fd)
    try:
        for name, value in [('body', '.body\né\n'), ('from', sender), ('to', recipient)]:
            with budget.open(name, 'wb') as sink:
                sink.write(value.encode('utf8'))
        def candidate():
            from app.mime_preparation import send_spool
            artifacts = prepare_quarterly_mail(budget, 'body', 'from', 'to', 'config')
            with budget.open(artifacts['envelope'], 'rb') as source:
                envelope = json.load(source)
            smtp = SMTP()
            selected = artifacts['mime_utf8'] if envelope['international'] else artifacts['mime']
            with budget.open(selected, 'rb') as source:
                send_spool(smtp, envelope['sender'], envelope['recipients'], source,
                           envelope['international'])
            return smtp.commands, bytes(smtp.wire)
        assert outcome(candidate) == outcome(baseline)
    finally:
        budget.close()
        reservation.release()


@pytest.mark.parametrize('phase', ['greeting', 'quit'])
def test_actual_prepared_parent_death_reaps_network_helper_and_reclaims_orphan(tmp_path, phase):
    import ctypes
    from pathlib import Path
    import signal
    import subprocess
    import sys
    from app.preparation_resources import PreparationBusy

    libc = None
    previous_subreaper = ctypes.c_int()
    if sys.platform == 'linux':
        libc = ctypes.CDLL(None)
        assert libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0) == 0
        assert libc.prctl(36, 1, 0, 0, 0) == 0
    source = Path(__file__).resolve().parents[1]
    parent_path = tmp_path / 'prepared-parent.py'
    parent_path.write_text('''import json, os, struct, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from app.mime_preparation import prepare_quarterly_mail
from app.prepared_mail import PreparedMail
from app.preparation_resources import ResourceBudget, SpoolReservation
from app.smtp_supervisor import HELPER_PATH
reservation = SpoolReservation.acquire(sys.argv[2], time.monotonic() + 5)
budget = ResourceBudget(reservation.directory, reservation.directory_fd)
config = dict(host='127.0.0.1', port=int(sys.argv[3]), username='', password='', security='none', tls_insecure=False)
for name, value in [('body', '.body\\n'), ('from', 'sender@example.test'), ('to', 'recipient@example.test'), ('config', json.dumps(config))]:
    with budget.open(name, 'wb') as sink:
        sink.write(value.encode('utf8'))
artifacts = prepare_quarterly_mail(budget, 'body', 'from', 'to', 'config')
budget.close()
stack, descriptors, guard = PreparedMail(reservation, artifacts).open_descriptors()
read, write = os.pipe()
child = subprocess.Popen([sys.executable, '-I', str(HELPER_PATH), str(time.monotonic() + 30), str(os.getpid()), str(read), 'fd2'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True, pass_fds=(read, *descriptors, guard))
os.close(read)
assert child.stdout.readline() == b'SMTPFD2 READY\\n'
child.stdin.write(struct.pack('!4i', *descriptors))
child.stdin.flush()
child.stdin.close()
stack.close()
# The child guard alone must retain this orphan's reservation charge.
os.close(reservation.guard)
os.close(reservation.directory_fd)
os.close(reservation.root_fd)
print(json.dumps(dict(pid=child.pid, name=reservation.name)), flush=True)
time.sleep(60)
''')

    async def scenario():
        root = tmp_path / 'spool'
        observers = [SpoolReservation.acquire(root, time.monotonic() + 5) for _ in range(3)]
        relay = Relay(phase)
        port = await relay.start()
        parent = subprocess.Popen([sys.executable, '-I', str(parent_path), str(source), str(root), str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        helper_pid = None
        try:
            identity = json.loads(await asyncio.to_thread(parent.stdout.readline))
            helper_pid = identity['pid']
            if phase == 'quit':
                await asyncio.wait_for(relay.data_accepted.wait(), 3)
            else:
                await asyncio.wait_for(relay.accepted.wait(), 3)
            with pytest.raises(PreparationBusy, match='reservation exhausted'):
                SpoolReservation.acquire(root, time.monotonic() + 5)
            assert (root / identity['name']).is_dir()
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
            await asyncio.wait_for(relay.eof.wait(), 1)
            assert time.monotonic() - began < 2
            recovered = SpoolReservation.acquire(root, time.monotonic() + 5)
            assert not (root / identity['name']).exists()
            assert len(list(root.glob('op-*'))) == 4
            recovered.release()
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
            for reservation in observers:
                reservation.release()
    try:
        asyncio.run(scenario())
    finally:
        if libc is not None:
            assert libc.prctl(36, previous_subreaper.value, 0, 0, 0) == 0
