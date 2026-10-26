"""Opaque keyset cursors for API list and queue endpoints.

A cursor is unpadded base64url over a fixed binary layout: format version,
cursor kind, the last row's `started_at` (microseconds since the Unix
epoch) and `id`, and a check value: a truncated SHA-256 over those fields
and the normalized filter set. The check is keyed by nothing secret, so a
cursor is not signed; it catches edits and reuse with other filters, and a
deliberately forged position still moves only within the caller's own
account-scoped rows. Anything that does not decode exactly or fails the
check is `invalid_cursor`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
from datetime import datetime, timedelta, timezone

from app.api_errors import ApiError

CURSOR_VERSION = 1
KINDS = {"trips": 1, "review": 2, "expenses": 3}
_HEADER = struct.Struct(">BBqq")
_LAYOUT = struct.Struct(">BBqq16s")
_TEXT = re.compile(r"[A-Za-z0-9_-]+")
_TEXT_LENGTH = len(base64.urlsafe_b64encode(bytes(_LAYOUT.size)).rstrip(b"="))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)
_MAX_ID = 2**63 - 1


def filter_digest(kind: str, filters: dict[str, str]) -> bytes:
    """Digest of the normalized filter values that drive the query."""
    canonical = json.dumps([kind, sorted(filters.items())], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).digest()


def _check(header: bytes, digest: bytes) -> bytes:
    return hashlib.sha256(header + digest).digest()[:16]


def encode_cursor(kind: str, position: tuple[datetime, int], digest: bytes) -> str:
    started_at, trip_id = position
    micros = (started_at - _EPOCH) // _MICROSECOND
    header = _HEADER.pack(CURSOR_VERSION, KINDS[kind], micros, trip_id)
    return base64.urlsafe_b64encode(header + _check(header, digest)).rstrip(b"=").decode("ascii")


def decode_cursor(kind: str, text: str, digest: bytes) -> tuple[datetime, int]:
    if len(text) != _TEXT_LENGTH or not _TEXT.fullmatch(text):
        raise ApiError("invalid_cursor")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ApiError("invalid_cursor") from None
    if len(raw) != _LAYOUT.size:
        raise ApiError("invalid_cursor")
    version, cursor_kind, micros, trip_id, check = _LAYOUT.unpack(raw)
    if (version != CURSOR_VERSION or cursor_kind != KINDS[kind]
            or check != _check(raw[:_HEADER.size], digest) or not 1 <= trip_id <= _MAX_ID):
        raise ApiError("invalid_cursor")
    try:
        started_at = _EPOCH + micros * _MICROSECOND
    except OverflowError:
        raise ApiError("invalid_cursor") from None
    return started_at, trip_id
