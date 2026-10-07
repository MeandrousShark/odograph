"""Real helper wall-clock samples precede slow operator-literal projection."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import preparation
from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.notification_preparation import Projection, capture_email_job, capture_email_settings, replay_email_job
from app.ntfy_preparation import capture_ntfy_job
from app.preparation import PreparationOperation

pytestmark = pytest.mark.ops

CASES = [
    ('weekly_nudge','2026-10-04T17:59:59+00:00','2026-10-04T18:00:01+00:00','2026-09-27T18:00:00+00:00'),
    ('monthly_summary','2026-10-01T08:59:59+00:00','2026-10-01T09:00:01+00:00','2026-09-01T09:00:00+00:00'),
    ('filing_reminder','2027-01-15T08:59:59+00:00','2027-01-15T09:00:01+00:00','2026-01-15T09:00:00+00:00'),
    ('quarterly_odometer','2026-10-01T08:59:59+00:00','2026-10-01T09:00:01+00:00','2026-07-01T09:00:00+00:00'),
]


def controlled_helper_clock(tmp_path,monkeypatch,before,after):
    clock=tmp_path/'wall-clock'; samples=tmp_path/'samples'
    clock.write_text(before)
    # Keep production framing, guards, limits, renderers and reaping. Only the
    # two renderer datetime.now calls read this private fixture clock.
    source_root=preparation._HELPER.parent.parent
    bootstrap=tmp_path/'controlled-helper.py'
    bootstrap.write_text(f'''import sys,resource
if sys.platform == 'linux':
    _,hard=resource.getrlimit(resource.RLIMIT_AS)
    ceiling=256*1024*1024 if hard==resource.RLIM_INFINITY else min(256*1024*1024,hard)
    resource.setrlimit(resource.RLIMIT_AS,(ceiling,ceiling))
sys.path.insert(0,{str(source_root)!r})
from datetime import datetime
from pathlib import Path
import app.notification_renderer as email
import app.ntfy_renderer as ntfy
import app.preparation_helper as helper
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):
        value=datetime.fromisoformat(Path({str(clock)!r}).read_text())
        with Path({str(samples)!r}).open('a') as sink:
            sink.write(value.isoformat()+'\\n')
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
email.datetime=ntfy.datetime=Clock
sys.exit(helper.main())
''')
    monkeypatch.setattr(preparation,'_HELPER',bootstrap)
    original=Projection.literal
    async def delayed_literal(self,key,value):
        # A complete held operator string is genuinely framed after crossing
        # the boundary; no now= hook is passed to any capture function.
        clock.write_text(after)
        await original(self,key,value)
    monkeypatch.setattr(Projection,'literal',delayed_literal)
    fields={'display_tz':'UTC','email_to':'to@example.com','ntfy_topic':'topic','email_filing_reminder_mmdd':'01-15'}
    row={'tz_size':3,'to_size':14,'topic_size':5,'ntfy_size':5,'filing_size':5,
        'nudge_weekly_hour':18,'odometer_reminder_hour':9,'email_digest_hour':9,'odometer_reminder_requested':True,
        'email_weekly_nudge':True,'email_monthly_summary':True,'email_filing_reminder':True,'email_odometer_reminder':True}
    async def scalar(self,conn,query,params=()):
        self.operation.check()
        if 'FOR SHARE' in query:
            return row
        field=next(key for key in fields if f'convert_to({key},' in query)
        position,size,_=params
        return {'value':fields[field].encode('utf8')[position-1:position-1+size]}
    monkeypatch.setattr(Projection,'scalar',scalar)
    monkeypatch.setattr('app.notification_preparation.account_id',lambda conn:41)
    monkeypatch.setattr('app.ntfy_preparation.account_id',lambda conn:41)
    config=SimpleNamespace(app_url='https://example.invalid/'+'界😀'*50000,
        email_from='from@example.com',smtp_host='smtp.invalid',smtp_username='',smtp_password='secret'*20000,
        smtp_security='none',smtp_port=25,smtp_tls_insecure=False,ntfy_url='https://example.invalid',
        ntfy_token='',ntfy_username='',ntfy_password='secret'*20000)
    return samples,SimpleNamespace(info=SimpleNamespace(encoding='utf8'),settings=row,fields=fields),config


@pytest.mark.parametrize('kind,before,after,expected',CASES)
def test_actual_email_helper_samples_before_operator_projection(tmp_path,monkeypatch,kind,before,after,expected):
    samples,conn,config=controlled_helper_clock(tmp_path,monkeypatch,before,after)
    async def run():
        async with AdmissionManager().operation('background',AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                job=await capture_email_job(conn,operation,config,kind)
                assert job.period_end==datetime.fromisoformat(expected)
                await job.session.request({'type':'capture'})
                await operation.finish_preparation()
                assert operation.process.returncode==0
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(run())
    assert samples.read_text().splitlines()==[before]


@pytest.mark.parametrize('kind,before,after,expected',[CASES[0],CASES[3]])
def test_actual_legacy_helper_retains_one_initial_clock_through_replay(tmp_path,monkeypatch,kind,before,after,expected):
    samples,conn,config=controlled_helper_clock(tmp_path,monkeypatch,before,after)
    async def run():
        async with AdmissionManager().operation('background',AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as capture:
                snapshot=await capture_email_settings(conn,capture,config)
                assert snapshot.now==datetime.fromisoformat(before)
                await capture.finish_preparation()
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    job=await replay_email_job(snapshot,operation,kind)
                    assert job.period_end==datetime.fromisoformat(expected)
                    await job.session.request({'type':'capture'})
                    await operation.finish_preparation()
                    assert operation.process.returncode==0
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(run())
    assert samples.read_text().splitlines()==[before]


@pytest.mark.parametrize('kind,case',[('weekly',CASES[0]),('quarterly',CASES[3]),
    ('quarterly',('quarterly_odometer','0001-01-01T10:00:00+00:00','0001-01-02T10:00:00+00:00','0001-01-01T09:00:00+00:00'))])
def test_actual_ntfy_helper_samples_before_operator_projection(tmp_path,monkeypatch,kind,case):
    _,before,after,expected=case
    samples,conn,config=controlled_helper_clock(tmp_path,monkeypatch,before,after)
    async def run():
        async with AdmissionManager().operation('background',AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                job=await capture_ntfy_job(conn,operation,config,kind)
                assert job.end==datetime.fromisoformat(expected)
                if kind=='quarterly':
                    from app.odometer import latest_quarter_start
                    assert job.end==latest_quarter_start(datetime.fromisoformat(before),9)
                    assert job.start==job.end
                await job.session.request({'type':'discard'})
                await operation.finish_preparation()
                assert operation.process.returncode==0
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(run())
    assert samples.read_text().splitlines()==[before]


@pytest.mark.parametrize('kind,disabled',[('weekly','url'),('weekly','topic'),('quarterly','url'),('quarterly','requested')])
def test_disabled_year_one_actual_helper_matches_legacy_factory_skip(tmp_path,monkeypatch,kind,disabled):
    from app.config import Config
    from app.nudge import latest_window_end
    from app.odometer import latest_quarter_start
    before,after='0001-01-01T08:59:59+00:00','0001-01-01T09:00:01+00:00'
    samples,conn,config=controlled_helper_clock(tmp_path,monkeypatch,before,after)
    if disabled=='url': config.ntfy_url=''
    elif disabled=='topic':
        conn.fields['ntfy_topic']=''; conn.settings['topic_size']=0
    else: conn.settings['odometer_reminder_requested']=False
    legacy=SimpleNamespace(ntfy_url=config.ntfy_url,ntfy_topic=conn.fields['ntfy_topic'],
        odometer_reminder_requested=conn.settings['odometer_reminder_requested'])
    legacy.nudge_enabled=Config.nudge_enabled.fget(legacy)
    assert not (legacy.nudge_enabled if kind=='weekly' else Config.odometer_reminder_enabled.fget(legacy))
    # The real old factory skips before these otherwise invalid date calculations.
    with pytest.raises((ValueError,OverflowError)):
        (latest_window_end if kind=='weekly' else latest_quarter_start)(datetime.fromisoformat(before),18 if kind=='weekly' else 9)
    async def run():
        async with AdmissionManager().operation('background',AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                job=await capture_ntfy_job(conn,operation,config,kind)
                assert not job.enabled and job.end==job.start==datetime.fromisoformat(before)
                await job.session.request({'type':'discard'})
                await operation.finish_preparation()
                assert operation.process.returncode==0
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(run())
    assert samples.read_text().splitlines()==[before]


def test_disabled_actual_helper_still_validates_timezone_before_skipping(tmp_path,monkeypatch):
    from zoneinfo import ZoneInfoNotFoundError
    samples,conn,config=controlled_helper_clock(tmp_path,monkeypatch,
        '0001-01-01T08:59:59+00:00','0001-01-01T09:00:01+00:00')
    config.ntfy_url=''
    conn.fields['display_tz']='invalid-zone'; conn.settings['tz_size']=len('invalid-zone')
    async def run():
        async with AdmissionManager().operation('background',AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                with pytest.raises(ZoneInfoNotFoundError):
                    await capture_ntfy_job(conn,operation,config,'quarterly')
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(run())
    assert not samples.exists()
