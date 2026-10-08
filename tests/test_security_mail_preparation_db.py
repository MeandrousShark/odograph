"""Prepared security delivery retains the existing managed roles and lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import secrets

import pytest

from app.account_context import control_connection
from app.account_work import external_account_work
from app.mailer import Mailer
from app.password_reset import SecurityMailAdmission, issue_password_reset, password_reset_send_usable
from app.security_mail_preparation import SecurityMailSpec
from security_mail_support import read_prepared_message
from test_capacity_db import _scenario

pytestmark = [pytest.mark.db, pytest.mark.capacity_contract,
              pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='requires disposable PostGIS')]


@pytest.mark.parametrize('denied', [False, True])
def test_real_managed_control_usability_closes_before_prepared_send_and_retains_lease(monkeypatch, tmp_path, denied):
    async def scenario():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            await control._pool.resize(min_size=1, max_size=5)
            account, _ = accounts
            identifier = account.principal.account_id
            address = 'security-mail@example.test'
            async with raw.connection() as conn:
                await conn.execute("UPDATE accounts SET email=%s,email_verified_at=now(),password_hash='hash' WHERE id=%s",
                                   (address, identifier))
            token = secrets.token_urlsafe(32)
            async with control_connection(control) as conn:
                delivered_to = await issue_password_reset(conn, token, initiator='public', email=address)
            assert delivered_to == address
            if denied:
                async with raw.connection() as conn:
                    await conn.execute('UPDATE accounts SET is_enabled=false WHERE id=%s', (identifier,))
            root = tmp_path / 'spool'
            admission = SecurityMailAdmission(capacity=manager, spool_root=root)
            sender = Mailer('smtp.example.test', 587, '', '', 'starttls', False,
                            'sender@example.test', address)
            events = []
            unlocked = asyncio.Event()
            @asynccontextmanager
            async def admit():
                async with control_connection(control, lane='mail') as conn:
                    async with conn.transaction():
                        assert (await (await conn.execute('SELECT current_user')).fetchone())[0] == 'odograph_control'
                        events.append('lock')
                        yield await password_reset_send_usable(conn, token)
                events.append('unlock')
                unlocked.set()
            async def receiver(self, prepared, *, before_transport=None):
                # Task creation remains inside usability; the commit can run
                # while transport is pending, and completes before its await.
                await unlocked.wait()
                assert events == ['lock', 'unlock']
                assert manager.snapshot()['leases'] == 1
                assert manager.snapshot()['mail']['active'] == 1
                assert manager.snapshot()['routine']['active'] == 0
                await before_transport()
                message = read_prepared_message(prepared)
                assert token in message.get_content()
                assert str(message['To']) == address
                assert len(list(root.glob('op-*'))) == 1
                events.append('sent')
            monkeypatch.setattr(Mailer, 'send_prepared', receiver)
            admitted = await admission.send(sender, SecurityMailSpec('reset', 'https://example.test', token),
                admit=admit, lease=lambda: external_account_work(control, identifier))
            await admission.drain()
            assert admitted is not denied
            assert events == (['lock', 'unlock'] if denied else ['lock', 'unlock', 'sent'])
            assert manager.snapshot()['leases'] == 0
            assert manager.snapshot()['mail']['active'] == 0
            assert not list(root.glob('op-*'))
            assert runtime._pool.max_size == 6 and control._pool.max_size == 5
    asyncio.run(scenario())


def test_cancelled_request_leaves_mail_owner_lease_and_files_until_real_delivery_finishes(monkeypatch, tmp_path):
    async def scenario():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            account, _ = accounts
            root = tmp_path / 'spool'
            admission = SecurityMailAdmission(capacity=manager, spool_root=root)
            entered, release = asyncio.Event(), asyncio.Event()
            async def receiver(self, prepared, *, before_transport=None):
                await before_transport()
                entered.set()
                await release.wait()
                assert manager.snapshot()['leases'] == 1
            monkeypatch.setattr(Mailer, 'send_prepared', receiver)
            sender = Mailer('smtp.example.test', 587, '', '', 'starttls', False,
                            'sender@example.test', 'recipient@example.test')
            task = asyncio.create_task(admission.send(sender, SecurityMailSpec('reset', 'https://example.test', 'fixture'),
                lease=lambda: external_account_work(control, account.principal.account_id)))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert manager.snapshot()['leases'] == 1
                assert manager.snapshot()['mail']['active'] == 1
                assert len(list(root.glob('op-*'))) == 1
            finally:
                release.set()
                await admission.drain()
            assert manager.snapshot()['leases'] == 0
            assert manager.snapshot()['mail']['active'] == 0
            assert not list(root.glob('op-*'))
    asyncio.run(scenario())
