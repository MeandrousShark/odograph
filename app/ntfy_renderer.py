"""NTFY scheduling, complete text and transport artifacts under helper authority."""
from __future__ import annotations

import codecs
from datetime import datetime, timedelta
import json
import zoneinfo

from app.preparation_sort import CHUNK_BYTES, SortedRecords
from app.nudge import latest_window_end
from app.odometer import latest_quarter_start

KEYS = {'display_tz', 'email_to', 'filing_mmdd', 'ntfy_topic', 'current_display_tz',
        'current_ntfy_topic', 'timezone_paths', 'app_url', 'url', 'token', 'username',
        'password', 'environment', 'vehicle_name'}


class Renderer:
    def __init__(self, budget):
        self.budget = budget
        self.texts = budget.open('ntfy-text')
        self.cookies = budget.open('ntfy-cookies')
        self.refs = {}
        self.vehicles = SortedRecords(budget, 'ntfy-vehicles', numeric=True)

    def text(self, command, channel):
        key, left = command['key'], command['size']
        keep, literal = command.get('keep', True), command.get('literal', False)
        if (key not in KEYS or type(left) is not int or left < 0 or type(keep) is not bool
                or type(literal) is not bool or (literal and command.get('encoding', 'utf8') != 'utf8')):
            raise ValueError('invalid ntfy text')
        self.texts.seek(0, 2)
        offset = self.texts.tell()
        errors = 'surrogatepass' if command.get('literal', False) else 'strict'
        decoder = codecs.getincrementaldecoder(command.get('encoding', 'utf8'))(errors=errors)
        while left:
            raw = channel.recv_text()
            if not raw or len(raw) > left:
                raise ValueError('invalid ntfy text frame')
            decoded = decoder.decode(raw)
            if keep:
                self.texts.write(decoded.encode('utf8', errors=errors))
            left -= len(raw)
        tail = decoder.decode(b'', final=True)
        if keep:
            self.texts.write(tail.encode('utf8', errors=errors))
        self.texts.flush()
        self.refs[key] = (offset, self.texts.tell() - offset, errors) if keep else None

    def chunks(self, ref):
        with self.budget.open('ntfy-text', 'rb') as source:
            source.seek(ref[0])
            left = ref[1]
            while left:
                raw = source.read(min(CHUNK_BYTES, left))
                if not raw:
                    raise ValueError('incomplete ntfy spool')
                yield raw
                left -= len(raw)

    def whole(self, key):
        ref = self.refs[key]
        return b''.join(self.chunks(ref)).decode('utf8', errors=ref[2])

    def equal(self, left, right):
        a, b = self.refs[left], self.refs[right]
        return a[1] == b[1] and all(x == y for x, y in zip(self.chunks(a), self.chunks(b), strict=True))

    def initialize(self, command):
        zoneinfo.reset_tzpath(json.loads(self.whole('timezone_paths')))
        tz = zoneinfo.ZoneInfo(self.whole('display_tz'))
        now = datetime.fromisoformat(command['now']) if command['now'] is not None else datetime.now(tz)
        self.kind = command['kind']
        if self.kind not in ('weekly', 'quarterly'):
            raise ValueError('unknown ntfy kind')
        now = now.astimezone(tz)
        enabled = command.get('enabled',True)
        if type(enabled) is not bool:
            raise ValueError('invalid ntfy enablement')
        if not enabled:
            return {'end':now.isoformat(),'start':now.isoformat()}
        end = latest_window_end(now, command['hour']) if self.kind == 'weekly' else latest_quarter_start(now, command['hour'])
        start = end - timedelta(days=7) if self.kind == 'weekly' else end
        return {'end':end.isoformat(),'start':start.isoformat()}

    def cookie(self, size, channel):
        while size:
            raw = channel.recv_text()
            if not raw or len(raw) > size:
                raise ValueError('invalid cookie spool')
            self.cookies.write(raw)
            size -= len(raw)
        self.cookies.write(b'\n')
        self.cookies.flush()

    def vehicle(self, command):
        ref = self.refs.pop('vehicle_name')
        if command['due']:
            self.vehicles.append(self.chunks(ref), command['id'], ref)

    def body(self, count):
        with self.budget.open('ntfy-body') as output:
            if self.kind == 'weekly':
                noun = 'trip' if count == 1 else 'trips'
                output.write(f'Odograph: {count} unclassified {noun} in the past week.'.encode('ascii'))
            else:
                output.write(b'Odograph: log an odometer reading for ')
                for index, ref in enumerate(self.vehicles):
                    if index:
                        output.write(b', ')
                    for raw in self.chunks(ref):
                        output.write(raw)
                output.write(b' (vehicle).' if count == 1 else b' (vehicles).')
            # Complete rstrip matches the old pure message helpers exactly.
            app_url = self.whole('app_url').rstrip('/')
            if app_url:
                output.write(b'\n')
                for offset in range(0, len(app_url), 16380):
                    output.write(app_url[offset:offset + 16380].encode('utf8'))
                output.write(b'/review' if self.kind == 'weekly' else b'/settings')

    def prepare(self, count):
        if self.kind != 'weekly':
            count = self.vehicles.count
        if not count:
            return {'count': 0, 'artifacts': None}
        self.body(count)
        config = {key: self.whole(key) for key in ('url', 'token', 'username', 'password')}
        config.update(topic=self.whole('ntfy_topic'), environment=json.loads(self.whole('environment')),
                      body_bytes=self.budget.verify('ntfy-body')[1])
        with self.budget.open('ntfy-config') as output:
            for raw in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(config):
                for offset in range(0, len(raw), 4096):
                    output.write(raw[offset:offset + 4096].encode('utf8', 'surrogatepass'))
        return {'count': count, 'artifacts': {'config': 'ntfy-config', 'body': 'ntfy-body', 'cookies': 'ntfy-cookies'}}

    def close(self):
        self.texts.close()
        self.cookies.close()
        self.vehicles.close()


def render(channel, budget):
    renderer = Renderer(budget)
    try:
        while (command := channel.recv_command()) is not None:
            kind = command['type']
            if kind == 'text':
                renderer.text(command, channel)
            elif kind == 'cookie':
                renderer.cookie(command['size'], channel)
            elif kind == 'initialize':
                channel.send_response(renderer.initialize(command))
            elif kind == 'preferences':
                channel.send_response({'current': renderer.equal('display_tz', 'current_display_tz')
                    and renderer.equal('ntfy_topic', 'current_ntfy_topic')})
            elif kind == 'vehicle':
                renderer.vehicle(command)
            elif kind == 'discard':
                channel.send_response({})
                return
            elif kind == 'count':
                channel.send_response({'count': renderer.vehicles.count})
            elif kind == 'render':
                channel.send_response(renderer.prepare(command.get('count', 0)))
                return
            else:
                raise ValueError('unknown ntfy command')
    finally:
        renderer.close()
