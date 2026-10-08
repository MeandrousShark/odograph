"""Real restricted-role quarterly projections and coherent streamed names."""
import asyncio
import os
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from psycopg.errors import LockNotAvailable
from psycopg import DataError

from app.account_context import account_id
from app.account_work import external_account_work
from app.capacity import AdmissionManager
from app.db import make_pool
from app.email_digest import _render
from app.notification_preparation import (
    QuarterlyJob, capture_email_settings, capture_quarterly_job, prepare_quarterly_message,
    quarterly_preferences_current, replay_quarterly_job, select_email_turn,
)
from app.notifications import odometer_reminder_vehicles
from app.preparation import PreparationOperation
from conftest import add_test_account, reset_account_db

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),
    reason='requires disposable PostGIS')]
NOW = datetime.fromisoformat('2026-10-01T17:00:00+00:00')
CONFIG = SimpleNamespace(app_url='https://example.invalid/', email_from='Odograph <from@example.com>',
    smtp_host='smtp.invalid', smtp_port=25, smtp_username='', smtp_password='',
    smtp_security='none', smtp_tls_insecure=False)


def decoded_oracle(body):
    message = EmailMessage()
    message.set_content(body)
    return BytesParser(policy=policy.default).parsebytes(message.as_bytes().replace(b'\n',b'\r\n')).get_content()


async def seed(pool):
    names = ['Z', 'a', 'A', 'é', 'e\u0301', 'duplicate', 'duplicate', '', '😀車' * 40000]
    async with pool.connection() as conn:
        await conn.execute('UPDATE vehicles SET active=false WHERE account_id=%s', (account_id(conn),))
        await conn.execute("UPDATE account_settings SET display_tz='America/Los_Angeles',"
            "email_to='Group: a@example.com, a@example.com;',email_weekly_nudge=false,"
            'email_monthly_summary=false,email_filing_reminder=false,email_odometer_reminder=true,'
            'odometer_reminder_hour=9 WHERE account_id=%s', (account_id(conn),))
        ids = []
        for name in names:
            row = await (await conn.execute('INSERT INTO vehicles(account_id,name,active) VALUES(%s,%s,true) RETURNING id',
                (account_id(conn),name))).fetchone()
            ids.append(row[0])
        # At-boundary and future readings exclude vehicles; earlier readings do not.
        boundary = datetime.fromisoformat('2026-10-01T09:00:00-07:00')
        for identity, recorded in ((ids[0],boundary),(ids[1],boundary+timedelta(days=1)),
                                   (ids[2],boundary-timedelta(microseconds=1))):
            await conn.execute('INSERT INTO odometer_readings(account_id,vehicle_id,recorded_at,odometer_m) VALUES(%s,%s,%s,1000)',
                (account_id(conn),identity,recorded))
        await conn.execute('UPDATE vehicles SET active=false WHERE account_id=%s AND id=%s', (account_id(conn),ids[3]))
    return names, ids


async def with_pool(scenario):
    raw = make_pool(os.environ['TEST_DATABASE_URL'])
    await raw.open(wait=True)
    try:
        pool = await reset_account_db(raw)
        await scenario(raw,pool)
    finally:
        await raw.close()


async def captured(pool, operation):
    async with pool.connection() as conn:
        selection = await select_email_turn(conn, operation=operation)
        assert selection.kind == 'quarterly_odometer' and not selection.ready and selection.cursor is None
        assert (await (await conn.execute('SELECT session_user')).fetchone())[0] == 'odograph_runtime'
        return await capture_quarterly_job(conn, operation, CONFIG, now=NOW)


def test_restricted_projection_matches_eligibility_body_and_account_scope(tmp_path):
    async def scenario(raw,pool):
        await seed(pool)
        other = await add_test_account(raw,52)
        async with other.connection() as conn:
            await conn.execute("INSERT INTO vehicles(account_id,name) VALUES(%s,'foreign vehicle')", (account_id(conn),))
        async with pool.connection() as conn:
            expected = await odometer_reminder_vehicles(conn,datetime.fromisoformat('2026-10-01T09:00:00-07:00'))
        async with AdmissionManager().operation('foreground',pool.principal):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def work():
                    async with external_account_work(pool.control_pool,pool.principal.account_id):
                        try:
                            job = await captured(pool,operation)
                            assert job.quarter_start == datetime.fromisoformat('2026-10-01T09:00:00-07:00')
                            async with pool.connection() as conn:
                                assert await quarterly_preferences_current(conn,job)
                                result = await prepare_quarterly_message(conn,job)
                                assert result.due_count == len(expected)
                                with operation.budget.open(result.artifacts['mime'],'rb') as source:
                                    actual = BytesParser(policy=policy.default).parse(source)
                                body = _render('quarterly_odometer.txt',vehicles=', '.join(expected),noun='vehicles',
                                    settings_url=CONFIG.app_url+'/settings')
                                assert actual.get_content() == decoded_oracle(body)
                                assert str(actual['Subject']) == 'Odograph: log an odometer reading'
                                assert 'foreign vehicle' not in actual.get_content()
                                await operation.finish_preparation()
                        finally:
                            await operation.close()
                await operation.perform(work)
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(with_pool(scenario))


