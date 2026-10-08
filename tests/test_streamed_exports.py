from datetime import datetime, timedelta
from io import BytesIO
import json
import time
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook
from openpyxl.utils.exceptions import IllegalCharacterError

from app.export import to_csv, to_xlsx
from app.export_renderer import Renderer
from app.preparation_resources import SpoolReservation, ResourceBudget, PreparationResourceError
from app.rates import YearRate
from app.report_preparation import _encode
from test_export_filter_preparation import text

pytestmark=pytest.mark.unit
TZ=ZoneInfo('America/New_York')
RATES={-3:YearRate(.2),0:YearRate(.3),2026:YearRate(.7,.8,7),9999:YearRate(.9),10000:YearRate(1e10)}
FIELDS=('start_place_name','end_place_name','start_address','end_address','vehicle_name','purpose','notes')


def trip(i):
    start=datetime(2026 if i%5 else 2025,1,1,tzinfo=TZ)+timedelta(hours=i)
    return dict(id=i,started_at=start,ended_at=start+timedelta(minutes=72),
        display_distance_m=[1e16,1.0,2.0,1e-5,-1e16][i%5],category=['business','personal','unclassified'][i%3],
        exclusion=[None,'not_deductible','not_my_vehicle'][i%3],has_gap=bool(i%2),source='manual',
        start_place_name='Place 車,%"\r\n' if i%3==0 else None,end_place_name='' if i%2 else None,
        start_address='Full street, City 車' if i%3==1 else None,end_address=None,
        start_lat=2.0,start_lon=3.0,end_lat=None,end_lon=None,
        vehicle_name='=1+1' if i%3==1 else '#N/A' if i%3==2 else '車',purpose='Visit,%"\r\n',notes='=SUM(A1:A2)')


def signature(raw):
    book=load_workbook(BytesIO(raw))
    try:
        return [(sheet.title,[(cell.value,cell.data_type,cell.number_format,str(cell.font),str(cell.fill),str(cell.border),str(cell.alignment),str(cell.protection))
            for row in sheet for cell in row],str(sheet.sheet_format),str(sheet.sheet_view)) for sheet in book]
    finally: book.close()


def make_renderer(tmp_path,format):
    reservation=SpoolReservation.acquire(tmp_path/'spool',time.monotonic()+5)
    renderer=Renderer(ResourceBudget(reservation.directory))
    for key,value in (('display_tz',str(TZ)),('timezone_paths',json.dumps(__import__('zoneinfo').TZPATH)),
            ('category',''),('from',''),('to',''),('vehicle',''),('q',''),('exclusion','')):
        text(renderer.store,key,value)
    renderer.initialize({'format':format,'client_codec':'utf8'})
    for year,rate in RATES.items():
        if 1<=year<=9999 or year==0:
            text(renderer.store,'rate1',str(rate.rate_per_mi))
            if rate.rate_h2_per_mi is not None: text(renderer.store,'rate2',str(rate.rate_h2_per_mi))
            renderer.rate(dict(year=year,has_rate2=rate.rate_h2_per_mi is not None,h2_start_month=rate.h2_start_month))
    return renderer,reservation


def send_row(renderer,row):
    result=_encode(row)
    for field in FIELDS:
        value=row.get(field)
        result[field]=None if value is None else {'text':text(renderer.store,'field:'+field,value,65520)}
    renderer.detail(result)


@pytest.mark.parametrize('format',['csv','xlsx'])
def test_complete_export_matches_legacy_bytes_or_every_cell_style(tmp_path,format):
    rows=[trip(i) for i in range(513)]
    rows.reverse()
    renderer,reservation=make_renderer(tmp_path,format)
    try:
        for row in rows: send_row(renderer,row)
        result=renderer.render()
        actual=renderer.budget.path(result['path']).read_bytes()
        expected=to_csv(rows,RATES,TZ) if format=='csv' else to_xlsx(rows,RATES,TZ)
        assert actual==expected if format=='csv' else signature(actual)==signature(expected)
        assert not list(reservation.directory.glob('export-sheet-*'))
    finally:
        renderer.close();renderer.budget.close();reservation.release()


@pytest.mark.parametrize('format',['csv','xlsx'])
def test_long_unicode_and_old_character_prefix(tmp_path,format):
    row=trip(0)
    row['start_place_name']='車' * 32767 + '\x01beyond prefix'
    row['purpose']='😀,%"\r\n' * 16000
    row['notes']='車'*32767+'\x01beyond prefix'
    renderer,reservation=make_renderer(tmp_path,format)
    try:
        send_row(renderer,row)
        actual=renderer.budget.path(renderer.render()['path']).read_bytes()
        if format=='csv': assert actual==to_csv([row],RATES,TZ)
        else: assert signature(actual)==signature(to_xlsx([row],RATES,TZ))
    finally:
        renderer.close();renderer.budget.close();reservation.release()


def test_illegal_character_before_prefix_cleans_unfinished_xml(tmp_path):
    row=trip(0);row['notes']='車'*32766+'\x01'
    renderer,reservation=make_renderer(tmp_path,'xlsx')
    try:
        with pytest.raises(IllegalCharacterError): send_row(renderer,row)
        renderer.close()
        assert not list(reservation.directory.glob('export-sheet-*'))
        with pytest.raises(IllegalCharacterError): to_xlsx([row],RATES,TZ)
    finally:
        renderer.budget.close();reservation.release()


@pytest.mark.parametrize('format',['csv','xlsx'])
def test_empty_export_matches_baseline(tmp_path,format):
    renderer,reservation=make_renderer(tmp_path,format)
    try:
        actual=renderer.budget.path(renderer.render()['path']).read_bytes()
        assert actual==to_csv([],RATES,TZ) if format=='csv' else signature(actual)==signature(to_xlsx([],RATES,TZ))
    finally:
        renderer.close();renderer.budget.close();reservation.release()


@pytest.mark.parametrize('format',['csv','xlsx'])
def test_output_growth_failure_retains_charge_and_cleans_worksheet(tmp_path,format,monkeypatch):
    renderer,reservation=make_renderer(tmp_path,format)
    row=trip(0)
    try:
        send_row(renderer,row)
        original=renderer.budget._change
        def exhausted(delta):
            if delta>0: raise PreparationResourceError('test resource refusal')
            return original(delta)
        monkeypatch.setattr(renderer.budget,'_change',exhausted)
        if format=='csv':
            row['notes']='quotes,"'*5000
            # Source spooling would fail first, so exercise the prepared sink.
            with pytest.raises(PreparationResourceError): renderer.output.write(b'x'*65520)
        else:
            with pytest.raises(PreparationResourceError): renderer.render()
        renderer.close()
        assert not list(reservation.directory.glob('export-sheet-*'))
        assert renderer.budget.usage()[0]>=65536
    finally:
        renderer.budget.close();reservation.release()
