from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
import itertools
import time
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from app.expenses import build_expense_report
from app.export import to_report_xlsx, to_range_report_xlsx
from app.odometer import OdometerReading, vehicle_coverage_for_report
from app.preparation_resources import SpoolReservation, ResourceBudget
from app.preparation_sort import SortedRecords
from app.rates import YearRate
from app.report import build_annual_report, build_range_report
from app.report_renderer import Renderer, Replay

pytestmark = pytest.mark.unit
TZ = ZoneInfo('America/New_York')
RATES = {2026: YearRate(.7, .8, 7)}
NAMES = {2: 'Straße', 10: 'STRASSE', 20: '車', 101: '車'}


def trip(i, vid):
    stamp = datetime(2026, 1, 1, tzinfo=TZ) + timedelta(hours=i * 7)
    return dict(id=i, vehicle_id=vid, vehicle_name=NAMES[vid], started_at=stamp,
                ended_at=stamp + timedelta(minutes=12), display_distance_m=[1e16, 1.0, 2.0, 1e-5][i % 4],
                category=['business', 'personal', 'unclassified'][i % 3], exclusion=None,
                purpose='work' if i % 2 else '', purpose_nonblank=bool(i % 2), notes='a<&',
                has_gap=bool(i % 3), snap_status='low_confidence', source='manual',
                start_lat=2.0, start_lon=3.0, end_lat=2.1, end_lon=3.1)


def text(renderer, key, value):
    class Channel:
        def recv_text(self): return value.encode()
    renderer.text(key, len(value.encode()), Channel())


def populated(tmp_path, rows, expenses, readings, kind='annual_xlsx'):
    reservation = SpoolReservation.acquire(tmp_path / 'spool', time.monotonic() + 5)
    renderer = Renderer(ResourceBudget(reservation.directory))
    text(renderer, 'display_tz', str(TZ))
    text(renderer, 'email', 'ß A<&@example.com')
    text(renderer, 'app_version', 'test')
    renderer.initialize(dict(kind=kind, start='2026-01-01', end='2026-12-31', user=dict(id=1,is_admin=True,has_avatar=False,avatar_version=0), csrf='csrf', csp_nonce='nonce', review_count=1, storage=None, request_path='/report/2026'))
    text(renderer, 'rate1', '.7'); text(renderer, 'rate2', '.8')
    renderer.rate(dict(year=2026, has_rate2=True, h2_start_month=7))
    for row in rows: renderer.global_fold.add(row)
    for vid in NAMES:
        text(renderer, 'vehicle_name', NAMES[vid]); renderer.begin_group(vid)
        for row in rows:
            if row['vehicle_id'] == vid: renderer.group_trip(row)
        for row in expenses:
            if row['vehicle_id'] == vid: renderer.group_expense(row)
        for row in readings:
            if row['vehicle_id'] == vid: renderer.reading(row)
        renderer.end_group()
    for row in rows: renderer.detail_row(renderer.serial(row))
    for row in expenses: renderer.expense_row(renderer.serial(row))
    return renderer, reservation


def normalize(raw):
    wb = load_workbook(BytesIO(raw))
    return [(ws.title, [(tuple((c.value, c.data_type, c.number_format, str(c.font)) for c in row)) for row in ws],
             {name: dim.width for name, dim in ws.column_dimensions.items()}) for ws in wb]


def coverage(rows, readings):
    byread = {(vid, NAMES[vid]): [OdometerReading(r['recorded_at'], r['odometer_m']) for r in readings if r['vehicle_id'] == vid] for vid in NAMES}
    bytrip = {(vid, NAMES[vid]): [(r['started_at'], r['display_distance_m']) for r in rows if r['vehicle_id'] == vid] for vid in NAMES}
    return vehicle_coverage_for_report(byread, bytrip, datetime(2026,1,1,tzinfo=TZ), datetime(2027,1,1,tzinfo=TZ))


def test_complete_multivehicle_workbook(tmp_path):
    rows = [trip(i, list(NAMES)[i % 4]) for i in range(1025)]
    expenses = [dict(id=i, vehicle_id=vid, vehicle_name=NAMES[vid], incurred_on=date(2026,1,1), amount=Decimal('12.34') + i, category='fuel', treatment='business_use_allocated', notes='<&') for i,vid in enumerate(NAMES,1)]
    readings = [dict(vehicle_id=vid, vehicle_name=NAMES[vid], recorded_at=stamp, odometer_m=meters) for vid in NAMES for stamp,meters in ((datetime(2026,1,1,tzinfo=TZ),0.0),(datetime(2026,7,1,tzinfo=TZ),1e19),(datetime(2027,1,1,tzinfo=TZ),2e19))]
    renderer, reservation = populated(tmp_path, rows, expenses, readings)
    try:
        result = renderer.render()
        actual = renderer.budget.path(result['path']).read_bytes()
        expected = to_report_xlsx(build_annual_report(rows,RATES,TZ,2026),rows,RATES,TZ,coverage(rows,readings),build_expense_report(2026,rows,expenses,readings,RATES,TZ),expenses)
        assert normalize(actual) == normalize(expected)
        renderer.budget.verify('report.xlsx')
        assert not list(reservation.directory.glob('report-sheet-*'))
    finally:
        renderer.close(); reservation.release()


