"""Digest notification text and settings preparation in the isolated helper."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import codecs
import zoneinfo

from app.odometer import latest_quarter_start
from app.preparation_sort import CHUNK_BYTES, SortedRecords

_TEXT_KEYS = {'display_tz', 'email_to', 'current_display_tz', 'current_email_to',
              'timezone_paths', 'app_url', 'email_from', 'smtp_host', 'smtp_username',
              'smtp_password', 'smtp_security', 'vehicle_name', 'filing_mmdd', 'current_filing_mmdd', 'ntfy_topic', 'rate', 'rate_h2'}


class Renderer:
    def __init__(self, budget):
        self.budget = budget
        self.texts = budget.open('notification-text')
        self.refs = {}
        self.vehicles = SortedRecords(budget, 'notification-vehicles', numeric=True)
        self.initialized = False

    def text(self,key,size,channel,encoding='utf8',keep=True,literal=False):
        if (key not in _TEXT_KEYS or type(size) is not int or size < 0 or type(keep) is not bool
                or type(literal) is not bool or (literal and encoding != 'utf8')):
            raise ValueError('invalid notification metadata')
        self.texts.seek(0, 2)
        offset, left = self.texts.tell(), size
        errors = 'surrogatepass' if literal else 'strict'
        decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        while left:
            chunk = channel.recv_text()
            if not chunk or len(chunk) > left:
                raise ValueError('invalid notification text frame')
            decoded = decoder.decode(chunk)
            if keep:
                self.texts.write(decoded.encode('utf8',errors))
            left -= len(chunk)
        tail = decoder.decode(b'', final=True)
        if keep:
            self.texts.write(tail.encode('utf8',errors))
        self.texts.flush()
        self.refs[key] = (offset,self.texts.tell()-offset,errors) if literal and keep else (offset,self.texts.tell()-offset) if keep else None

    def chunks(self, ref):
        with self.budget.open('notification-text', 'rb') as source:
            source.seek(ref[0])
            left = ref[1]
            while left:
                chunk = source.read(min(left, CHUNK_BYTES))
                if not chunk:
                    raise ValueError('incomplete notification text spool')
                yield chunk
                left -= len(chunk)

    def whole(self, key):
        # Complete metadata parsing is confined by the helper's memory authority.
        ref = self.refs[key]
        return b''.join(self.chunks(ref)).decode('utf8',ref[2] if len(ref)==3 else 'strict')

    def initialize(self, command):
        zoneinfo.reset_tzpath(json.loads(self.whole('timezone_paths')))
        tz = zoneinfo.ZoneInfo(self.whole('display_tz'))
        now = datetime.fromisoformat(command['now']) if command['now'] is not None else datetime.now(tz)
        self.now = now.astimezone(tz)
        self.port, self.tls_insecure = command['port'], command['tls_insecure']
        self.initialized = True
        return {}

    def quarter_bounds(self, hour):
        if not self.initialized:
            raise ValueError('notification is not initialized')
        return {'quarter_start':latest_quarter_start(self.now,hour).isoformat()}

    def email_bounds(self, kind, hour):
        if not self.initialized:
            raise ValueError('notification is not initialized')
        from app.nudge import latest_window_end
        from app.email_digest import latest_month_boundary, latest_filing_reminder_at, covered_month, _calendar_month_days
        self.kind = kind
        if kind == 'quarterly_odometer':
            self.bounds = {'period_end':latest_quarter_start(self.now,hour).isoformat()}
        elif kind == 'weekly_nudge':
            end = latest_window_end(self.now,hour)
            self.bounds = {'period_end':end.isoformat(),'window_start':(end-timedelta(days=7)).isoformat()}
        elif kind in ('monthly_summary','filing_reminder'):
            if kind == 'monthly_summary':
                end = latest_month_boundary(self.now,hour)
                year,month = covered_month(end)
                start,last = _calendar_month_days(year,month)
            else:
                end = latest_filing_reminder_at(self.now,self.whole('filing_mmdd'),hour)
                year,month = end.year-1,None
                self.bounds = {'period_end':end.isoformat(),'year':year,'month':month}
                from app.digest_summary import DigestSummary
                self.summary,self.rates = DigestSummary(),{}
                return self.bounds
            next_day = last+timedelta(days=1)
            self.bounds = {'period_end':end.isoformat(),'year':year,'month':month,
                'start':start.isoformat(),'end':last.isoformat(),
                'range_start':datetime.combine(start,datetime.min.time(),tzinfo=self.now.tzinfo).isoformat(),
                'range_end':datetime.combine(next_day,datetime.min.time(),tzinfo=self.now.tzinfo).isoformat()}
            from app.digest_summary import DigestSummary
            self.summary = DigestSummary()
            self.rates = {}
        else:
            raise ValueError('unknown email kind')
        return self.bounds

    def history_bounds(self):
        # Filing constructs its covered-year dates only after preference/ledger checks.
        if self.kind != 'filing_reminder':
            raise ValueError('invalid filing history request')
        year = self.bounds['year']
        start,last = date(year,1,1),date(year,12,31)
        next_day = last+timedelta(days=1)
        self.bounds.update(start=start.isoformat(),end=last.isoformat(),
            range_start=datetime.combine(start,datetime.min.time(),tzinfo=self.now.tzinfo).isoformat(),
            range_end=datetime.combine(next_day,datetime.min.time(),tzinfo=self.now.tzinfo).isoformat())
        return self.bounds

    def history_rows(self, rows):
        if self.kind not in ('monthly_summary','filing_reminder') or len(rows)>256:
            raise ValueError('invalid digest history')
        self.summary.fold(((datetime.fromisoformat(started),category,exclusion,float.fromhex(distance))
            for started,category,exclusion,distance in rows),self.now.tzinfo,
            date.fromisoformat(self.bounds['start']),date.fromisoformat(self.bounds['end']))

    def rate(self, command):
        from app.rates import YearRate
        self.rates[command['year']] = YearRate(float(self.whole('rate')),
            float(self.whole('rate_h2')) if command['h2'] else None,command['month'])

    def history_finish(self):
        self.summary.finish(self.rates,self.bounds['year'])
        return {}

    def email_body(self):
        from app.email_digest import _render, MONTH_ABBR
        from app.formatting import format_miles, format_usd
        # Tokens stand only for fixed template positions. Variable URLs stream afterwards.
        links = {}
        if self.kind == 'weekly_nudge':
            subject = 'Odograph: weekly unclassified-trip digest'
            context = {'count':self.weekly_count,'noun':'trip' if self.weekly_count == 1 else 'trips'}
            paths = {'review_url':'/review'}
        else:
            year,month = self.bounds['year'],self.bounds['month']
            context = {'business_mi':format_miles(self.summary.business_m),
                'nondeductible_mi':format_miles(self.summary.nondeductible_m) if self.summary.nondeductible_m else '',
                'deduction':format_usd(self.summary.total_deduction)}
            if self.kind == 'monthly_summary':
                context.update(month_label=f'{MONTH_ABBR[month]} {year}',unclassified=self.summary.unclassified_trips)
                subject = f'Odograph: {MONTH_ABBR[month]} summary'
                paths = {'report_url':f"/report/range?from={self.bounds['start']}&to={self.bounds['end']}"}
            else:
                context['year'] = year
                subject = f'Odograph: {year} filing reminder'
                paths = {'report_url':f'/report/{year}','export_url':f'/report/{year}/export'}
        for index,(key,path) in enumerate(paths.items()):
            token = f'__ODOGRAPH_NOTIFICATION_URL_{index}__'
            context[key] = token if self.refs['app_url'][1] else ''
            links[token] = path
        body = _render(self.kind+'.txt',**context)
        with self.budget.open('mail-body') as output:
            while body:
                positions = [(body.find(token),token) for token in links if token in body]
                if not positions:
                    output.write(body.encode('utf8'))
                    break
                index,token = min(positions)
                output.write(body[:index].encode('utf8'))
                for chunk in self.chunks(self.refs['app_url']):
                    output.write(chunk)
                output.write(links[token].encode('ascii'))
                body = body[index+len(token):]
        return subject

    def prepare_email(self):
        if self.kind == 'weekly_nudge' and not self.weekly_count:
            return {'should_send':False,'artifacts':None}
        subject = self.email_body()
        return {'should_send':True,'artifacts':self.mime(subject)}

    def equal(self, left, right):
        a, b = self.refs[left], self.refs[right]
        return a[1] == b[1] and all(x == y for x, y in zip(self.chunks(a), self.chunks(b), strict=True))

    def vehicle(self, identifier, due=True):
        if not self.initialized or type(identifier) is not int:
            raise ValueError('notification is not initialized')
        ref = self.refs.pop('vehicle_name')
        if not due:
            return
        if ref is None:
            raise ValueError('eligible notification name is missing')
        self.vehicles.append(self.chunks(ref), identifier, ref)

    def copy(self, name, key):
        with self.budget.open(name) as output:
            for chunk in self.chunks(self.refs[key]):
                output.write(chunk)

    def body(self):
        with self.budget.open('mail-body') as output:
            output.write(b'Odograph: log an odometer reading for ')
            for index, ref in enumerate(self.vehicles):
                if index:
                    output.write(b', ')
                for chunk in self.chunks(ref):
                    output.write(chunk)
            output.write(b' (vehicle).\n' if self.vehicles.count == 1 else b' (vehicles).\n')
            if self.refs['app_url'][1]:
                for chunk in self.chunks(self.refs['app_url']):
                    output.write(chunk)
                output.write(b'/settings\n')

    def prepare(self):
        if not self.initialized:
            raise ValueError('notification is not initialized')
        if not self.vehicles:
            return {'due_count': 0, 'artifacts': None}
        self.body()
        artifacts = self.mime('Odograph: log an odometer reading')
        return {'due_count':self.vehicles.count,'artifacts':artifacts}

    def mime(self, subject):
        from app.mime_preparation import checked_headers, prepare_text_mail
        sender,recipients = self.whole('email_from'),self.whole('email_to')
        header = checked_headers(sender,recipients,subject)
        # set_content encodes body only after header assignment, including its errors.
        ref = self.refs['app_url']
        if ref[1] and len(ref)==3:
            decoder = codecs.getincrementaldecoder('utf8')(errors=ref[2])
            for chunk in self.chunks(ref):
                decoder.decode(chunk).encode('utf8')
            decoder.decode(b'',final=True).encode('utf8')
        try:
            sender.encode('utf8')
            recipients.encode('utf8')
        except UnicodeEncodeError:
            surrogate_headers = True
        else:
            surrogate_headers = False
            del header,sender,recipients
            self.copy('mail-from','email_from')
            self.copy('mail-to','email_to')
        config = {key:self.whole('smtp_'+key) for key in ('host','username','password','security')}
        config.update(port=self.port,tls_insecure=self.tls_insecure)
        with self.budget.open('mail-config') as output:
            for chunk in json.JSONEncoder(ensure_ascii=True,separators=(',',':')).iterencode(config):
                for offset in range(0,len(chunk),4096):
                    output.write(chunk[offset:offset+4096].encode('ascii'))
        if surrogate_headers:
            return prepare_checked_mail(self.budget,header)
        return prepare_text_mail(self.budget,'mail-body','mail-from','mail-to','mail-config',subject)


    def close(self):
        self.vehicles.close()
        self.texts.close()



def prepare_checked_mail(budget,header,*,body_name='mail-body',config_name='mail-config'):
    # Surrogateescape headers can be valid unknown-8bit encoded words.
    # Reuse the same header hop and MIME codec without staging invalid UTF-8.
    from email.generator import BytesGenerator
    from email.parser import BytesParser
    from email import policy
    from app.mime_preparation import envelope, _json_file, normalize, file_chunks, write_mime
    header.set_payload('')
    with budget.open('mail-header') as output:
        BytesGenerator(output).flatten(header)
    with budget.open('mail-header','rb') as source:
        header = BytesParser(policy=policy.default).parse(source)
    sender,recipients,international = envelope(header)
    _json_file(budget,'mail-envelope',dict(sender=sender,recipients=recipients,international=international))
    with budget.open(body_name,'rb') as source,budget.open('mail-normalized') as output:
        normalize(file_chunks(source),output)
    for name,utf8 in (('mail-mime',False),('mail-mime-utf8',True)):
        with budget.open('mail-normalized','rb') as source,budget.open(name) as output:
            write_mime(source,header,output,utf8)
    for name in ('mail-header','mail-normalized'):
        budget.remove(name)
    return dict(mime='mail-mime',mime_utf8='mail-mime-utf8',envelope='mail-envelope',config=config_name)


def render(channel, budget):
    renderer = Renderer(budget)
    try:
        while (command := channel.recv_command()) is not None:
            kind = command['type']
            if kind == 'text':
                renderer.text(command['key'], command['size'], channel, command.get('encoding','utf8'),command.get('keep',True),command.get('literal',False))
            elif kind == 'initialize':
                channel.send_response(renderer.initialize(command))
            elif kind == 'quarter_bounds':
                channel.send_response(renderer.quarter_bounds(command['hour']))
            elif kind == 'email_bounds':
                channel.send_response(renderer.email_bounds(command['kind'],command['hour']))
            elif kind == 'history_bounds':
                channel.send_response(renderer.history_bounds())
            elif kind == 'weekly_count':
                renderer.weekly_count = command['count']
            elif kind == 'history_rows':
                renderer.history_rows(command['rows'])
            elif kind == 'rate':
                renderer.rate(command)
            elif kind == 'history_finish':
                channel.send_response(renderer.history_finish())
            elif kind == 'render_email':
                channel.send_response(renderer.prepare_email())
                return
            elif kind == 'preferences':
                channel.send_response({'current': renderer.equal('display_tz', 'current_display_tz')
                    and renderer.equal('email_to', 'current_email_to')
                    and (not command.get('filing',False) or renderer.equal('filing_mmdd','current_filing_mmdd'))})
            elif kind == 'capture':
                if not renderer.initialized:
                    raise ValueError('notification is not initialized')
                channel.send_response({'references':renderer.refs,'now':renderer.now.isoformat()})
                return
            elif kind == 'vehicle':
                renderer.vehicle(command['id'],command.get('due',True))
            elif kind == 'render':
                channel.send_response(renderer.prepare())
                return
            else:
                raise ValueError('unknown notification command')
    finally:
        renderer.close()
