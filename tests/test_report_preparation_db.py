"""Prepared report files match the legacy pure and database report oracles."""
import asyncio
import os
import time
from datetime import date, datetime
from types import SimpleNamespace

from psycopg import DataError
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
from app.report_preparation import prepare_report, Projection
from app.report_renderer import Renderer
from app.preparation_resources import ResourceBudget, SpoolReservation
from app.ui import reports
from conftest import reset_account_db
from test_report_projection_db import _seed, _full_rows, TZ
from test_streamed_reports import normalize

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='requires disposable PostGIS')]


@pytest.mark.parametrize('client_encoding',['UTF8','LATIN1'])
@pytest.mark.parametrize('unicode_suffix',[False,True])
def test_purpose_projection_preserves_python_strip_with_narrow_client(tmp_path,client_encoding,unicode_suffix):
    async def run():
        raw=make_pool(os.environ['TEST_DATABASE_URL']);await raw.open(wait=True)
        reservation=SpoolReservation.acquire(tmp_path/'spool',time.monotonic()+60)
        budget=ResourceBudget(reservation.directory,reservation.directory_fd)
        renderer=object.__new__(Renderer)
        renderer.budget,renderer.texts,renderer.refs=budget,budget.open('report-text'),{}
        try:
            pool=await reset_account_db(raw)
            async with pool.connection() as conn:
                await _seed(conn)
                server=conn.info.parameter_status('server_encoding')
                if unicode_suffix and server!='UTF8':
                    pytest.skip('Unicode conversion corpus requires UTF8 server')
                purposes=[None,'',' \t\n\r\x1c\x1f\x85\xa0','\xa0Visit\x85','Plain visit',
                    '\xa0'*40000,' '*40000+'Visit']
                if unicode_suffix:
                    purposes += [' '*40000+'\u3000']
                    if client_encoding=='UTF8':
                        purposes += ['\u3000\u2003\u202f','\u3000Visit\u2003']
                ids=[row[0] for row in await (await conn.execute('SELECT id FROM trips WHERE account_id=%s ORDER BY id',
                    (account_id(conn),))).fetchall()]
                for i,identity in enumerate(ids):
                    await conn.execute('UPDATE trips SET purpose=%s WHERE account_id=%s AND id=%s',
                        (purposes[i%len(purposes)],account_id(conn),identity))
            async with pool.connection() as conn:
                await conn.execute("SELECT set_config('client_encoding',%s,true)",(client_encoding,))
                assert conn.info.parameter_status('client_encoding')==client_encoding
                class Operation:
                    def check(self): pass
                    def remaining_ms(self): return 15000
                class Session:
                    def __init__(self): self.actual={};self.pending=None;self.chunks=[]
                    async def send_command(self,value):
                        if value['type']=='text':
                            assert self.pending is None
                            self.pending=value;self.chunks=[]
                            if not value['size']: self.finish_text()
                        elif value['type']=='vehicle_trip':
                            row=renderer.trip(value['row'])
                            self.actual[row['id']]=row['purpose_nonblank']
                    def finish_text(self):
                        chunks=iter(self.chunks)
                        channel=SimpleNamespace(recv_text=lambda:next(chunks))
                        renderer.text(self.pending['key'],self.pending['size'],channel,self.pending.get('encoding'))
                        self.pending=None
                    async def send_text(self,value):
                        assert len(value)<=65520
                        self.chunks.append(value)
                        if sum(map(len,self.chunks))==self.pending['size']: self.finish_text()
                session=Session();projection=Projection(Operation(),session)
                bounds={'start':datetime(2026,1,1,tzinfo=TZ),'end':datetime(2027,1,1,tzinfo=TZ)}
                if unicode_suffix and client_encoding=='LATIN1':
                    # Full legacy fetch fails even when the invalid suffix is beyond a UI prefix.
                    with pytest.raises(DataError):
                        async with conn.transaction():
                            await (await conn.execute('SELECT purpose FROM trips WHERE account_id=%s ORDER BY id',
                                (account_id(conn),))).fetchall()
                    with pytest.raises(DataError):
                        async with conn.transaction():
                            await projection.groups(conn,account_id(conn),bounds,2026,False)
                    assert not session.actual
                else:
                    rows=await (await conn.execute('SELECT id,purpose FROM trips WHERE account_id=%s ORDER BY id',
                        (account_id(conn),))).fetchall()
                    expected={identity:bool((purpose or '').strip()) for identity,purpose in rows}
                    await projection.groups(conn,account_id(conn),bounds,2026,False)
                    assert session.pending is None and session.actual==expected
        finally:
            renderer.texts.close();budget.close();reservation.release()
            await raw.close()
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