@pytest.mark.parametrize('reverse_treatments',[False,True])
def test_decimal_global_first_seen_orders(tmp_path,reverse_treatments):
    for permutation in itertools.permutations(NAMES):
        rows = [trip(i, vid) for i,vid in enumerate(NAMES)]
        amounts = dict(zip(NAMES, map(Decimal, ['1E25','.001','-1E25','.004'])))
        treatments = ('fully_business','business_use_allocated') if reverse_treatments else ('business_use_allocated','fully_business')
        expenses = [dict(id=i,vehicle_id=vid,vehicle_name=NAMES[vid],incurred_on=date(2026,1,1),amount=amounts[vid],category='fuel',treatment=treatment) for i,(treatment,vid) in enumerate(itertools.product(treatments,permutation),1)]
        renderer,reservation = populated(tmp_path,rows,expenses,[])
        try:
            expected = build_expense_report(2026,rows,expenses,[],RATES,TZ)
            renderer.comparisons.finish(); renderer.allocated_ranks.finish(); renderer.fully_ranks.finish()
            assert list(Replay(renderer.comparisons,renderer._comparison)) == expected.comparisons
            from app.expenses import _money
            assert _money(sum((Decimal(r['amount']) for r in renderer.allocated_ranks),Decimal('0'))) == expected.allocated_total
            assert _money(sum((Decimal(r['amount']) for r in renderer.fully_ranks),Decimal('0'))) == expected.fully_business_total
        finally: renderer.close(); reservation.release()


def test_external_sort_long_unicode_keys_and_secondary(tmp_path):
    reservation = SpoolReservation.acquire(tmp_path/'spool',time.monotonic()+5)
    budget = ResourceBudget(reservation.directory)
    try:
        for numeric in (False,True):
            sorter = SortedRecords(budget,'numeric' if numeric else 'strings',numeric=numeric)
            values = [(('ß' * 40000 + str(i % 3)).casefold(), i) for i in range(1025)]
            for key,i in reversed(values):
                raw = key.encode(); sorter.append((raw[j:j+65520] for j in range(0,len(raw),65520)), i, {'id':i})
            assert [r['id'] for r in sorter] == [i for key,i in sorted(values,key=lambda x:(x[0],x[1] if numeric else str(x[1])))]
            sorter.close()
    finally: reservation.release()


@pytest.mark.parametrize('kind', ['annual_html', 'range_html'])
def test_complete_html_literal_token_names_and_avatar(tmp_path, kind, monkeypatch):
    from types import SimpleNamespace
    from app.main import make_templates
    from app.report import next_year_disabled
    # Literal token-like source text is escaped and replayed once, never parsed recursively.
    name = ('<&\" ß\u2003車__ODOGRAPH_TEXT_0000000000000000_0000000000000003__' * 2000)
    monkeypatch.setitem(NAMES, 2, name)
    rows = [trip(1, 2)]
    renderer,reservation = populated(tmp_path,rows,[],[],kind)
    email = 'ß A<&@example.com'
    csrf = '__ODOGRAPH_TEXT_0000000000000000_0000000000000003__<&車' * 1500
    text(renderer,'csrf',csrf)
    text(renderer,'csp_nonce','nonce<&')
    try:
        result = renderer.render()
        report = build_annual_report(rows,RATES,TZ,2026) if kind.startswith('annual') else build_range_report(rows,RATES,TZ,date(2026,1,1),date(2026,12,31))
        templates = make_templates(SimpleNamespace(display_tz=TZ,app_version='test'))
        metadata = renderer.metadata
        user = {**metadata['user'],'email':email,'name':email.split('@',1)[0]}
        expected = templates.env.get_template('report.html' if kind.startswith('annual') else 'report_range.html').render(
            report=report,odometer_coverage=[],expense_report=build_expense_report(2026,rows,[],[],RATES,TZ),user=user,
            csrf=csrf,csp_nonce='nonce<&',review_count=1,storage=None,
            next_year_disabled=next_year_disabled(2026,datetime.now(TZ)),request=SimpleNamespace(url=SimpleNamespace(path='/report/2026')))
        assert renderer.budget.path(result['path']).read_text() == expected
    finally: renderer.close(); reservation.release()


def test_range_workbook_and_existing_prefix_illegal_character_order(tmp_path, monkeypatch):
    name = '車' * 32767 + '\x01remaining'
    monkeypatch.setitem(NAMES, 2, name)
    rows = [trip(1,2)]
    rows[0]['notes'] = 'n' * 32767 + '\x01'
    renderer,reservation = populated(tmp_path,rows,[],[],'range_xlsx')
    try:
        result = renderer.render()
        expected = to_range_report_xlsx(build_range_report(rows,RATES,TZ,date(2026,1,1),date(2026,12,31)),rows,RATES,TZ)
        assert normalize(renderer.budget.path(result['path']).read_bytes()) == normalize(expected)
    finally: renderer.close(); reservation.release()


def test_workbook_exhaustion_retains_no_unfinished_xml(tmp_path, monkeypatch):
    from app.preparation_resources import PreparationResourceError
    rows = [trip(i,2) for i in range(100)]
    renderer,reservation = populated(tmp_path,rows,[],[])
    # Enough for the existing inputs; insufficient for complete XML plus ZIP.
    monkeypatch.setattr('app.preparation_resources.OPERATION_BYTES',renderer.budget.usage()[0]+32768)
    try:
        with pytest.raises(PreparationResourceError): renderer.render()
        assert not list(reservation.directory.glob('report-sheet-*'))
    finally: renderer.close();reservation.release()


def test_illegal_character_inside_xlsx_prefix_still_fails_and_cleans(tmp_path):
    from openpyxl.utils.exceptions import IllegalCharacterError
    rows = [trip(1,2)]
    rows[0]['notes'] = '\x01not outside prefix'
    renderer,reservation = populated(tmp_path,rows,[],[])
    try:
        with pytest.raises(IllegalCharacterError): renderer.render()
        assert not list(reservation.directory.glob('report-sheet-*'))
    finally: renderer.close();reservation.release()
