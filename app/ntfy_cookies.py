"""Incremental cookie records; new records inherit locked HTTP/1 and URL bounds."""
from __future__ import annotations

from http.cookiejar import Cookie
import json
import struct

FRAME_BYTES = 65520
HEADER = struct.Struct('!cI')
# httpcore reads 64 KiB after h11 accepted at most 100 KiB incomplete input.
# HTTPX allows 65536 URL codepoints, each at most twelve percent-encoded bytes.
# Cookie defaults use that normalized URL; supplied attributes/rest use headers.
# These conservative expansion factors also cover JSON escaping and field keys.
COOKIE_RECORD_BYTES = 32 * (100 * 1024 + 64 * 1024) + 16 * 12 * 65536 + 8192
FIELDS = ('version', 'name', 'value', 'port', 'port_specified', 'domain',
          'domain_specified', 'domain_initial_dot', 'path', 'path_specified',
          'secure', 'expires', 'discard', 'comment', 'comment_url', 'rfc2109')


def _value(value):
    if isinstance(value, str):
        yield b'"'
        for offset in range(0, len(value), 4096):
            yield json.dumps(value[offset:offset + 4096], ensure_ascii=False)[1:-1].encode('utf8', 'surrogatepass')
        yield b'"'
    else:
        yield json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8', 'surrogatepass')


def cookie_chunks(cookie):
    yield b'{'
    for index, field in enumerate(FIELDS):
        if index:
            yield b','
        yield ('"' + field + '":').encode('ascii')
        yield from _value(getattr(cookie, field))
    yield b',"rest":{'
    for index, (key, value) in enumerate(cookie._rest.items()):
        if index:
            yield b','
        yield from _value(key)
        yield b':'
        yield from _value(value)
    yield b'}}'


def cookies_in_place(jar):
    # CookieJar.__iter__ uses list(mapping.values()) at each level. Traverse
    # the already-held dictionaries without copying any complete bucket.
    for paths in jar._cookies.values():
        for names in paths.values():
            yield from names.values()


def restore_cookie(record):
    if not isinstance(record, dict) or set(record) != {*FIELDS, 'rest'}:
        raise ValueError('invalid cookie record')
    return Cookie(**record)


def apply_cookie(jar, kind, record):
    if kind == b'S':
        jar.set_cookie(restore_cookie(json.loads(record)))
    elif kind == b'X':
        values = json.loads(record)
        if not isinstance(values, list) or len(values) != 3 or not all(isinstance(x, str) for x in values):
            raise ValueError('invalid cookie deletion')
        try:
            jar.clear(*values)
        except KeyError:
            pass
    else:
        raise ValueError('invalid cookie event')