def test_preference_capture_rejects_changed_complete_values_and_holds_row_lock(tmp_path):
    async def scenario(raw,pool):
        await seed(pool)
        async with AdmissionManager().operation('foreground',pool.principal):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def work():
                    async with external_account_work(pool.control_pool,pool.principal.account_id):
                        try:
                            job = await captured(pool,operation)
                            async with pool.connection() as conn:
                                assert await quarterly_preferences_current(conn,job)
                                async with raw.connection() as writer:
                                    with pytest.raises(LockNotAvailable):
                                        async with writer.transaction():
                                            await writer.execute("SET LOCAL lock_timeout='50ms'")
                                            await writer.execute('UPDATE account_settings SET email_to=%s WHERE account_id=%s',
                                                ('changed@example.com',pool.principal.account_id))
                            async with pool.connection() as conn:
                                await conn.execute('UPDATE account_settings SET email_to=%s WHERE account_id=%s',
                                    ('é😀<&' * 30000 + 'tail@example.com',account_id(conn)))
                            async with pool.connection() as conn:
                                assert not await quarterly_preferences_current(conn,job)
                            assert operation.process.returncode is None
                        finally:
                            await operation.close()
                await operation.perform(work)
    asyncio.run(with_pool(scenario))


def test_name_chunks_share_statement_snapshot_during_rename(tmp_path):
    async def scenario(raw,pool):
        names,ids = await seed(pool)
        original = names[-1]
        async with AdmissionManager().operation('foreground',pool.principal):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def work():
                    async with external_account_work(pool.control_pool,pool.principal.account_id):
                        try:
                            job = await captured(pool,operation)
                            class Session:
                                mutated = False
                                name_active = False
                                async def send_command(self,command):
                                    if command['type']=='text':
                                        self.name_active = command['size'] == len(original.encode('utf8'))
                                    await job.session.send_command(command)
                                async def send_text(self,value):
                                    await job.session.send_text(value)
                                    if self.name_active and not self.mutated:
                                        self.mutated = True
                                        async with raw.connection() as writer:
                                            await writer.execute("UPDATE vehicles SET name='new name' WHERE account_id=%s AND id=%s",
                                                (pool.principal.account_id,ids[-1]))
                                async def request(self,command): return await job.session.request(command)
                                async def finish_input(self): return await job.session.finish_input()
                            wrapper = Session()
                            copied = QuarterlyJob(operation,wrapper,job.quarter_start,job.hour)
                            async with pool.connection() as conn:
                                expected = await odometer_reminder_vehicles(conn,job.quarter_start)
                                result = await prepare_quarterly_message(conn,copied)
                                assert wrapper.mutated
                                with operation.budget.open(result.artifacts['mime'],'rb') as source:
                                    message = BytesParser(policy=policy.default).parse(source)
                                expected_body = _render('quarterly_odometer.txt',vehicles=', '.join(expected),noun='vehicles',
                                    settings_url=CONFIG.app_url+'/settings')
                                assert message.get_content() == decoded_oracle(expected_body)
                                assert original in message.get_content() and 'new name' not in message.get_content()
                                await operation.finish_preparation()
                        finally:
                            await operation.close()
                await operation.perform(work)
    asyncio.run(with_pool(scenario))


def test_legacy_capture_replay_retains_initial_flags_time_and_stale_preferences(tmp_path):
    async def scenario(raw,pool):
        await seed(pool)
        initial_to = 'Name😀é' * 30000 + ' <a@example.com>'
        async with pool.connection() as conn:
            await conn.execute('UPDATE account_settings SET email_to=%s WHERE account_id=%s',
                (initial_to,account_id(conn)))
        async with AdmissionManager().operation('foreground',pool.principal):
            async with PreparationOperation(spool_root=tmp_path/'spool') as initial:
                async def capture():
                    async with external_account_work(pool.control_pool,pool.principal.account_id):
                        async with pool.connection() as conn:
                            return await capture_email_settings(conn,initial,CONFIG,now=NOW)
                snapshot = await initial.perform(capture)
                await initial.finish_preparation()
                assert initial.process.returncode == 0 and snapshot.enabled == (False,False,False,True)
                assert snapshot.now == NOW
                assert snapshot.references['email_to'][1] == len(initial_to.encode('utf8'))
                async with pool.connection() as conn:
                    await conn.execute("UPDATE account_settings SET email_to='changed@example.com',"
                        "display_tz='UTC',email_weekly_nudge=true,email_odometer_reminder=false,odometer_reminder_hour=10 "
                        'WHERE account_id=%s', (account_id(conn),))
                async with PreparationOperation(spool_root=tmp_path/'spool') as current:
                    async def replay():
                        async with external_account_work(pool.control_pool,pool.principal.account_id):
                            try:
                                job = await replay_quarterly_job(snapshot,current)
                                assert job.quarter_start == datetime.fromisoformat('2026-10-01T09:00:00-07:00')
                                assert job.hour == 9
                                assert snapshot.enabled == (False,False,False,True)
                                async with pool.connection() as conn:
                                    assert not await quarterly_preferences_current(conn,job)
                                assert current.budget.usage()[0] > len(initial_to.encode('utf8'))
                                assert initial.directory.exists() and current.directory.exists()
                            finally:
                                await current.close()
                    await current.perform(replay)
                assert initial.directory.exists()
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(with_pool(scenario))


