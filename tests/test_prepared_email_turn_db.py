"""Serving admission and legacy sweeps use real quarterly preparation helpers."""
import asyncio
import fcntl
import json
import os
from datetime import timedelta
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from psycopg.errors import LockNotAvailable

from app.account_context import account_id
from app.account_workers import AccountWorker
from app.capacity import AdmissionManager, ManagedPool, current_owner, owned_thread
from app.db import EMAIL_DIGEST_ADVISORY_LOCK_KEY, make_pool
from app.email_digest import EmailDigestWorker, _render, run_prepared_email_turn
from app.mailer import Mailer
from app.nudge import latest_window_end
from app import notification_preparation
from app.worker import RUN_SKIPPED, TurnOutcome
from auth_db_fixtures import auth_config
from conftest import reset_account_db, restricted_role_pools, seed_tracking_device
from test_email_digest_db import _insert_trip
from test_notification_preparation_db import NOW

pytestmark = [pytest.mark.db, pytest.mark.capacity_contract,
    pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='requires disposable PostGIS')]


async def setup(tmp_path, monkeypatch, *, weekly=False, due=True, bad_headers=False, now=NOW):
    raw = make_pool(os.environ['TEST_DATABASE_URL'])
    await raw.open(wait=True)
    try:
        pool = await reset_account_db(raw)
        async with pool.connection() as conn:
            await conn.execute('UPDATE vehicles SET active=false WHERE account_id=%s',(account_id(conn),))
            await conn.execute('INSERT INTO vehicles(account_id,name,active) VALUES(%s,%s,%s)',
                (account_id(conn),'A😀, duplicate',due))
            await conn.execute("UPDATE account_settings SET display_tz='America/Los_Angeles',email_to=%s,"
                'email_weekly_nudge=%s,email_monthly_summary=false,email_filing_reminder=false,'
                'email_odometer_reminder=true,odometer_reminder_hour=9 WHERE account_id=%s',
                ('bad\nheader' if bad_headers else 'you@example.com',weekly,account_id(conn)))
            if weekly:
                await seed_tracking_device(conn,'phone',device_id=1)
                await _insert_trip(conn,latest_window_end(NOW.astimezone(ZoneInfo('America/Los_Angeles')),18)-timedelta(days=1))
        config = auth_config(raw.conninfo, preparation_spool_dir=str(tmp_path/'spool'),
            smtp_host='smtp.invalid',email_from='bad\nheader' if bad_headers else 'from@example.com',
            smtp_security='none',app_url='https://example.invalid/')
        capacity = AdmissionManager(config)
        roles = await restricted_role_pools(raw)
        pools = SimpleNamespace(runtime=ManagedPool(roles.runtime,capacity,'runtime'),
            control=ManagedPool(roles.control,capacity,'control'))
        factory_calls, messages, prepared_messages = [], [], []
        def factory(account_pool, cfg):
            factory_calls.append(cfg)
            if not cfg.email_enabled:
                return None
            return EmailDigestWorker(account_pool,Mailer(cfg.smtp_host,cfg.smtp_port,cfg.smtp_username,
                cfg.smtp_password,cfg.smtp_security,cfg.smtp_tls_insecure,cfg.email_from,cfg.email_to),
                cfg.app_url,cfg.display_tz,cfg.nudge_weekly_hour,cfg.odometer_reminder_hour,
                cfg.email_digest_hour,cfg.email_filing_reminder_mmdd,cfg.email_weekly_nudge,
                cfg.email_monthly_summary,cfg.email_filing_reminder,cfg.email_odometer_reminder)
        generic_capture = notification_preparation.capture_email_job
        async def frozen_generic(conn,op,cfg,kind,**kwargs): return await generic_capture(conn,op,cfg,kind,now=now)
        monkeypatch.setattr(notification_preparation,"capture_email_job",frozen_generic)
        capture = notification_preparation.capture_quarterly_job
        sweep_capture = notification_preparation.capture_email_settings
        async def frozen_capture(conn,op,cfg,**kwargs): return await capture(conn,op,cfg,now=now)
        async def frozen_sweep(conn,op,cfg,**kwargs): return await sweep_capture(conn,op,cfg,now=now)
        monkeypatch.setattr(notification_preparation,'capture_quarterly_job',frozen_capture)
        monkeypatch.setattr(notification_preparation,'capture_email_settings',frozen_sweep)
        async def send(self,message): messages.append(message)
        async def send_prepared(self,prepared,*,before_transport=None):
            stack, descriptors, guard = await owned_thread(prepared.open_descriptors)
            try:
                assert current_owner().lane == 'background' and capacity.snapshot()['leases'] == 1
                assert all(fcntl.fcntl(fd,fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY for fd in descriptors)
                if before_transport:
                    await before_transport()
                def read():
                    envelope = json.loads(os.pread(descriptors[1],os.fstat(descriptors[1]).st_size,0))
                    mime = os.pread(descriptors[2],os.fstat(descriptors[2]).st_size,0)
                    return envelope, BytesParser(policy=policy.default).parsebytes(mime)
                prepared_messages.append(await owned_thread(read))
                async with raw.connection() as conn:
                    assert not (await (await conn.execute('SELECT pg_try_advisory_xact_lock(%s)',
                        (EMAIL_DIGEST_ADVISORY_LOCK_KEY,))).fetchone())[0]
                    with pytest.raises(LockNotAvailable):
                        async with conn.transaction():
                            await conn.execute('SELECT 1 FROM account_settings WHERE account_id=%s FOR UPDATE NOWAIT',
                                (pool.principal.account_id,))
            finally:
                await owned_thread(stack.close)
        monkeypatch.setattr(Mailer,'send',send)
        monkeypatch.setattr(Mailer,'send_prepared',send_prepared)
        worker = AccountWorker(pools,config,factory,label='email-digest',debounce_s=1,sweep_s=3600,
            capacity=capacity,admitted_turn=run_prepared_email_turn)
        return SimpleNamespace(raw=raw,pool=pool,capacity=capacity,worker=worker,config=config,
            factory_calls=factory_calls,messages=messages,prepared=prepared_messages)
    except BaseException:
        await raw.close()
        raise


def assert_clean(state):
    snapshot = state.capacity.snapshot()
    assert snapshot['leases'] == 0 and all(value['active'] == 0 for key,value in snapshot.items() if key != 'leases')
    assert not list(Path(state.config.preparation_spool_dir).glob('op-*'))


async def deliveries(state):
    async with state.raw.connection() as conn:
        return await (await conn.execute('SELECT kind,sent,period_end FROM email_deliveries ORDER BY kind')).fetchall()


def test_serving_turn_prepares_sends_commits_and_deduplicates(tmp_path,monkeypatch):
    async def run():
        state = await setup(tmp_path,monkeypatch)
        try:
            result = await state.worker.run_turn()
            assert result.batch.attempted == result.batch.completed == 1
            assert not result.ready and result.cursor is None
            assert not state.factory_calls and not state.messages and len(state.prepared) == 1
            envelope,message = state.prepared[0]
            assert envelope['recipients'] == ['you@example.com']
            assert message.get_content().replace('\r\n','\n') == _render('quarterly_odometer.txt',
                vehicles='A😀, duplicate',noun='vehicle',settings_url='https://example.invalid//settings')
            ledger = await deliveries(state)
            assert [(kind,sent) for kind,sent,boundary in ledger] == [('quarterly_odometer',True)]
            state.worker.wake_cycle()
            await state.worker.run_turn()
            assert len(state.prepared) == 1 and await deliveries(state) == ledger
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


def test_zero_due_bad_headers_records_false_without_factory_or_transport(tmp_path,monkeypatch):
    async def run():
        state = await setup(tmp_path,monkeypatch,due=False,bad_headers=True)
        try:
            await state.worker.run_turn()
            assert not state.prepared and not state.messages and not state.factory_calls
            assert [(kind,sent) for kind,sent,boundary in await deliveries(state)] == [('quarterly_odometer',False)]
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('field',['smtp_host','email_from'])
@pytest.mark.parametrize('legacy',[False,True])
def test_unconfigured_transport_preserves_empty_turn_and_legacy_skip(tmp_path,monkeypatch,field,legacy):
    async def run():
        state = await setup(tmp_path,monkeypatch)
        state.worker.config = replace(state.worker.config,**{field:''})
        try:
            if legacy:
                assert await state.worker.run_once() is RUN_SKIPPED
            else:
                result = await state.worker.run_turn()
                assert result.batch.attempted == 0 and not result.ready and result.cursor is None
            assert not state.prepared and not state.messages and not await deliveries(state)
            assert state.worker.last_outcome.attempted == 0
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage',['configuration','schedule'])
def test_configuration_failure_retains_cursor_and_schedule_failure_advances(tmp_path,monkeypatch,stage):
    async def run():
        state = await setup(tmp_path,monkeypatch,now=NOW.replace(year=1,month=1,day=1,hour=8))
        try:
            async with state.pool.connection() as conn:
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s',
                    ('invalid-zone' if stage=='configuration' else 'UTC',account_id(conn)))
            state.worker._continuations[state.pool.principal.account_id] = TurnOutcome(ready=True,cursor=3)
            result = await state.worker.run_turn()
            assert state.worker.status.last_failure_type == ('ZoneInfoNotFoundError' if stage=='configuration' else 'ValueError')
            assert result.cursor == (3 if stage=='configuration' else None)
            assert result.batch.attempted == (0 if stage=='configuration' else 1)
            assert not await deliveries(state) and not state.prepared
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',["email_to='changed@example.com'","display_tz='UTC'",'odometer_reminder_hour=10'])
def test_legacy_sweep_rejects_quarter_against_initial_settings(tmp_path,monkeypatch,change):
    async def run():
        state = await setup(tmp_path,monkeypatch,weekly=True)
        from app.email_digest import _prepared_email_delivery
        initial_run = _prepared_email_delivery
        observed = []
        async def delivery(pool,principal,job,operation):
            await initial_run(pool,principal,job,operation)
            if job.kind == 'weekly_nudge':
                observed.append(job.period_end)
                async with state.raw.connection() as conn:
                    await conn.execute(f'UPDATE account_settings SET {change},email_monthly_summary=true WHERE account_id=%s',
                        (state.pool.principal.account_id,))
        monkeypatch.setattr('app.email_digest._prepared_email_delivery',delivery)
        try:
            await state.worker.run_once()
            assert not state.messages and len(state.prepared) == 1 and not state.factory_calls
            assert observed == [latest_window_end(NOW.astimezone(ZoneInfo('America/Los_Angeles')),18)]
            assert state.prepared[0][0]['recipients'] == ['you@example.com']
            assert [(kind,sent) for kind,sent,boundary in await deliveries(state)] == [('weekly_nudge',True)]
            assert state.worker.last_outcome.attempted == 2
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


