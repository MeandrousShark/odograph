"""Security mail uses its existing mail owners and preserves failure stages."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
import time

import pytest

from app.account_context import AccountPrincipal
from app.admin import _invitation_message
from app.capacity import AdmissionManager
from app.mailer import Mailer
from app.password_reset import SecurityMailAdmission, reset_message
from app.preparation import PreparationOperation
from app.preparation_resources import PreparationBusy, SpoolReservation
from app.security_mail_preparation import SecurityMailConstructionError, SecurityMailSpec
from security_mail_support import read_prepared_message

pytestmark = pytest.mark.unit
BASE = 'https://example.test/odograph'
TOKEN = 'fixture-token+&'


def mailer(**kwargs):
    values = dict(host='smtp.example.test', port=587, username='', password='',
                  security='starttls', tls_insecure=False,
                  from_addr='Sender <sender@example.test>', to_addr='recipient@example.test')
    values.update(kwargs)
    return Mailer(**values)


def baseline(sender, spec):
    if spec.kind == 'reset':
        result = reset_message(sender, spec.link_base, spec.token)
    elif spec.kind == 'invitation':
        from urllib.parse import quote
        result = _invitation_message(sender, f'{spec.link_base}/invite#token={quote(spec.token, safe="")}', spec.token)
    else:
        link = f'{spec.link_base}/settings/account/email/confirm#purpose={spec.purpose}&token={spec.token}'
        result = sender.compose('Confirm your Odograph email address',
            'To confirm your email address, open this link while signed in:\n'
            f'{link}\n\nIf the link does not fill the form, choose {spec.purpose} '
            f'and enter this code manually: {spec.token}\n\n'
            'The code expires in 30 minutes. If you did not request this, ignore this email.')
    return BytesParser(policy=policy.default).parsebytes(result.as_bytes())


@pytest.mark.parametrize('kind,purpose', [('reset', ''), ('invitation', ''),
                                         ('challenge', 'current'), ('challenge', 'change')])
def test_complete_security_body_mime_and_closed_admission_transaction(monkeypatch, tmp_path, kind, purpose):
    async def scenario():
        manager = AdmissionManager()
        admission = SecurityMailAdmission(capacity=manager, spool_root=tmp_path / 'spool')
        sender = mailer(password='界' * 40000)
        spec = SecurityMailSpec(kind, BASE + '/long-' + 'x' * 80000, TOKEN, purpose)
        expected = baseline(sender, spec)
        events = []
        lease_held = False
        @asynccontextmanager
        async def lease():
            nonlocal lease_held
            lease_held = True
            yield
            assert not list((tmp_path / 'spool').glob('op-*'))
            lease_held = False
        @asynccontextmanager
        async def admit():
            events.append('lock')
            yield True
            events.append('unlock')
        async def capture(self, prepared, *, before_transport=None):
            assert lease_held
            assert events == ['lock', 'unlock']
            assert manager.snapshot()['mail']['active'] == 1
            await before_transport()
            actual = read_prepared_message(prepared)
            assert actual.as_bytes(policy=policy.SMTP) == expected.as_bytes(policy=policy.SMTP)
            events.append('sent')
        monkeypatch.setattr(Mailer, 'send_prepared', capture)
        assert await admission.send(sender, spec, admit=admit, lease=lease)
        await admission.drain()
        assert events == ['lock', 'unlock', 'sent']
        assert not lease_held and manager.snapshot()['mail']['active'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['reset', 'invitation', 'challenge'])
def test_header_construction_failure_is_typed_and_never_runs_usability_or_send(tmp_path, kind):
    async def scenario():
        admission = SecurityMailAdmission(spool_root=tmp_path / 'spool')
        entered = False
        @asynccontextmanager
        async def admit():
            nonlocal entered
            entered = True
            yield True
        with pytest.raises(SecurityMailConstructionError) as failure:
            await admission.send(mailer(from_addr='sender@example.test\nBcc: injection'),
                                 SecurityMailSpec(kind, BASE, TOKEN), admit=admit)
        assert failure.value.category == 'ValueError'
        assert not entered and not list((tmp_path / 'spool').glob('op-*'))
        assert admission._slots._value == 2
    asyncio.run(scenario())


def test_reservation_precedes_lifecycle_wait_and_rejection_cleans_inside_lease(monkeypatch, tmp_path):
    async def scenario():
        admission = SecurityMailAdmission(spool_root=tmp_path / 'spool')
        events = []
        @asynccontextmanager
        async def lease():
            assert len(list((tmp_path / 'spool').glob('op-*'))) == 1
            events.append('lease')
            yield
            assert not list((tmp_path / 'spool').glob('op-*'))
            events.append('released')
        @asynccontextmanager
        async def deny():
            events.append('denied')
            yield False
        assert not await admission.send(mailer(), SecurityMailSpec('reset', BASE, TOKEN),
                                        admit=deny, lease=lease)
        assert events == ['lease', 'denied', 'released']
        assert admission._slots._value == 2
    asyncio.run(scenario())


def test_two_mail_owners_share_four_spool_grants_with_foreground_and_background(monkeypatch, tmp_path):
    async def scenario():
        manager = AdmissionManager()
        root = tmp_path / 'spool'
        admission = SecurityMailAdmission(capacity=manager, spool_root=root)
        entered, release = asyncio.Event(), asyncio.Event()
        sends = 0
        async def capture(self, prepared, *, before_transport=None):
            nonlocal sends
            await before_transport()
            sends += 1
            if sends == 2:
                entered.set()
            await release.wait()
        monkeypatch.setattr(Mailer, 'send_prepared', capture)
        principal = AccountPrincipal(41, True, 1)
        async with manager.operation('foreground', principal):
            async with PreparationOperation(spool_root=root):
                async with manager.operation('background', principal):
                    async with PreparationOperation(spool_root=root):
                        tasks = [asyncio.create_task(admission.send(mailer(), SecurityMailSpec('reset', BASE, TOKEN)))
                                 for _ in range(2)]
                        try:
                            await asyncio.wait_for(entered.wait(), 5)
                            assert manager.snapshot()['mail']['active'] == 2
                            assert len(list(root.glob('op-*'))) == 4
                            with pytest.raises(PreparationBusy):
                                SpoolReservation.acquire(root, time.monotonic() + 5)
                            assert not await admission.send(mailer(), SecurityMailSpec('reset', BASE, TOKEN))
                        finally:
                            release.set()
                            assert all(await asyncio.gather(*tasks))
                            await admission.drain()
        assert not list(root.glob('op-*'))
        assert manager.snapshot()['mail']['active'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['construction', 'serialization', 'resource', 'helper_protocol'])
def test_reset_preserves_construction_boundary_and_exact_revocation(monkeypatch, tmp_path, failure):
    from app.password_reset import RecoveryQueue
    from test_password_reset import _FakeDb
    db = _FakeDb(monkeypatch)
    async def scenario():
        root = tmp_path / 'spool'
        admission = SecurityMailAdmission(spool_root=root)
        sender = mailer(from_addr='sender@example.test\nBcc: injection' if failure == 'construction'
                        else 'sender@example.test')
        if failure in ('serialization', 'helper_protocol'):
            # Inject the equivalent failure into the isolated serialization
            # stage, preserving the real construction phase and its protocol.
            from pathlib import Path
            import app.preparation as preparation
            target = preparation._HELPER
            wrapper = tmp_path / 'serialization-helper.py'
            wrapper.write_text(f"import runpy, sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
                "from app.security_mail_renderer import Renderer\n"
                "def fail(*args):\n    raise UnicodeEncodeError('utf8', '', 0, 0, 'fixture failure')\n"
                "Renderer.serialize = fail\n"
                f"runpy.run_path({str(target)!r}, run_name='__main__')\n")
            if failure == 'helper_protocol':
                wrapper.write_text('import os\nos._exit(74)\n')
            monkeypatch.setattr(preparation, '_HELPER', wrapper)
        reservations = [SpoolReservation.acquire(root, time.monotonic() + 5) for _ in range(4)] if failure == 'resource' else []
        queue = RecoveryQueue(object(), BASE, lambda address: sender, admission)
        try:
            if failure == 'construction':
                with pytest.raises(SecurityMailConstructionError):
                    await queue._process(('public', 'typed@example.test'))
                assert db.revoked == []
            else:
                await queue._process(('public', 'typed@example.test'))
                assert db.revoked == [db.issued[0][0]]
        finally:
            await admission.drain()
            for reservation in reservations:
                reservation.release()
        assert not list(root.glob('op-*'))
    asyncio.run(scenario())


def test_challenge_header_construction_failure_revokes_issued_token(monkeypatch, tmp_path):
    import app.auth as auth
    from test_email_challenge_routes import _app, _client
    issued, revoked = [], []
    async def issue(*args):
        issued.append('fixture-token')
        return issued[-1]
    async def revoke(conn, account, purpose, token):
        revoked.append(token)
    monkeypatch.setattr(auth, 'issue_email_challenge', issue)
    monkeypatch.setattr(auth, 'revoke_email_challenge', revoke)
    app = _app(monkeypatch)
    app.state.config.email_from = 'sender@example.test\nBcc: injection'
    app.state.security_mail.spool_root = tmp_path / 'spool'
    async def scenario():
        async with await _client(app) as client:
            await client.get('/seed')
            response = await client.post('/settings/account/email/verify/request', data={
                'current_password': 'correct', 'csrf_token': 'csrf-test'})
        assert response.status_code == 503
        assert issued == revoked == ['fixture-token']
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())


@pytest.mark.parametrize('sender,recipient', [
    ('Sender \udc80 <sender@example.test>', 'recipient@example.test'),
    ('sender@example.test', 'Recipient \udcff <recipient@example.test>'),
    ('Sender \udc80 <sender@example.test>', 'Recipient \udcff <recipient@example.test>'),
])
def test_surrogateescape_headers_preserve_complete_mime_variants_and_envelope(monkeypatch, tmp_path, sender, recipient):
    import json
    import os
    from app.mime_preparation import envelope
    async def scenario():
        admission = SecurityMailAdmission(spool_root=tmp_path / 'spool')
        transport = mailer(from_addr=sender, to_addr=recipient)
        spec = SecurityMailSpec('reset', BASE, TOKEN)
        expected = baseline(transport, spec)
        expected_sender, expected_recipients, international = envelope(expected)
        async def receive(self, prepared, *, before_transport=None):
            stack, descriptors, guard = prepared.open_descriptors()
            try:
                await before_transport()
                with os.fdopen(os.dup(descriptors[1]), 'rb') as source:
                    assert json.load(source) == dict(sender=expected_sender,
                        recipients=expected_recipients, international=international)
                for fd, utf8 in ((descriptors[2], False), (descriptors[3], True)):
                    selected = expected.policy.clone(utf8=True) if utf8 else expected.policy
                    with os.fdopen(os.dup(fd), 'rb') as source:
                        assert source.read() == expected.as_bytes(policy=selected.clone(linesep='\r\n'))
            finally:
                stack.close()
        monkeypatch.setattr(Mailer, 'send_prepared', receive)
        assert await admission.send(transport, spec)
        await admission.drain()
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())


@pytest.mark.parametrize('sender', ['high\ud800 <sender@example.test>', 'newline\n\ud800',
                                   'pair\ud800\udc80 <sender@example.test>'])
def test_surrogate_header_error_precedence_matches_legacy_constructor(tmp_path, sender):
    async def scenario():
        transport = mailer(from_addr=sender)
        spec = SecurityMailSpec('reset', BASE, TOKEN)
        with pytest.raises(Exception) as expected:
            baseline(transport, spec)
        admission = SecurityMailAdmission(spool_root=tmp_path / 'spool')
        with pytest.raises(SecurityMailConstructionError) as actual:
            await admission.send(transport, spec)
        assert actual.value.category == type(expected.value).__name__
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,field', [
    (kind, field) for kind in ('reset', 'invitation', 'challenge')
    for field in ('link_base', 'token', 'purpose')
    if field != 'purpose' or kind == 'challenge'
])
def test_adjacent_surrogates_keep_security_construction_failure_phase(tmp_path, kind, field):
    async def scenario():
        values = dict(kind=kind, link_base=BASE, token=TOKEN, purpose='current')
        values[field] += '\ud800\udc80'
        spec = SecurityMailSpec(**values)
        with pytest.raises(UnicodeEncodeError):
            baseline(mailer(), spec)
        admission = SecurityMailAdmission(spool_root=tmp_path / 'spool')
        with pytest.raises(SecurityMailConstructionError) as actual:
            await admission.send(mailer(), spec)
        assert actual.value.category == 'UnicodeEncodeError'
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())
