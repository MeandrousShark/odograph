"""Complete CSV and XLSX files from bounded export rows and text references."""
from __future__ import annotations

import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import zoneinfo

from app.export import HEADERS, _export_row, _trip_distance_and_deduction, _write_only_cell, _cleanup_write_only_workbook
from app.export_filters import TextStore, prepare_filters
from app.rates import METERS_PER_MILE, YearRate
from app.report_renderer import _decode


class Renderer:
    def __init__(self,budget):
        self.budget = budget
        self.store = TextStore(budget)
        self.rates = {}
        self.book = self.sheet = self.output = None
        self.total_m = self.total_ded = 0.0
        self.finished = False

    def initialize(self,metadata):
        zoneinfo.reset_tzpath(json.loads(self.store.whole('timezone_paths')))
        self.tz = zoneinfo.ZoneInfo(self.store.whole('display_tz'))
        self.format = metadata['format']
        if self.format not in ('csv','xlsx'):
            raise ValueError('invalid export format')
        return prepare_filters(self.store,self.tz,metadata['client_codec'])

    def rate(self,row):
        self.rates[row['year']] = YearRate(float(Decimal(self.store.whole('rate1'))),
            float(Decimal(self.store.whole('rate2'))) if row['has_rate2'] else None,row['h2_start_month'])

    def start(self):
        if self.output is not None or self.book is not None:
            return
        if self.format == 'csv':
            self.output = self.budget.open('trips.csv')
            buf = io.StringIO();csv.writer(buf).writerow(HEADERS)
            self.output.write(buf.getvalue().encode('utf8'))
            return
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.worksheet._writer import WorksheetWriter
        from openpyxl.xml.functions import xmlfile
        from openpyxl.xml.constants import SHEET_MAIN_NS
        budget = self.budget
        class Writer(WorksheetWriter):
            def get_stream(inner):
                with budget.open(Path(inner.out).name) as sink,xmlfile(sink) as xf:
                    with xf.element('worksheet',xmlns=SHEET_MAIN_NS):
                        try:
                            while True:
                                element = (yield)
                                if element is True:
                                    yield xf
                                elif element is not None:
                                    xf.write(element)
                        except GeneratorExit:
                            pass
            def cleanup(inner):
                budget.remove(Path(inner.out).name)
        self.book = Workbook(write_only=True)
        self.sheet = self.book.create_sheet('Trips')
        def get_writer():
            if self.sheet._writer is None:
                self.sheet._writer = Writer(self.sheet,out=str(budget.path('export-sheet-Trips')))
                self.sheet._writer.write_top()
        self.sheet._get_writer = get_writer
        self.bold = Font(bold=True)
        self.sheet.append([_write_only_cell(self.sheet,value,font=self.bold) for value in HEADERS])

    def csv_field(self,value):
        if isinstance(value,dict):
            ref = value['text']
            quoted = any(any(c in chunk for c in ',"\r\n') for chunk in self.store.chunks(ref))
            if quoted:
                self.output.write(b'"')
            for chunk in self.store.chunks(ref):
                self.output.write((chunk.replace('"','""') if quoted else chunk).encode('utf8'))
            if quoted:
                self.output.write(b'"')
        else:
            text = str(value) if value is not None else ''
            quoted = any(c in text for c in ',"\r\n')
            self.output.write(('"'+text.replace('"','""')+'"' if quoted else text).encode('utf8'))

    def detail(self,value):
        self.start()
        value = _decode(value)
        for key,ref in tuple(value.items()):
            if isinstance(ref,dict) and 'text' in ref:
                value[key] = (self.store.prefix(ref['text']) if self.format=='xlsx' else ref) if ref['text'][1] else None
        row = _export_row(value,self.rates,self.tz)
        if self.format == 'csv':
            for i,cell in enumerate(row):
                if i:
                    self.output.write(b',')
                self.csv_field(cell)
            self.output.write(b'\r\n')
        else:
            distance,ded = _trip_distance_and_deduction(value,self.rates,self.tz)
            if value.get('exclusion')!='not_my_vehicle':
                self.total_m += distance
            if ded is not None:
                self.total_ded += ded
            if isinstance(row[-1],(int,float)):
                row[-1] = _write_only_cell(self.sheet,row[-1],number_format='"$"#,##0.00')
            self.sheet.append(row)

    def render(self):
        self.start()
        if self.format == 'xlsx':
            totals = ['']*len(HEADERS)
            totals[0] = 'Total'
            totals[6] = round(self.total_m/METERS_PER_MILE,1)
            totals[7] = round(self.total_m/1000.0,1)
            totals[15] = round(self.total_ded,2)
            self.sheet.append([_write_only_cell(self.sheet,value,font=self.bold,
                number_format='"$"#,##0.00' if i==15 else None) for i,value in enumerate(totals)])
            try:
                with self.budget.open('trips.xlsx') as output:
                    self.book.save(output)
            finally:
                _cleanup_write_only_workbook(self.book)
                self.book.close()
                self.book = None
        else:
            self.output.flush()
            self.output.close()
            self.output = None
        self.finished = True
        return {'path':'trips.'+self.format,'filename':'trips.'+self.format,
                'media_type':'text/csv' if self.format=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}

    def close(self):
        try:
            if self.book is not None:
                _cleanup_write_only_workbook(self.book)
                self.book.close()
        finally:
            if self.output is not None:
                self.output.close()
            self.store.close()


def render(channel,budget):
    renderer = Renderer(budget)
    try:
        while True:
            command = channel.recv_command()
            if command is None:
                raise EOFError('incomplete export preparation')
            kind = command['type']
            if kind=='text':
                renderer.store.receive(command['key'],command['size'],channel,command.get('encoding'))
            elif kind=='initialize':
                channel.send_response(renderer.initialize(command['metadata']))
            elif kind=='rate':
                renderer.rate(command['row'])
            elif kind=='detail':
                renderer.detail(command['row'])
            elif kind=='render':
                channel.send_response(renderer.render())
                return
            else:
                raise ValueError('unknown export command')
    finally:
        renderer.close()