def test_legacy_weekly_failure_does_not_stop_quarterly(tmp_path,monkeypatch):
    async def run():
        state = await setup(tmp_path,monkeypatch,weekly=True)
        original_send = Mailer.send_prepared
        calls = 0
        async def fail(self,prepared,*,before_transport=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('local fixture refusal')
            await original_send(self,prepared,before_transport=before_transport)
        monkeypatch.setattr(Mailer,'send_prepared',fail)
        try:
            await state.worker.run_once()
            assert len(state.prepared) == 1
            assert [(kind,sent) for kind,sent,boundary in await deliveries(state)] == [('quarterly_odometer',True)]
            assert state.worker.last_outcome.attempted == 2 and state.worker.last_outcome.retriable_failures == 1
            assert state.worker.status.last_failure_type == 'RuntimeError'
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


def test_repeated_cancel_retains_owner_lease_and_files_until_send_cleanup(tmp_path,monkeypatch):
    async def run():
        state = await setup(tmp_path,monkeypatch)
        started,cleanup,release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def waiting(self,prepared,*,before_transport=None):
            stack,descriptors,guard = await owned_thread(prepared.open_descriptors)
            try:
                await before_transport()
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cleanup.set()
                    await release.wait()
                    raise
            finally:
                await owned_thread(stack.close)
        monkeypatch.setattr(Mailer,'send_prepared',waiting)
        task = asyncio.create_task(state.worker.run_turn())
        try:
            await asyncio.wait_for(started.wait(),5)
            task.cancel()
            await asyncio.wait_for(cleanup.wait(),5)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            assert state.capacity.snapshot()['background']['active'] == state.capacity.snapshot()['leases'] == 1
            assert list(Path(state.config.preparation_spool_dir).glob('op-*'))
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not await deliveries(state)
            assert_clean(state)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['weekly_nudge','monthly_summary','filing_reminder'])
