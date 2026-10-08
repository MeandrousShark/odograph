"""Charged text references and exact streaming export search normalization."""
from __future__ import annotations

import codecs
import struct

from psycopg.errors import DataError

from app.preparation import FRAME_BYTES
from app.ui._common import CATEGORIES, EXCLUSIONS, EXCLUSION_FILTER_NONE, _parse_vehicle_id, parse_date_range

RECORD = struct.Struct('!I')
SEARCH_SETTING = 'app.export_search_pattern'


class TextStore:
    def __init__(self, budget):
        self.budget = budget
        self.output = budget.open('export-text')
        self.refs = {}

    def receive(self, key, size, channel, encoding=None):
        if key not in ('display_tz','timezone_paths','category','from','to','vehicle','q','exclusion','rate1','rate2',
                'field:start_place_name','field:end_place_name','field:start_address','field:end_address',
                'field:vehicle_name','field:purpose','field:notes') or type(size) is not int or size<0:
            raise ValueError('invalid export text descriptor')
        if key == 'display_tz' and encoding == 'ascii':
            # Client SQL_ASCII returns bytes, which legacy ZoneInfo rejects.
            raise TypeError('timezone key must be str')
        decoder = codecs.getincrementaldecoder(encoding or 'utf8')()
        self.output.seek(0, 2)
        offset = self.output.tell()
        left = size
        while left:
            value = channel.recv_text()
            if not value or len(value)>FRAME_BYTES or len(value)>left:
                raise ValueError('invalid export text frame')
            decoder.decode(value)
            self.output.write(value)
            left -= len(value)
        decoder.decode(b'', final=True)
        self.output.flush()
        self.refs[key] = [offset,size] + ([encoding] if encoding else [])

    def chunks(self, ref):
        # Stored refs annotate the psycopg codec; literals use implicit UTF8.
        decoder = codecs.getincrementaldecoder(ref[2] if len(ref)==3 else 'utf8')()
        with self.budget.open('export-text','rb') as source:
            source.seek(ref[0])
            left = ref[1]
            while left:
                raw = source.read(min(left,16384))
                if not raw:
                    raise ValueError('incomplete export text spool')
                left -= len(raw)
                value = decoder.decode(raw)
                if value:
                    yield value
            final = decoder.decode(b'',final=True)
            if final:
                yield final

    def whole(self, key):
        return ''.join(self.chunks(self.refs[key]))

    def prefix(self, ref, limit=32767):
        parts = []
        for chunk in self.chunks(ref):
            parts.append(chunk[:limit])
            limit -= min(limit,len(chunk))
            if not limit:
                break
        return ''.join(parts)

    def choice(self, key, choices):
        ref = self.refs[key]
        if ref[1] > max(map(len,choices),default=0):
            return ''
        value = self.whole(key)
        return value if value in choices else ''

    def trimmed(self, ref):
        start = end = None
        position = 0
        for chunk in self.chunks(ref):
            if start is None:
                stripped = chunk.lstrip()
                if stripped:
                    start = position + len(chunk[:len(chunk)-len(stripped)].encode('utf8'))
            stripped = chunk.rstrip()
            if stripped:
                end = position + len(stripped.encode('utf8'))
            position += len(chunk.encode('utf8'))
        return [0,0] if start is None else [ref[0]+start,end-start]

    def close(self):
        self.output.close()


def pattern(store, ref, codec):
    trimmed = store.trimmed(ref)
    if not trimmed[1]:
        return {'search':False}
    count = size = 0
    with store.budget.open('export-pattern') as target:
        def record(text):
            nonlocal count,size
            if '\x00' in text:
                raise DataError('PostgreSQL text fields cannot contain NUL (0x00) bytes')
            raw = text.encode(codec)
            if not raw or len(raw)>FRAME_BYTES:
                raise ValueError('invalid export search record')
            target.write(RECORD.pack(len(raw)))
            target.write(raw)
            count += 1
            size += len(raw)
        record('%')
        for chunk in store.chunks(trimmed):
            for offset in range(0,len(chunk),4096):
                value = chunk[offset:offset+4096]
                record(value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_'))
        record('%')
    return {'search':True,'pattern_path':'export-pattern','pattern_records':count,'pattern_bytes':size}


def prepare_filters(store, tz, codec):
    from_dt,to_dt = parse_date_range(store.whole('from'),store.whole('to'),tz)
    vehicle = _parse_vehicle_id(store.whole('vehicle'))
    category = store.choice('category',CATEGORIES)
    exclusion = store.choice('exclusion',(*EXCLUSIONS,EXCLUSION_FILTER_NONE))
    return dict(category=category,exclusion=exclusion,vehicle=vehicle,
                from_dt=from_dt.isoformat() if from_dt is not None else None,
                to_dt=to_dt.isoformat() if to_dt is not None else None,
                **pattern(store,store.refs['q'],codec))
