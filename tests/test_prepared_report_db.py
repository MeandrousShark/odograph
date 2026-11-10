"""Complete prepared reports match the existing database-backed workbook."""
import asyncio
import os
from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from psycopg.pq import TransactionStatus

from app.account_context import account_id, control_connection
from app.account_work import report_account_work
from app.main import make_templates
from app.page import _fetch_review_count
from app.report import next_year_disabled
from app.storage import storage_status
from app.export import to_report_xlsx, to_range_report_xlsx
from app.preparation import PreparationOperation
from app.report_preparation import prepare_report
from app.ui.reports import _build_annual_report_data, _build_range_report_data
from test_capacity_db import _scenario
from test_report_projection_db import _seed, TZ

pytestmark = [pytest.mark.db, pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='requires disposable PostGIS')]


def cells(content):
    book = load_workbook(BytesIO(content))
    return [(sheet.title, [(cell.value, cell.data_type, cell.number_format, cell.style_id)
                          for row in sheet for cell in row]) for sheet in book]


async def _move_to_carried_year(conn):
    # 2027 trips with a single 2026 rate row: the report is priced with the
    # carried rate, so both folds must agree on the carried caveat.
    await conn.execute("UPDATE trips SET started_at = started_at + interval '1 year', ended_at = ended_at + interval '1 year'")
    await conn.execute("UPDATE points SET recorded_at = recorded_at + interval '1 year'")
    await conn.execute("UPDATE expenses SET incurred_on = incurred_on + interval '1 year'")
    await conn.execute("UPDATE odometer_readings SET recorded_at = recorded_at + interval '1 year'")
    await conn.execute('DELETE FROM mileage_rates')
    await conn.execute("INSERT INTO mileage_rates (account_id, year, rate_per_mi) VALUES (%s, 2026, 0.7250)", (account_id(conn),))


@pytest.mark.parametrize('year', [2026, 2027])
@pytest.mark.parametrize('kind', ['annual_xlsx', 'range_xlsx', 'annual_html', 'range_html'])
def test_prepared_report_database_projection_and_response_cleanup(tmp_path, kind, year):
    async def run():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            account, _ = accounts
            async with account.connection() as conn:
                await _seed(conn)
                if year == 2027:
                    await _move_to_carried_year(conn)
                await conn.execute('UPDATE account_settings SET display_tz=%s WHERE account_id=%s', (str(TZ), account.principal.account_id))
            config = SimpleNamespace(display_tz=TZ, app_version='test')
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(config=config)),
                state=SimpleNamespace(principal=account.principal, account_pool=account, config=config, csp_nonce='nonce'),
                session={'csrf':'csrf'}, url=SimpleNamespace(path=f'/report/{year}'))
            user = dict(id=account.principal.account_id,is_admin=True,is_enabled=True,has_avatar=False,avatar_version=0,legacy_oidc=False)
            async with manager.operation('foreground', account.principal):
                data = await (_build_annual_report_data(request,year) if kind.startswith('annual') else _build_range_report_data(request,f'{year}-01-01',f'{year}-12-31'))
                assert data.report.caveats.carried_from_year == (2026 if year == 2027 else None)
                async with control_connection(control) as identity:
                    email = (await (await identity.execute('SELECT email FROM accounts WHERE id=%s', (account.principal.account_id,))).fetchone())[0]
                async with account.connection() as conn:
                    review_count = await _fetch_review_count(conn)
                    storage = await storage_status(conn)
                expected = None
                if kind == 'annual_xlsx':
                    expected = to_report_xlsx(data.report,data.trips,data.rates,data.tz,data.odometer_coverage,data.expense_report,data.expenses)
                elif kind == 'range_xlsx':
                    expected = to_range_report_xlsx(data.report,data.trips,data.rates,data.tz)
                else:
                    context = dict(report=data.report,user={**user,'email':email,'name':email.split('@',1)[0]},
                                   csrf='csrf',csp_nonce='nonce',review_count=review_count,storage=storage,request=request)
                    if kind == 'annual_html':
                        context.update(odometer_coverage=data.odometer_coverage,expense_report=data.expense_report,
                                       next_year_disabled=next_year_disabled(year,datetime.now(TZ)))
                    expected = make_templates(config).env.get_template('report.html' if kind == 'annual_html' else 'report_range.html').render(**context).encode()
                async with PreparationOperation(spool_root=tmp_path/'spool') as op:
                    request.state._preparation = op
                    async def work():
                        async with report_account_work(control, account.principal) as conn:
                            request.state._report_control_connection = conn
                            try:
                                response = await prepare_report(request,user,kind,year=year,start=date(year,1,1),end=date(year,12,31))
                                assert conn.info.transaction_status == TransactionStatus.IDLE
                                assert op.process.returncode == 0
                                pieces = []
                                async def send(message):
                                    assert manager.snapshot()['leases'] == 1
                                    assert conn.info.transaction_status == TransactionStatus.IDLE
                                    if message['type'] == 'http.response.body': pieces.append(message['body'])
                                await response({},None,send)
                                content = b''.join(pieces)
                                assert int(response.headers['content-length']) == len(content)
                                if kind.endswith('xlsx'): assert cells(content) == cells(expected)
                                else: assert content == expected
                            finally:
                                await op.close()
                    await op.perform(work)
                    assert op.closed and not op.directory.exists()
                assert manager.snapshot()['leases'] == 0
    asyncio.run(run())