@pytest.mark.parametrize('empty',[False,True])
def test_admitted_digest_complete_body_matches_old_source_and_deduplicates(tmp_path,monkeypatch,kind,empty):
    from datetime import datetime, date
    from app.digest_summary import fetch_digest_summary
    from app.email_digest import latest_month_boundary, covered_month, _calendar_month_days, latest_filing_reminder_at, MONTH_ABBR
    from app.formatting import format_miles, format_usd
    async def run():
        now = datetime(2027,1,15,18,tzinfo=ZoneInfo('UTC'))
        state = await setup(tmp_path,monkeypatch,now=now)
        try:
            flag = notification_preparation.FLAGS[notification_preparation.KINDS.index(kind)]
            local = now.astimezone(ZoneInfo('America/Los_Angeles'))
            if kind == 'weekly_nudge':
                period = latest_window_end(local,18); first = period-timedelta(days=7)
            elif kind == 'monthly_summary':
                period = latest_month_boundary(local,9); year,month=covered_month(period)
                first,last = _calendar_month_days(year,month)
            else:
                period = latest_filing_reminder_at(local,'01-15',9); year=period.year-1
                first,last=date(year,1,1),date(year,12,31)
            async with state.pool.connection() as conn:
                await conn.execute('UPDATE account_settings SET '+','.join(f+('=true' if f==flag else '=false') for f in notification_preparation.FLAGS)+
                    f',email_filing_reminder_mmdd=\'01-15\',email_digest_hour=9,nudge_weekly_hour=18 WHERE account_id=%s',
                    (account_id(conn),))
                await seed_tracking_device(conn,'phone',device_id=1)
                if not empty:
                    if kind == 'weekly_nudge':
                        await _insert_trip(conn,first,'unclassified')
                        await _insert_trip(conn,period,'unclassified')  # exclusive end
                    else:
                        tz=ZoneInfo('America/Los_Angeles')
                        for index in range(259):
                            await _insert_trip(conn,datetime.combine(first,datetime.min.time(),tzinfo=tz)+timedelta(minutes=index*20),
                                'business',1e20 if index == 0 else 1609.344)
                        await _insert_trip(conn,datetime.combine(last,datetime.min.time(),tzinfo=tz),'unclassified')
                if kind != 'weekly_nudge':
                    await conn.execute('DELETE FROM mileage_rates WHERE account_id=%s',(account_id(conn),))
                    await conn.execute('INSERT INTO mileage_rates(account_id,year,rate_per_mi,rate_h2_per_mi,h2_start_month) '
                        'VALUES(%s,%s,.56,.625,7)',(account_id(conn),year))
            if kind == 'weekly_nudge':
                expected = _render('weekly_nudge.txt',count=1,noun='trip',review_url=state.config.app_url+'/review')
                subject='Odograph: weekly unclassified-trip digest'
            else:
                async with state.capacity.operation('foreground',state.pool.principal):
                    async with state.pool.connection() as conn:
                        summary=await fetch_digest_summary(conn,ZoneInfo('America/Los_Angeles'),first,last)
                context=dict(business_mi=format_miles(summary.business_m),
                    nondeductible_mi=format_miles(summary.nondeductible_m) if summary.nondeductible_m else '',
                    deduction=format_usd(summary.total_deduction))
                if kind == 'monthly_summary':
                    context.update(month_label=f'{MONTH_ABBR[month]} {year}',unclassified=summary.unclassified_trips,
                        report_url=state.config.app_url+f'/report/range?from={first.isoformat()}&to={last.isoformat()}')
                    subject=f'Odograph: {MONTH_ABBR[month]} summary'
                else:
                    context.update(year=year,report_url=state.config.app_url+f'/report/{year}',
                        export_url=state.config.app_url+f'/report/{year}/export')
                    subject=f'Odograph: {year} filing reminder'
                expected=_render(kind+'.txt',**context)
            result=await state.worker.run_turn()
            assert result.batch.completed == 1 and result.batch.retriable_failures == 0
            assert not state.factory_calls and not state.messages
            should_send=not empty or kind != 'weekly_nudge'
            assert len(state.prepared) == int(should_send)
            if should_send:
                envelope,message=state.prepared[0]
                assert message.get_content().replace('\r\n','\n') == expected
                assert str(message['Subject']) == subject and not message.is_multipart()
            ledger=await deliveries(state)
            assert ledger == [(kind,should_send,period)]
            state.worker.wake_cycle(); await state.worker.run_turn()
            assert len(state.prepared) == int(should_send) and await deliveries(state) == ledger
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('empty_to',[False,True])
@pytest.mark.parametrize('invalid_tz',[False,True])
def test_no_selected_kind_still_validates_initial_settings(tmp_path,monkeypatch,empty_to,invalid_tz):
    async def run():
        state=await setup(tmp_path,monkeypatch)
        try:
            async with state.pool.connection() as conn:
                await conn.execute('UPDATE account_settings SET '+','.join(f+'=false' for f in notification_preparation.FLAGS)+
                    ',email_to=%s,display_tz=%s WHERE account_id=%s',
                    ('' if empty_to else 'you@example.com','invalid-zone' if invalid_tz else 'UTC',account_id(conn)))
            state.worker._continuations[state.pool.principal.account_id]=TurnOutcome(ready=True,cursor=3)
            result=await state.worker.run_turn()
            assert result.batch.attempted == 0 and not state.prepared and not state.messages
            assert not state.factory_calls and not await deliveries(state)
            if invalid_tz:
                assert state.worker.status.last_failure_type == 'ZoneInfoNotFoundError' and result.cursor == 3
            else:
                assert state.worker.status.last_failure_type is None and result.cursor is None
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