@pytest.mark.parametrize('encoding,names',[
    ('LATIN1',['éÿ'*40000,'A','é']),
    ('SJIS',['¥〜−'*30000,'A','¥']),
])
def test_binary_projection_matches_actual_client_decoded_baseline(tmp_path,encoding,names):
    async def scenario(raw,pool):
        await seed(pool)
        async with pool.connection() as conn:
            await conn.execute('UPDATE vehicles SET active=false WHERE account_id=%s',(account_id(conn),))
            for name in names:
                await conn.execute('INSERT INTO vehicles(account_id,name,active) VALUES(%s,%s,true)',(account_id(conn),name))
            await conn.execute("UPDATE account_settings SET display_tz='UTC',email_to='a@example.com' WHERE account_id=%s",
                (account_id(conn),))
        async with AdmissionManager().operation('foreground',pool.principal):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def work():
                    async with external_account_work(pool.control_pool,pool.principal.account_id):
                        try:
                            async with pool.connection() as conn:
                                await conn.execute("SELECT set_config('client_encoding',%s,true)",(encoding,))
                                expected = await odometer_reminder_vehicles(conn,datetime.fromisoformat('2026-10-01T09:00:00+00:00'))
                                assert await select_email_turn(conn,operation=operation)
                                job = await capture_quarterly_job(conn,operation,CONFIG,now=NOW)
                                assert await quarterly_preferences_current(conn,job)
                                result = await prepare_quarterly_message(conn,job)
                                with operation.budget.open(result.artifacts['mime'],'rb') as source:
                                    message = BytesParser(policy=policy.default).parse(source)
                                expected_body = _render('quarterly_odometer.txt',vehicles=', '.join(expected),noun='vehicles',
                                    settings_url=CONFIG.app_url+'/settings')
                                assert message.get_content() == decoded_oracle(expected_body)
                                if encoding == 'SJIS':
                                    assert '\\' in expected[1] and '¥' not in ''.join(expected)
                                await operation.finish_preparation()
                        finally:
                            await operation.close()
                await operation.perform(work)
    asyncio.run(with_pool(scenario))


def test_logged_active_name_preserves_baseline_client_conversion_failure(tmp_path):
    async def scenario(raw,pool):
        await seed(pool)
        async with pool.connection() as conn:
            await conn.execute('UPDATE vehicles SET active=false WHERE account_id=%s',(account_id(conn),))
            identity = (await (await conn.execute("INSERT INTO vehicles(account_id,name,active) VALUES(%s,'車',true) RETURNING id",
                (account_id(conn),))).fetchone())[0]
            await conn.execute('INSERT INTO odometer_readings(account_id,vehicle_id,recorded_at,odometer_m) VALUES(%s,%s,%s,1000)',
                (account_id(conn),identity,NOW))
            await conn.execute("UPDATE account_settings SET display_tz='UTC',email_to='a@example.com' WHERE account_id=%s",
                (account_id(conn),))
        async with pool.connection() as conn:
            await conn.execute("SELECT set_config('client_encoding','LATIN1',true)")
            with pytest.raises(DataError) as baseline:
                async with conn.transaction():
                    await odometer_reminder_vehicles(conn,NOW.replace(hour=9))
        async with AdmissionManager().operation('foreground',pool.principal):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def work():
                    async with external_account_work(pool.control_pool,pool.principal.account_id):
                        try:
                            async with pool.connection() as conn:
                                await conn.execute("SELECT set_config('client_encoding','LATIN1',true)")
                                job = await capture_quarterly_job(conn,operation,CONFIG,now=NOW)
                                with pytest.raises(type(baseline.value)):
                                    async with conn.transaction():
                                        await prepare_quarterly_message(conn,job)
                                assert not (operation.directory/'mail-mime').exists()
                        finally:
                            await operation.close()
                await operation.perform(work)
    asyncio.run(with_pool(scenario))
