"""Prepared report files match the legacy pure and database report oracles."""
import asyncio
import os
from datetime import date, datetime
from types import SimpleNamespace

from psycopg.pq import TransactionStatus
import pytest

from app.account_context import account_id
from app.account_work import report_account_work
from app.capacity import AdmissionManager
from app.db import make_pool
from app.expenses import build_expense_report
from app.export import to_report_xlsx, to_range_report_xlsx
from app.preparation import PreparationOperation
from app.report import build_annual_report, build_range_report
from app.report_preparation import prepare_report, Projection, _TRIP_SCALARS, _PURPOSE_NONBLANK, _purpose_projection
from app.ui import reports
from conftest import reset_account_db
from test_report_projection_db import _seed, _full_rows, TZ
from test_streamed_reports import normalize

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='requires disposable PostGIS')]


@pytest.mark.parametrize('client_encoding',['UTF8','LATIN1'])
@pytest.mark.parametrize('fallback',[False,True])
def test_purpose_projection_preserves_python_strip_with_narrow_client(client_encoding,fallback,monkeypatch):
    async def run():
        raw=make_pool(os.environ['TEST_DATABASE_URL']);await raw.open(wait=True)
        try:
            pool=await reset_account_db(raw)
            async with pool.connection() as conn:
                await _seed(conn)
                server=conn.info.parameter_status('server_encoding')
                purposes=[None,'',' \t\n\r\x1c\x1f\x85\xa0','\xa0Visit\x85','Plain visit']
                if server=='UTF8':
                    purposes += ['\u3000\u2003\u202f','\u3000Visit\u2003','\u3000'*40000]
                ids=[row[0] for row in await (await conn.execute('SELECT id FROM trips WHERE account_id=%s ORDER BY id',(account_id(conn),))).fetchall()]
                expected={identity:bool((purposes[i%len(purposes)] or '').strip()) for i,identity in enumerate(ids)}
                for i,identity in enumerate(ids):
                    await conn.execute('UPDATE trips SET purpose=%s WHERE account_id=%s AND id=%s',(purposes[i%len(purposes)],account_id(conn),identity))
            async with pool.connection() as conn:
                await conn.execute("SELECT set_config('client_encoding',%s,true)",(client_encoding,))
                assert conn.info.parameter_status('client_encoding')==client_encoding
                class Operation:
                    def check(self): pass
                    def remaining_ms(self): return 15000
                class Session:
                    def __init__(self): self.commands=[]
                    async def send_command(self,value): self.commands.append(value)
                    async def send_text(self,value): assert len(value)<=65520
                projection=Projection(Operation(),Session())
                bounds={'start':datetime(2026,1,1,tzinfo=TZ),'end':datetime(2027,1,1,tzinfo=TZ)}
                if fallback:
                    from psycopg.errors import NotSupportedError
                    def unmapped(name): raise NotSupportedError('test unmapped server codec')
                    monkeypatch.setattr('app.report_preparation.pg2pyenc',unmapped)
                purpose_sql,purpose_param=_purpose_projection(conn)
                scalars=_TRIP_SCALARS.replace(_PURPOSE_NONBLANK,purpose_sql)
                query=f'SELECT {scalars} FROM trips WHERE account_id=%s ORDER BY id'
                actual={row['id']:row['purpose_nonblank'] async for row in projection.rows(conn,query,(purpose_param,account_id(conn)))}
                assert actual==expected
                await projection.groups(conn,account_id(conn),bounds,2026,False)
                grouped={command['row']['id']:command['row']['purpose_nonblank'] for command in projection.session.commands if command['type']=='vehicle_trip'}
                assert grouped==expected
        finally: await raw.close()
    asyncio.run(run())


def test_projection_crosses_unicode_source_frames_and_preserves_xlsx_prefix(tmp_path):
    kind = 'annual_xlsx'
    async def run():
        raw = make_pool(os.environ['TEST_DATABASE_URL']); await raw.open(wait=True)
        try:
            pool = await reset_account_db(raw)
            async with pool.connection() as conn:
                await _seed(conn)
                large_name = ('車ß<&__ODOGRAPH_TEXT_0000000000000000_0000000000000003__' * 1600)
                await conn.execute('UPDATE vehicles SET name=%s WHERE account_id=%s',(large_name,account_id(conn)))
                await conn.execute('UPDATE trips SET notes=%s,purpose=%s WHERE account_id=%s',('車' * 32767 + '\x01beyond old XLSX prefix','\u2003' * 40000,account_id(conn)))
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s',(str(TZ),account_id(conn)))
            start,end = date(2026,1,1),date(2026,12,31)
            async with pool.connection() as conn:
                rows = await _full_rows(conn,start,end)
                _,rates = await reports._fetch_range_trips_in(conn,TZ,start,end)
            if kind=='annual_xlsx':
                coverage = await reports._fetch_year_odometer_coverage(pool,TZ,2026,rows)
                expenses,expense_report = await reports._fetch_year_expense_report(pool,TZ,2026,rows,rates)
                expected = to_report_xlsx(build_annual_report(rows,rates,TZ,2026),rows,rates,TZ,coverage,expense_report,expenses)
            else:
                expected = to_range_report_xlsx(build_range_report(rows,rates,TZ,start,end),rows,rates,TZ)
            manager=AdmissionManager()
            async with manager.operation('foreground',pool.principal):
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    async with report_account_work(pool.control_pool,pool.principal) as control:
                        state=SimpleNamespace(_preparation=operation,_report_control_connection=control,principal=pool.principal,account_pool=pool,csp_nonce='test')
                        request=SimpleNamespace(state=state,app=SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(app_version='test'))),session={'csrf':'車<&' * 25000},url=SimpleNamespace(path='/report/2026'))
                        user={'id':pool.principal.account_id,'is_admin':True,'is_enabled':True,'legacy_oidc':False,'has_avatar':False,'avatar_version':0}
                        messages=[]
                        async def send(message):
                            assert control.info.transaction_status == TransactionStatus.IDLE
                            messages.append(message)
                        async def prepare_and_send():
                            response=await prepare_report(request,user,kind,year=2026,start=start,end=end)
                            assert operation.process.returncode == 0 and operation.finished
                            await response({},None,send)
                        await operation.perform(prepare_and_send)
                        actual=b''.join(m.get('body',b'') for m in messages)
                        assert normalize(actual)==normalize(expected)
                        assert operation.closed and not operation.directory.exists()
            assert not list((tmp_path/'spool').glob('op-*'))
        finally: await raw.close()
    asyncio.run(run())