def test_real_digest_history_deadline_rolls_back_and_sweep_continues(tmp_path,monkeypatch):
    import psycopg
    original=psycopg.AsyncServerCursor.fetchmany
    fail=True
    async def fetch(cursor,size=0):
        nonlocal fail
        if cursor.name.startswith('notification_') and fail:
            fail=False
            await cursor.connection.execute('SELECT pg_sleep(2)')
        return await original(cursor,size)
    monkeypatch.setattr(psycopg.AsyncServerCursor,'fetchmany',fetch)
    monkeypatch.setattr('app.digest_summary.PREPARATION_SECONDS',.05)
    async def run():
        state=await setup(tmp_path,monkeypatch)
        try:
            async with state.pool.connection() as conn:
                await conn.execute('UPDATE account_settings SET email_monthly_summary=true WHERE account_id=%s',(account_id(conn),))
            await state.worker.run_once()
            assert state.worker.status.last_failure_type in ('DigestPreparationTimeout','QueryCanceled')
            assert state.worker.last_outcome.attempted == 2 and state.worker.last_outcome.retriable_failures == 1
            assert len(state.prepared) == 1 and str(state.prepared[0][1]['Subject']) == 'Odograph: log an odometer reading'
            assert [(kind,sent) for kind,sent,end in await deliveries(state)] == [('quarterly_odometer',True)]
            assert_clean(state)
            monkeypatch.setattr('app.digest_summary.PREPARATION_SECONDS',15.0)
            await state.worker.run_once()
            assert len(state.prepared) == 2 and [(kind,sent) for kind,sent,end in await deliveries(state)] == [
                ('monthly_summary',True),('quarterly_odometer',True)]
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('value',['NaN','Infinity','1e-16383','maximum'])
def test_prepared_numeric_rates_preserve_decimal_extremes(tmp_path,monkeypatch,value):
    async def run():
        state=await setup(tmp_path,monkeypatch)
        try:
            async with state.pool.connection() as conn:
                await conn.execute('UPDATE account_settings SET email_monthly_summary=true,email_odometer_reminder=false '
                    'WHERE account_id=%s',(account_id(conn),))
                await seed_tracking_device(conn,'phone',device_id=1)
                from datetime import datetime
                await _insert_trip(conn,datetime(2026,9,15,18,tzinfo=ZoneInfo('America/Los_Angeles')),'business',1.0)
                await conn.execute('DELETE FROM mileage_rates WHERE account_id=%s',(account_id(conn),))
                if value=='maximum':
                    await conn.execute("INSERT INTO mileage_rates(account_id,year,rate_per_mi) "
                        "VALUES(%s,2026,('9'||repeat('0',131071)||'.'||repeat('1',16383))::numeric)",(account_id(conn),))
                else:
                    await conn.execute('INSERT INTO mileage_rates(account_id,year,rate_per_mi) VALUES(%s,2026,%s::numeric)',
                        (account_id(conn),value))
            updated = False
            if value == 'maximum':
                from app.preparation import PreparationSession
                original_text = PreparationSession.send_text
                async def fragment(session,raw):
                    nonlocal updated
                    await original_text(session,raw)
                    if len(raw) == 65520 and not updated:
                        updated = True
                        async with state.raw.connection() as conn:
                            await conn.execute('UPDATE mileage_rates SET rate_per_mi=.25 WHERE account_id=%s',
                                (state.pool.principal.account_id,))
                monkeypatch.setattr(PreparationSession,'send_text',fragment)
            result=await state.worker.run_turn()
            assert result.batch.completed==1 and result.batch.retriable_failures==0
            assert updated == (value == 'maximum')
            assert len(state.prepared)==1
            deduction='nan' if value=='NaN' else 'inf' if value in ('Infinity','maximum') else '0.00'
            assert 'Deduction: $'+deduction in state.prepared[0][1].get_content()
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


