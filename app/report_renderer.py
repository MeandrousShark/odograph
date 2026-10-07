"""Isolated report folds and replayable file rendering, without database access."""
from __future__ import annotations

import codecs
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
from types import SimpleNamespace
from pathlib import Path
import zoneinfo
from zoneinfo import ZoneInfo

from app.preparation_sort import CHUNK_BYTES, SortedRecords
from app.report import AnnualReport, RangeReport, MonthLine, VehicleLine, ReportCaveats, NO_VEHICLE_LABEL, _rate_periods, sum_month_deductions, next_year_disabled, range_filename_slug, range_label
from app.rates import YearRate, deduction, rate_for
from app.expenses import ExpenseReport, ExpenseComparison, _money, _decimal
from app.odometer import VehicleCoverage

_TOKEN = re.compile(rb'__ODOGRAPH_TEXT_([0-9a-f]{16})_([0-9a-f]{16})__')
_TOKEN_PREFIX = b'__ODOGRAPH_TEXT_'


def _decode(row):
    row = dict(row)
    for name in ('started_at', 'ended_at', 'recorded_at'):
        if name in row:
            row[name] = datetime.fromisoformat(row[name])
    if 'incurred_on' in row:
        row['incurred_on'] = date.fromisoformat(row['incurred_on'])
    for name in ('display_distance_m', 'odometer_m', 'start_lat', 'start_lon', 'end_lat', 'end_lon'):
        if isinstance(row.get(name), str):
            row[name] = float.fromhex(row[name])
    if 'amount' in row:
        row['amount'] = Decimal(row['amount'])
    return row


class Fold:
    def __init__(self, start, end, tz):
        self.start, self.end, self.tz = start, end, tz
        self.business = self.personal = self.nondeductible = 0.0
        self.months, self.counts = {}, {}
        self.count = self.unclassified = self.gaps = self.low = self.manual = self.missing = 0
        self.included = False
        self.gps = 0.0
        self.gps_present = False

    def add(self, row):
        local = row['started_at'].astimezone(self.tz)
        if not self.start <= local.date() <= self.end:
            return False
        category, exclusion = row['category'], row.get('exclusion')
        self.unclassified += category == 'unclassified'
        if exclusion == 'not_my_vehicle':
            return False
        self.count += 1
        distance = row['display_distance_m']
        self.gps += float(distance)
        self.gps_present = True
        if exclusion == 'not_deductible':
            self.nondeductible += distance
            self.included = True
        elif category == 'business':
            self.business += distance
            self.months[local.month] = self.months.get(local.month, 0.0) + distance
            self.counts[local.month] = self.counts.get(local.month, 0) + 1
            self.missing += not row.get('purpose_nonblank', True)
            self.included = True
        elif category == 'personal':
            self.personal += distance
            self.included = True
        self.gaps += bool(row.get('has_gap'))
        self.low += row.get('snap_status') == 'low_confidence'
        self.manual += row.get('source') == 'manual'
        return True

    def report(self, rates, vehicles, annual):
        year = self.start.year
        month_rates = {m: rate_for(rates, year, m) for m in range(self.start.month, self.end.month + 1)}
        total = self.business + self.personal + self.nondeductible
        values = dict(year=year, months=[MonthLine(m, self.counts[m], self.months[m], month_rates[m], deduction(self.months[m], year, rates, m)) for m in sorted(self.months)],
                      business_m=self.business, personal_m=self.personal, nondeductible_m=self.nondeductible,
                      business_pct=self.business / total * 100.0 if total > 0 else None,
                      total_deduction=sum_month_deductions(list(self.months.items()), year, rates), rate_periods=_rate_periods(month_rates), trip_count=self.count,
                      caveats=ReportCaveats(self.gaps, self.low, self.manual, self.unclassified, self.missing, month_rates[self.start.month] is None), by_vehicle=vehicles)
        return AnnualReport(**values) if annual else RangeReport(**values, start=self.start, end=self.end)


class Replay:
    def __init__(self, records, factory):
        self.records, self.factory = records, factory

    def __bool__(self):
        return bool(self.records)

    def __iter__(self):
        for row in self.records:
            yield self.factory(row)


