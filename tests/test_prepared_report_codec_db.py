"""Real client conversion and complete report bytes/cells remain compatible."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
from io import BytesIO
import os
from types import SimpleNamespace

from openpyxl import load_workbook
import pytest

from app.account_context import control_connection
from app.account_work import report_account_work
from app.export import to_report_xlsx, to_range_report_xlsx
from app.main import make_templates
from app.page import _fetch_review_count
from app.preparation import PreparationOperation
from app.report import next_year_disabled
from app.report_preparation import prepare_report
from app.storage import storage_status
from app.ui.reports import _build_annual_report_data, _build_range_report_data
from test_capacity_db import _scenario
from test_report_projection_db import _seed, TZ

pytestmark = [pytest.mark.db,pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'),reason='requires disposable PostGIS')]


def signature(content):
    book=load_workbook(BytesIO(content))
    try:
        return [(sheet.title,[(cell.value,cell.data_type,cell.number_format,str(cell.font),str(cell.fill),str(cell.border),str(cell.alignment),str(cell.protection)) for row in sheet for cell in row],str(sheet.sheet_format),str(sheet.sheet_view)) for sheet in book]
    finally: book.close()


class ClientPool:
    def __init__(self,account,client): self.account,self.client=account,client
    @asynccontextmanager
    async def connection(self,**kwargs):
        async with self.account.connection(**kwargs) as conn:
            await conn.execute("SELECT set_config('client_encoding',%s,true)",(self.client,))
            yield conn


def request_for(account,client):
    config=SimpleNamespace(display_tz=TZ,app_version='test')
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)),
        state=SimpleNamespace(principal=account.principal,account_pool=ClientPool(account,client),config=config,csp_nonce='nonce'),
        session={'csrf':'csrf'},url=SimpleNamespace(path='/report/2026'))


async def existing(request,control,user,kind,client):
    data=await (_build_annual_report_data(request,2026) if kind.startswith('annual') else _build_range_report_data(request,'2026-01-01','2026-12-31'))
    if kind=='annual_xlsx': return to_report_xlsx(data.report,data.trips,data.rates,data.tz,data.odometer_coverage,data.expense_report,data.expenses)
    if kind=='range_xlsx': return to_range_report_xlsx(data.report,data.trips,data.rates,data.tz)
    async with control_connection(control) as conn:
        await conn.execute("SELECT set_config('client_encoding',%s,true)",(client,))
        email=(await (await conn.execute('SELECT email FROM accounts WHERE id=%s',(request.state.principal.account_id,))).fetchone())[0]
    async with request.state.account_pool.connection() as conn:
        review_count=await _fetch_review_count(conn)
        storage=await storage_status(conn)
    context=dict(report=data.report,user={**user,'email':email,'name':email.split('@',1)[0]},csrf='csrf',csp_nonce='nonce',review_count=review_count,storage=storage,request=request)
    if kind=='annual_html': context.update(odometer_coverage=data.odometer_coverage,expense_report=data.expense_report,next_year_disabled=next_year_disabled(2026,datetime.now(TZ)))
    return make_templates(request.state.config).env.get_template('report.html' if kind=='annual_html' else 'report_range.html').render(**context).encode()


@pytest.mark.parametrize('kind',['annual_html','range_html','annual_xlsx','range_xlsx'])
@pytest.mark.parametrize('client,value',[('UTF8','車😀'),('LATIN1','Caféß'),('SJIS','¥～車')],ids=['utf8','latin1','sjis'])
def test_complete_client_decoded_reports_match_existing_oracle(tmp_path,kind,client,value):
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with account.connection() as conn:
                await _seed(conn)
                owner=account.principal.account_id
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s',(str(TZ),owner))
                await conn.execute('UPDATE vehicles SET name=%s WHERE account_id=%s',(value*17000,owner))
                await conn.execute('UPDATE trips SET notes=%s,purpose=%s WHERE account_id=%s',(value*17000+'\x01outside prefix',value+' work',owner))
                await conn.execute('UPDATE trips SET purpose=%s WHERE account_id=%s AND id=(SELECT min(id) FROM trips WHERE account_id=%s)',('\u3000\t' if client!='LATIN1' else '\xa0\t',owner,owner))
                await conn.execute('UPDATE expenses SET notes=%s WHERE account_id=%s',(value*17000+'\x01outside prefix',owner))
            async with raw.connection() as conn:
                await conn.execute('UPDATE accounts SET email=%s WHERE id=%s',(value.lower()+'a__odograph_text_0000000000000000_0000000000000001__@example.test',account.principal.account_id))
            request=request_for(account,client)
            user=dict(id=account.principal.account_id,is_admin=True,is_enabled=True,has_avatar=False,avatar_version=0,legacy_oidc=False)
            async with manager.operation('foreground',account.principal):
                expected=await existing(request,control,user,kind,client)
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    request.state._preparation=operation
                    async def work():
                        async with report_account_work(control,account.principal) as lease:
                            await lease.execute("SELECT set_config('client_encoding',%s,false)",(client,))
                            request.state._report_control_connection=lease
                            response=await prepare_report(request,user,kind,year=2026,start=date(2026,1,1),end=date(2026,12,31))
                            messages=[]
                            async def send(message): messages.append(message)
                            await response({},None,send)
                            actual=b''.join(message.get('body',b'') for message in messages)
                            assert actual==expected if kind.endswith('html') else signature(actual)==signature(expected)
                    await operation.perform(work)
                    assert operation.closed and not operation.directory.exists()
    asyncio.run(run())


@pytest.mark.parametrize('kind,field',[
    ('annual_html','purpose'),('range_html','purpose'),('range_xlsx','notes'),
    ('annual_xlsx','expense_notes'),('annual_xlsx','vehicle'),
])
def test_used_text_client_conversion_failure_precedes_prefix_and_response(tmp_path,kind,field):
    from psycopg.errors import UntranslatableCharacter
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            async with account.connection() as conn:
                await _seed(conn)
                owner=account.principal.account_id
                value='a'*32768+'車'
                if field=='expense_notes':
                    await conn.execute('UPDATE expenses SET notes=%s WHERE account_id=%s',(value,owner))
                elif field=='vehicle':
                    await conn.execute('UPDATE vehicles SET name=%s WHERE account_id=%s',(value,owner))
                else:
                    await conn.execute(f'UPDATE trips SET {field}=%s WHERE account_id=%s',(value,owner))
            request=request_for(account,'LATIN1')
            user=dict(id=account.principal.account_id,is_admin=True,is_enabled=True,has_avatar=False,avatar_version=0,legacy_oidc=False)
            async with manager.operation('foreground',account.principal):
                with pytest.raises(UntranslatableCharacter): await existing(request,control,user,kind,'LATIN1')
                with pytest.raises(UntranslatableCharacter):
                    async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                        request.state._preparation=operation
                        async def work():
                            async with report_account_work(control,account.principal) as lease:
                                request.state._report_control_connection=lease
                                await prepare_report(request,user,kind,year=2026,start=date(2026,1,1),end=date(2026,12,31))
                        await operation.perform(work)
                assert operation.closed and not operation.directory.exists()
                assert operation.process.returncode is not None
    asyncio.run(run())


def test_client_sqlascii_preserves_settings_bytes_type_error(tmp_path):
    from zoneinfo import ZoneInfo
    async def run():
        async with _scenario() as (raw,manager,runtime,control,accounts):
            account,_=accounts
            request=request_for(account,'SQL_ASCII')
            async with manager.operation('foreground',account.principal):
                with pytest.raises(TypeError):
                    async with request.state.account_pool.connection() as conn:
                        value=(await (await conn.execute('SELECT display_tz FROM account_settings')).fetchone())[0]
                        ZoneInfo(value)
                with pytest.raises(TypeError):
                    async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                        request.state._preparation=operation
                        async def work():
                            async with report_account_work(control,account.principal) as lease:
                                request.state._report_control_connection=lease
                                await prepare_report(request,{'id':account.principal.account_id},'annual_html',year=2026)
                        await operation.perform(work)
                assert operation.closed and not operation.directory.exists()
    asyncio.run(run())