def test_repeated_cancel_during_real_digest_query_and_rollback_retains_lifetime(tmp_path,monkeypatch):
    import psycopg
    original_fetch=psycopg.AsyncServerCursor.fetchmany
    original_cancel=psycopg.AsyncConnection._try_cancel
    original_exit=psycopg.AsyncTransaction.__aexit__
    events={}; target=None
    async def fetch(cursor,size=0):
        nonlocal target
        if cursor.name.startswith('notification_') and target is None:
            target=cursor.connection.info.backend_pid
            events['query'].set()
            await cursor.connection.execute('SELECT pg_sleep(10)')
        return await original_fetch(cursor,size)
    async def cancel(conn,**kwargs):
        if conn.info.backend_pid == target:
            events['cancel'].set()
            await events['release_cancel'].wait()
        await original_cancel(conn,**kwargs)
    async def exit_transaction(transaction,exc_type,exc_value,traceback):
        if transaction.pgconn.backend_pid == target and exc_type is asyncio.CancelledError:
            events['rollback'].set()
            await events['release_rollback'].wait()
        return await original_exit(transaction,exc_type,exc_value,traceback)
    monkeypatch.setattr(psycopg.AsyncServerCursor,'fetchmany',fetch)
    monkeypatch.setattr(psycopg.AsyncConnection,'_try_cancel',cancel)
    monkeypatch.setattr(psycopg.AsyncTransaction,'__aexit__',exit_transaction)
    async def run():
        events.update({name:asyncio.Event() for name in ('query','cancel','rollback','release_cancel','release_rollback')})
        state=await setup(tmp_path,monkeypatch)
        task=None
        try:
            async with state.pool.connection() as conn:
                await conn.execute('UPDATE account_settings SET email_monthly_summary=true,email_odometer_reminder=false '
                    'WHERE account_id=%s',(account_id(conn),))
            task=asyncio.create_task(state.worker.run_turn())
            await asyncio.wait_for(events['query'].wait(),5)
            await asyncio.sleep(.02)
            task.cancel(); await asyncio.wait_for(events['cancel'].wait(),5)
            task.cancel(); await asyncio.sleep(.01)
            assert not task.done() and not state.prepared
            assert state.capacity.snapshot()['background']['active'] == state.capacity.snapshot()['leases'] == 1
            assert list(Path(state.config.preparation_spool_dir).glob('op-*'))
            async with state.raw.connection() as conn:
                assert (await (await conn.execute('SELECT state,xact_start IS NOT NULL FROM pg_stat_activity WHERE pid=%s',
                    (target,))).fetchone()) == ('active',True)
            events['release_cancel'].set()
            await asyncio.wait_for(events['rollback'].wait(),5)
            task.cancel(); await asyncio.sleep(.01)
            assert not task.done() and state.capacity.snapshot()['leases'] == 1
            async with state.raw.connection() as conn:
                assert (await (await conn.execute('SELECT xact_start IS NOT NULL FROM pg_stat_activity WHERE pid=%s',
                    (target,))).fetchone())[0]
            events['release_rollback'].set()
            with pytest.raises(asyncio.CancelledError): await task
            assert not await deliveries(state) and not state.prepared
            assert_clean(state)
        finally:
            events['release_cancel'].set(); events['release_rollback'].set()
            if task is not None:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('state_kind',['delivered','stale','needed'])