class Renderer:
    def __init__(self, budget):
        self.budget = budget
        self.texts = budget.open('report-text')
        self.refs = {}
        self.vehicles = SortedRecords(budget, 'report-vehicles')
        self.coverage = SortedRecords(budget, 'report-coverage')
        self.comparisons = SortedRecords(budget, 'report-comparisons', numeric=True)
        self.allocated_ranks = SortedRecords(budget, 'report-allocated')
        self.fully_ranks = SortedRecords(budget, 'report-fully')
        self.category_totals = {}
        self.rates = {}
        self.group = None
        self.detail = budget.open('report-detail')
        self.expenses = budget.open('report-expenses')

    def chunks(self, ref):
        decoder = codecs.getincrementaldecoder('utf-8')()
        with self.budget.open('report-text', 'rb') as source:
            source.seek(ref[0])
            left = ref[1]
            while left:
                data = source.read(min(CHUNK_BYTES, left))
                if not data:
                    raise ValueError('incomplete report text spool')
                left -= len(data)
                yield decoder.decode(data)
            tail = decoder.decode(b'', final=True)
            if tail:
                yield tail

    def whole(self, name):
        return ''.join(self.chunks(self.refs[name]))

    def text(self, key, size, channel):
        if key not in ('display_tz', 'email', 'app_version', 'rate1', 'rate2', 'vehicle_name', 'expense_amount', 'csrf', 'csp_nonce', 'timezone_paths') and key not in ('field:purpose', 'field:notes', 'field:vehicle_name', 'field:start_place_name', 'field:end_place_name', 'field:start_address', 'field:end_address'):
            raise ValueError('unknown report metadata')
        self.texts.seek(0, 2)
        start = self.texts.tell()
        left = size
        while left:
            data = channel.recv_text()
            if not data or len(data) > left:
                raise ValueError('invalid report text length')
            self.texts.write(data)
            left -= len(data)
        self.texts.flush()
        self.refs[key] = (start, size)

    @staticmethod
    def token(ref):
        return f'__ODOGRAPH_TEXT_{ref[0]:016x}_{ref[1]:016x}__'

    def prefix(self, ref, limit=32767):
        if not ref[1]:
            return ''
        parts, left = [], limit
        for chunk in self.chunks(ref):
            parts.append(chunk[:left])
            left -= min(left, len(chunk))
            if not left:
                break
        return ''.join(parts)

    def name(self, ref, fallback, *, xlsx=False):
        if not ref[1]:
            return fallback
        return self.prefix(ref) if xlsx else self.token(ref)

    def folded(self, ref, fallback):
        if not ref[1]:
            yield fallback.casefold().encode()
        else:
            for chunk in self.chunks(ref):
                for offset in range(0, len(chunk), 4096):
                    yield chunk[offset:offset + 4096].casefold().encode()

    def initialize(self, metadata):
        self.kind = metadata['kind']
        self.annual = self.kind.startswith('annual')
        self.xlsx = self.kind.endswith('xlsx')
        self.start = date.fromisoformat(metadata['start'])
        self.end = date.fromisoformat(metadata['end'])
        self.year = self.start.year
        if 'timezone_paths' in self.refs:
            zoneinfo.reset_tzpath(json.loads(self.whole('timezone_paths')))
        self.tz = ZoneInfo(self.whole('display_tz'))
        self.metadata = metadata
        self.global_fold = Fold(self.start, self.end, self.tz)
        beginning = datetime.combine(self.start, datetime.min.time(), self.tz)
        end = datetime.combine(self.end + timedelta(days=1), datetime.min.time(), self.tz)
        return {'start': beginning.isoformat(), 'end': end.isoformat(),
                'year_start': datetime(self.year, 1, 1, tzinfo=self.tz).isoformat(),
                'next_year_start': datetime(self.year + 1, 1, 1, tzinfo=self.tz).isoformat()}

    def rate(self, row):
        if row is not None:
            self.rates[row['year']] = YearRate(float(Decimal(self.whole('rate1'))), float(Decimal(self.whole('rate2'))) if row['has_rate2'] else None, row['h2_start_month'])

    def begin_group(self, vehicle):
        self.group = vehicle
        self.fold = Fold(self.start, self.end, self.tz)
        self.trip_file = self.budget.open('report-interval-trips')
        self.allocated = self.fully = Decimal('0')
        self.allocated_rank = self.fully_rank = None
        self.has_expenses = False
        self.anchor = self.last_reading = self.end_anchor = None
        self.invalid = False
        self.coverage_previous = self.coverage_first = self.coverage_last = None
        self.coverage_count = 0
        self.coverage_delta = self.coverage_detected = 0.0
        self.trip_iterator = None
        self.lookahead = None

    def group_trip(self, row):
        if self.fold.add(row) and self.annual and self.group is not None and self.refs['vehicle_name'][1]:
            self.trip_file.write(json.dumps([row['started_at'].isoformat(), row['display_distance_m'].hex()]).encode() + b'\n')

    def group_expense(self, row):
        if row['incurred_on'].year != self.year:
            return
        self.has_expenses = True
        rank = row['incurred_on'].isoformat().encode() + (int(row['id']) + 2**63).to_bytes(8, 'big')
        if row['treatment'] == 'fully_business':
            self.fully += row['amount']
            self.fully_rank = self.fully_rank or rank
        else:
            self.allocated += row['amount']
            self.allocated_rank = self.allocated_rank or rank

    def _trips(self):
        self.trip_file.flush()
        with self.budget.open('report-interval-trips', 'rb') as source:
            for line in source:
                stamp, distance = json.loads(line)
                yield datetime.fromisoformat(stamp), float.fromhex(distance)

    def reading(self, row):
        stamp, meters = row['recorded_at'], row['odometer_m']
        beginning = datetime(self.year, 1, 1, tzinfo=self.tz)
        ending = datetime(self.year + 1, 1, 1, tzinfo=self.tz)
        if self.end_anchor is None:
            if stamp <= beginning:
                self.anchor, self.last_reading, self.invalid = row, row, False
            else:
                if self.anchor is not None:
                    self.invalid |= _decimal(meters) <= _decimal(self.last_reading['odometer_m'])
                    self.last_reading = row
                if stamp >= ending:
                    self.end_anchor = row
        if not beginning <= stamp <= ending:
            return
        self.coverage_count += 1
        self.coverage_first = self.coverage_first or row
        self.coverage_last = row
        if self.coverage_previous is not None:
            previous = self.coverage_previous
            if self.trip_iterator is None:
                self.trip_iterator = iter(self._trips())
                self.lookahead = next(self.trip_iterator, None)
            def distances():
                while self.lookahead is not None and self.lookahead[0] < stamp:
                    trip_stamp, distance = self.lookahead
                    self.lookahead = next(self.trip_iterator, None)
                    if previous['recorded_at'] <= trip_stamp:
                        yield distance
            detected = sum(distances())
            delta = meters - previous['odometer_m']
            if not delta <= 0:
                self.coverage_delta += delta
                self.coverage_detected += detected
        self.coverage_previous = row

    def end_group(self):
        ref = self.refs['vehicle_name']
        label = self.name(ref, NO_VEHICLE_LABEL)
        vehicle_id = self.group
        if self.fold.included:
            business = sum(self.fold.months.values())
            line = VehicleLine(vehicle_id, label, business, self.fold.personal, self.fold.nondeductible,
                               business + self.fold.personal + self.fold.nondeductible,
                               sum_month_deductions(list(self.fold.months.items()), self.year, self.rates))
            self.vehicles.append(self.folded(ref, NO_VEHICLE_LABEL), str(vehicle_id), {**asdict(line), '_name': ref})
        if vehicle_id is not None and self.annual:
            if self.coverage_count >= 2 and self.coverage_delta > 0:
                first, last = self.coverage_first['recorded_at'], self.coverage_last['recorded_at']
                value = dict(vehicle_name=self.name(ref, ''), span_start=first.isoformat(), span_end=last.isoformat(),
                             coverage=self.coverage_detected / self.coverage_delta, gap_m=self.coverage_delta - self.coverage_detected,
                             fully_bracketed=first <= datetime(self.year, 1, 1, tzinfo=self.tz) and last >= datetime(self.year + 1, 1, 1, tzinfo=self.tz), _name=ref)
                self.coverage.append([f'({vehicle_id},'.encode()], str(vehicle_id), value)
            if self.fold.gps_present or self.has_expenses:
                line = self.expense_comparison(ref)
                self.comparisons.append(self.folded(ref, f'Vehicle {vehicle_id}'), vehicle_id, {**asdict(line), '_name': ref})
            for sorter, rank, amount in ((self.allocated_ranks, self.allocated_rank, self.allocated), (self.fully_ranks, self.fully_rank, self.fully)):
                if rank is not None:
                    sorter.append([rank], vehicle_id, {'amount': str(amount)})
        if self.trip_iterator is not None:
            self.trip_iterator.close()
        self.trip_file.close()
        self.budget.remove('report-interval-trips')
        self.group = None

    def expense_comparison(self, ref):
        biz, denominator, source, provisional = self.fold.business, self.fold.gps, 'gps', True
        denominator_decimal = _decimal(denominator)
        invalid = False
        if self.anchor is not None and self.end_anchor is not None:
            delta = _decimal(self.end_anchor['odometer_m']) - _decimal(self.anchor['odometer_m'])
            if not self.invalid and delta >= _decimal(biz) and delta > 0:
                denominator_decimal, denominator, source, provisional = delta, float(delta), 'odometer', False
            else:
                invalid = True
        pct = _decimal(biz) / denominator_decimal if denominator_decimal > 0 else None
        allocated, fully = _money(self.allocated), _money(self.fully)
        standard, any_rate = Decimal('0'), False
        for month, meters in self.fold.months.items():
            value = deduction(meters, self.year, self.rates, month)
            if value is not None:
                standard += _decimal(value)
                any_rate = True
        standard = _money(standard + fully) if any_rate or biz == 0 else None
        actual = _money(allocated * pct + fully) if pct is not None else None
        difference = leader = None
        if standard is not None and actual is not None:
            difference = _money(abs(actual - standard))
            leader = 'actual' if actual > standard else 'standard' if standard > actual else 'tie'
        return ExpenseComparison(self.group, self.name(ref, f'Vehicle {self.group}'), biz, denominator, source, provisional, invalid, pct, allocated, fully, standard, actual, difference, leader)

    @staticmethod
    def serial(row):
        return {k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, (date, datetime)) else v.hex() if isinstance(v, float) else v) for k, v in row.items()}

    def detail_row(self, row):
        self.detail.write(json.dumps(row, separators=(',', ':')).encode() + b'\n')

    def decoded(self, row):
        row = dict(row)
        amount = row.get('amount')
        if isinstance(amount, dict) and 'text' in amount:
            row['amount'] = ''.join(self.chunks(amount['text']))
        return _decode(row)

    def expense_row(self, row):
        decoded = self.decoded(row)
        category, amount = decoded['category'], decoded['amount']
        self.category_totals[category] = self.category_totals.get(category, Decimal('0')) + amount
        if self.xlsx:
            self.expenses.write(json.dumps(row, separators=(',', ':')).encode() + b'\n')

    def rows(self, name):
        with self.budget.open(name, 'rb') as source:
            for line in source:
                row = json.loads(line)
                for field, value in row.items():
                    if isinstance(value, dict) and 'text' in value:
                        row[field] = ''.join(self.chunks(value['text'])) if field == 'amount' else self.prefix(value['text'])
                yield _decode(row)

    def _vehicle(self, row):
        ref = row.pop('_name')
        row['vehicle_name'] = self.name(ref, NO_VEHICLE_LABEL, xlsx=self.xlsx)
        return VehicleLine(**row)

    def _coverage(self, row):
        ref = row.pop('_name')
        row['vehicle_name'] = self.name(ref, '', xlsx=self.xlsx)
        row['span_start'] = datetime.fromisoformat(row['span_start'])
        row['span_end'] = datetime.fromisoformat(row['span_end'])
        return VehicleCoverage(**row)

    def _comparison(self, row):
        ref = row.pop('_name')
        row['vehicle_name'] = self.name(ref, f"Vehicle {row['vehicle_id']}", xlsx=self.xlsx)
        for field in ('business_pct', 'allocated_expenses', 'fully_business_expenses', 'standard_total', 'actual_total', 'difference'):
            if row[field] is not None:
                row[field] = Decimal(row[field])
        return ExpenseComparison(**row)

    def render(self):
        for output in (self.detail, self.expenses, self.texts):
            output.flush()
        for records in (self.vehicles, self.coverage, self.comparisons, self.allocated_ranks, self.fully_ranks):
            records.finish()
        report = self.global_fold.report(self.rates, Replay(self.vehicles, self._vehicle), self.annual)
        coverage = Replay(self.coverage, self._coverage)
        allocated = _money(sum((Decimal(row['amount']) for row in self.allocated_ranks), Decimal('0')))
        fully = _money(sum((Decimal(row['amount']) for row in self.fully_ranks), Decimal('0')))
        expenses = ExpenseReport(Replay(self.comparisons, self._comparison), {k: _money(v) for k, v in self.category_totals.items()}, allocated, fully, _money(allocated + fully))
        if self.xlsx:
            self._xlsx(report, coverage, expenses)
            filename = f'mileage-report-{self.year}.xlsx' if self.annual else f'mileage-report-{range_filename_slug(self.start, self.end)}.xlsx'
            result = {'path': 'report.xlsx', 'media_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'filename': filename}
        else:
            self._html(report, coverage, expenses)
            result = {'path': 'report.html', 'media_type': 'text/html', 'filename': None}
        return result

    def _xlsx(self, report, coverage, expenses):
        from openpyxl import Workbook
        from app.export import _write_summary_sheet, _populate_trips_sheet, _populate_expenses_sheet, _cleanup_write_only_workbook
        from openpyxl.worksheet._writer import WorksheetWriter
        from openpyxl.xml.functions import xmlfile
        from openpyxl.xml.constants import SHEET_MAIN_NS
        budget = self.budget
        class Writer(WorksheetWriter):
            def get_stream(inner):
                with budget.open(Path(inner.out).name) as sink, xmlfile(sink) as xf:
                    with xf.element('worksheet', xmlns=SHEET_MAIN_NS):
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
        wb = Workbook(write_only=True)
        def sheet(name):
            ws = wb.create_sheet(name)
            # Openpyxl archives writer.out as a path; use a charged sink and
            # an absolute path referring to that same request-owned XML file.
            def get_writer():
                if ws._writer is None:
                    ws._writer = Writer(ws, out=str(budget.path('report-sheet-' + name)))
                    ws._writer.write_top()
            ws._get_writer = get_writer
            return ws
        try:
            _write_summary_sheet(sheet('Summary'), report, coverage if self.annual else None, expenses if self.annual else None,
                                 title=None if self.annual else f'Mileage Report: {range_label(self.start, self.end)}')
            _populate_trips_sheet(sheet('Trips'), self.rows('report-detail'), self.rates, self.tz)
            if self.annual:
                _populate_expenses_sheet(sheet('Expenses'), self.rows('report-expenses'))
            with budget.open('report.xlsx') as destination:
                wb.save(destination)
        finally:
            _cleanup_write_only_workbook(wb)
            wb.close()

    def _html(self, report, coverage, expenses):
        from app.main import make_templates
        from markupsafe import escape
        templates = make_templates(SimpleNamespace(display_tz=self.tz, app_version=self.token(self.refs['app_version'])))
        user = dict(self.metadata['user'])
        email_ref = self.refs['email']
        # Account shell uses the localpart as its name. This descriptor avoids
        # constructing the arbitrary email or its split-derived strings.
        local_size = 0
        for chars in self.chunks(email_ref):
            before, found, _ = chars.partition('@')
            local_size += len(before.encode())
            if found:
                break
        name_ref = (email_ref[0], local_size)
        user['name'], user['email'] = self.token(name_ref) if local_size else '', self.token(email_ref)
        words = 0
        first = last = ''
        in_word = False
        avatar_ref = name_ref if local_size else email_ref
        stop = False
        for chars in self.chunks(avatar_ref):
            for char in chars:
                if char == '@':
                    stop = True
                    break
                if char.isspace():
                    in_word = False
                elif not in_word:
                    words += 1
                    first = first or char
                    last = char
                    in_word = True
            if stop:
                break
        # A string subclass gives the existing shell's split operations their
        # bounded derived value, while interpolation remains a replay token.
        class AvatarSeed(str):
            def split(inner, separator=None, maxsplit=-1):
                return [] if not words else [first] if words == 1 else [first, last]
        class DisplayName(str):
            def split(inner, separator=None, maxsplit=-1):
                if separator == '@':
                    return [AvatarSeed('')]
                return super().split(separator, maxsplit)
        user['name'] = DisplayName(user['name']) if local_size else ''
        user['email'] = DisplayName(user['email']) if email_ref[1] else ''
        context = dict(report=report, odometer_coverage=coverage, expense_report=expenses, user=user,
                       csrf=self.token(self.refs['csrf']) if 'csrf' in self.refs else self.metadata['csrf'], csp_nonce=self.token(self.refs['csp_nonce']) if 'csp_nonce' in self.refs else self.metadata['csp_nonce'], review_count=self.metadata['review_count'], storage=self.metadata['storage'],
                       next_year_disabled=next_year_disabled(self.year, datetime.now(self.tz)), request=SimpleNamespace(url=SimpleNamespace(path=self.metadata['request_path'])))
        template = templates.env.get_template('report.html' if self.annual else 'report_range.html')
        pending = b''
        with self.budget.open('report.html') as target:
            for chunk in template.generate(**context):
                pending += chunk.encode()
                while pending:
                    at = pending.find(_TOKEN_PREFIX)
                    if at < 0:
                        keep = min(len(pending), len(_TOKEN_PREFIX) - 1)
                        target.write(pending[:-keep] if keep else pending)
                        pending = pending[-keep:] if keep else b''
                        break
                    target.write(pending[:at])
                    match = _TOKEN.match(pending, at)
                    if match is None:
                        pending = pending[at:]
                        if len(pending) > 64:
                            raise ValueError('invalid report replay token')
                        break
                    ref = (int(match[1], 16), int(match[2], 16))
                    if ref[0] + ref[1] > self.texts.seek(0, 2):
                        raise ValueError('invalid report replay reference')
                    for text in self.chunks(ref):
                        for offset in range(0, len(text), 8192):
                            target.write(str(escape(text[offset:offset + 8192])).encode())
                    pending = pending[match.end():]
            target.write(pending)

    def close(self):
        if self.group is not None:
            if self.trip_iterator is not None:
                self.trip_iterator.close()
            self.trip_file.close()
        for output in (self.texts, self.detail, self.expenses):
            output.close()
        for records in (self.vehicles, self.coverage, self.comparisons, self.allocated_ranks, self.fully_ranks):
            records.close()


def render(channel, budget):
    renderer = Renderer(budget)
    try:
        while True:
            command = channel.recv_command()
            kind = command['type']
            if kind == 'text':
                renderer.text(command['key'], command['size'], channel)
            elif kind == 'initialize':
                channel.send_response(renderer.initialize(command['metadata']))
            elif kind == 'rate':
                renderer.rate(command['row'])
            elif kind == 'global_trip':
                renderer.global_fold.add(_decode(command['row']))
            elif kind == 'vehicle_start':
                renderer.begin_group(command['id'])
            elif kind == 'vehicle_trip':
                renderer.group_trip(_decode(command['row']))
            elif kind == 'vehicle_expense':
                renderer.group_expense(renderer.decoded(command['row']))
            elif kind == 'reading':
                renderer.reading(_decode(command['row']))
            elif kind == 'vehicle_end':
                renderer.end_group()
            elif kind == 'detail':
                renderer.detail_row(command['row'])
            elif kind == 'expense':
                renderer.expense_row(command['row'])
            elif kind == 'render':
                channel.send_response(renderer.render())
                return
            else:
                raise ValueError('unknown report command')
    finally:
        renderer.close()
