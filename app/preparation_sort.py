"""Bounded external sorting of replay records and arbitrarily long UTF-8 keys."""
from __future__ import annotations

import functools
import json
import struct

CHUNK_BYTES = 65520
_RECORD = struct.Struct('!QQQQ21s')


class SortedRecords:
    """Two fixed-record merge files and one key/replay spool, never full keys."""

    def __init__(self, budget, name, *, numeric=False):
        self.budget, self.name, self.numeric = budget, name, numeric
        self.keys = budget.open(name + '-keys')
        self.data = budget.open(name + '-data')
        self.runs = budget.open(name + '-runs-a')
        self.batch = []
        self.count = 0
        self.finished = False

    def append(self, key_chunks, secondary, value):
        self.keys.seek(0, 2)
        key_offset = self.keys.tell()
        for chunk in key_chunks:
            for offset in range(0, len(chunk), CHUNK_BYTES):
                self.keys.write(chunk[offset:offset + CHUNK_BYTES])
        key_size = self.keys.tell() - key_offset
        data_offset = self.data.tell()
        payload = json.dumps(value, separators=(',', ':'), ensure_ascii=True, default=str).encode()
        if len(payload) > CHUNK_BYTES:
            raise ValueError('replay record exceeds frame limit')
        self.data.write(payload)
        self.batch.append((key_offset, key_size, data_offset, len(payload), str(secondary)))
        self.count += 1
        if len(self.batch) == 256:
            self._flush()

    def _compare(self, left, right):
        offset = 0
        while offset < min(left[1], right[1]):
            size = min(CHUNK_BYTES, min(left[1], right[1]) - offset)
            self.keys.seek(left[0] + offset)
            a = self.keys.read(size)
            self.keys.seek(right[0] + offset)
            b = self.keys.read(size)
            if a != b:
                return (a > b) - (a < b)
            offset += size
        if left[1] != right[1]:
            return (left[1] > right[1]) - (left[1] < right[1])
        a, b = (int(left[4]), int(right[4])) if self.numeric else (left[4], right[4])
        return (a > b) - (a < b)

    @staticmethod
    def _pack(record):
        secondary = record[4].encode('ascii')
        if len(secondary) > 21:
            raise ValueError('sort secondary key exceeds scalar limit')
        return _RECORD.pack(*record[:4], secondary.ljust(21, b'\0'))

    @staticmethod
    def _read(stream):
        raw = stream.read(_RECORD.size)
        if not raw:
            return None
        if len(raw) != _RECORD.size:
            raise ValueError('incomplete sort record')
        *values, secondary = _RECORD.unpack(raw)
        return (*values, secondary.rstrip(b'\0').decode('ascii'))

    def _flush(self):
        key = functools.cmp_to_key(self._compare)
        for record in sorted(self.batch, key=key):
            self.runs.write(self._pack(record))
        self.batch.clear()

    def finish(self):
        if self.finished:
            return self
        self._flush()
        self.keys.flush()
        self.data.flush()
        self.runs.close()
        source, target = self.name + '-runs-a', self.name + '-runs-b'
        width = 256
        while width < self.count:
            with self.budget.open(source, 'rb') as left, self.budget.open(source, 'rb') as right, self.budget.open(target) as output:
                for start in range(0, self.count, width * 2):
                    left.seek(start * _RECORD.size)
                    right.seek((start + width) * _RECORD.size)
                    na = min(width, self.count - start)
                    nb = max(0, min(width, self.count - start - width))
                    a = self._read(left) if na else None
                    b = self._read(right) if nb else None
                    while na or nb:
                        if not nb or (na and self._compare(a, b) <= 0):
                            output.write(self._pack(a))
                            na -= 1
                            a = self._read(left) if na else None
                        else:
                            output.write(self._pack(b))
                            nb -= 1
                            b = self._read(right) if nb else None
            self.budget.remove(source)
            source, target = target, source
            width *= 2
        self.sorted_name = source
        self.finished = True
        return self

    def __bool__(self):
        return bool(self.count)

    def __iter__(self):
        self.finish()
        with self.budget.open(self.sorted_name, 'rb') as records, self.budget.open(self.name + '-data', 'rb') as data:
            while record := self._read(records):
                data.seek(record[2])
                yield json.loads(data.read(record[3]))

    def close(self):
        self.keys.close()
        self.data.close()
        if not self.runs.closed:
            self.runs.close()