def test_filing_year_one_history_dates_are_not_evaluated_before_delivery_checks(tmp_path,monkeypatch,state_kind):
    from datetime import datetime
    from app.email_digest import _prepared_email_delivery
    async def run():
        state=await setup(tmp_path,monkeypatch,now=datetime(1,1,15,10,tzinfo=ZoneInfo('UTC')))
        try:
            async with state.pool.connection() as conn:
                await conn.execute("UPDATE account_settings SET display_tz='UTC',email_odometer_reminder=false,"
                    "email_filing_reminder=true,email_filing_reminder_mmdd='01-15',email_digest_hour=9 WHERE account_id=%s",
                    (account_id(conn),))
                if state_kind == 'delivered':
                    await conn.execute('INSERT INTO email_deliveries(account_id,kind,period_end,sent) VALUES(%s,%s,%s,false)',
                        (account_id(conn),'filing_reminder',datetime(1,1,15,9,tzinfo=ZoneInfo('UTC'))))
            if state_kind == 'stale':
                async def delivery(pool,principal,job,operation):
                    async with state.raw.connection() as conn:
                        await conn.execute("UPDATE account_settings SET email_to='changed@example.com' WHERE account_id=%s",
                            (principal.account_id,))
                    await _prepared_email_delivery(pool,principal,job,operation)
                monkeypatch.setattr('app.email_digest._prepared_email_delivery',delivery)
            result=await state.worker.run_turn()
            assert result.batch.attempted == 1 and result.batch.retriable_failures == int(state_kind == 'needed')
            assert state.worker.status.last_failure_type == ('ValueError' if state_kind == 'needed' else None)
            assert not state.prepared and not state.messages
            assert len(await deliveries(state)) == int(state_kind == 'delivered')
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())


@pytest.mark.parametrize('legacy',[False,True])
@pytest.mark.parametrize('case',['no_selected','zero_quarter','zero_weekly','newline_header','high_header','high_url','low_header','config'])
def test_operator_surrogates_defer_until_exact_legacy_use_phase(tmp_path,monkeypatch,legacy,case):
    from email.message import EmailMessage
    from app.mime_preparation import envelope
    async def run():
        skipped=case in ('no_selected','zero_quarter','zero_weekly')
        state=await setup(tmp_path,monkeypatch,due=not skipped)
        try:
            fields={}
            if skipped:
                fields={name:'bad\n\ud800\udc80' for name in ('app_url','email_from','smtp_host','smtp_username','smtp_password','smtp_security')}
            elif case=='newline_header': fields=dict(email_from='bad\n\ud800',app_url='\ud800')
            elif case=='high_header': fields=dict(email_from='\ud800@example.com')
            elif case=='high_url': fields=dict(app_url='https://example.invalid/\ud800')
            elif case=='low_header': fields=dict(email_from='\udc80@example.com')
            elif case=='config': fields=dict(smtp_host='smtp.\ud800.invalid',smtp_password='\ud800',smtp_username='')
            state.worker.config=replace(state.worker.config,**fields)
            async with state.pool.connection() as conn:
                if case in ('no_selected','zero_weekly'):
                    await conn.execute('UPDATE account_settings SET email_odometer_reminder=false,email_weekly_nudge=%s WHERE account_id=%s',
                        (case=='zero_weekly',account_id(conn)))
            observed=[]
            original_send=Mailer.send_prepared
            async def send(self,prepared,*,before_transport=None):
                stack,descriptors,guard=await owned_thread(prepared.open_descriptors)
                try:
                    observed.append(await owned_thread(lambda:json.loads(os.pread(descriptors[0],os.fstat(descriptors[0]).st_size,0))))
                finally:
                    await owned_thread(stack.close)
                await original_send(self,prepared,before_transport=before_transport)
            monkeypatch.setattr(Mailer,'send_prepared',send)
            if legacy:
                await state.worker.run_once()
                outcome=state.worker.last_outcome
            else:
                outcome=(await state.worker.run_turn()).batch
            failures=case in ('newline_header','high_header','high_url')
            assert outcome.retriable_failures==int(failures)
            if failures:
                assert state.worker.status.last_failure_type==('ValueError' if case=='newline_header' else 'UnicodeEncodeError')
                assert not state.prepared and not await deliveries(state) and not observed
            elif case=='no_selected':
                assert outcome.attempted==0 and not state.prepared and not await deliveries(state)
            elif skipped:
                assert outcome.completed==1 and not state.prepared and not observed
                kind='weekly_nudge' if case=='zero_weekly' else 'quarterly_odometer'
                assert [(k,sent) for k,sent,end in await deliveries(state)]==[(kind,False)]
            else:
                assert outcome.completed==1 and len(state.prepared)==len(observed)==1
                assert observed[0]['host']==state.worker.config.smtp_host
                assert observed[0]['password']==state.worker.config.smtp_password
                if case=='low_header':
                    body=_render('quarterly_odometer.txt',vehicles='A😀, duplicate',noun='vehicle',
                        settings_url=state.worker.config.app_url+'/settings')
                    oracle=EmailMessage(); oracle['From']=state.worker.config.email_from
                    oracle['To']='you@example.com'; oracle['Subject']='Odograph: log an odometer reading'; oracle.set_content(body)
                    parsed=BytesParser(policy=policy.default).parsebytes(oracle.as_bytes())
                    sender,recipients,international=envelope(parsed)
                    assert state.prepared[0][0]==dict(sender=sender,recipients=recipients,international=international)
            assert not state.factory_calls and not state.messages
            assert_clean(state)
        finally:
            await state.raw.close()
    asyncio.run(run())
