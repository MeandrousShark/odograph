"""Opaque keyset cursors: round trip, filter binding and tamper rejection."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.api_cursor import decode_cursor, encode_cursor, filter_digest
from app.api_errors import ApiError

AT = datetime(2026, 4, 14, 15, 0, 0, 123456, tzinfo=timezone.utc)
FILTERS = {"category": "business", "from": "", "to": "", "vehicle": "", "q": "", "exclusion": ""}


def _invalid(kind, text, digest):
    with pytest.raises(ApiError) as caught:
        decode_cursor(kind, text, digest)
    assert caught.value.code == "invalid_cursor"


def test_cursor_round_trips_microseconds_and_id():
    digest = filter_digest("trips", FILTERS)
    for position in ((AT, 1), (AT, 2**63 - 1), (datetime(1970, 1, 1, tzinfo=timezone.utc), 7),
                     (datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc), 8)):
        text = encode_cursor("trips", position, digest)
        assert set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert decode_cursor("trips", text, digest) == position
    local = AT.astimezone(timezone(timedelta(hours=-7)))
    assert decode_cursor("trips", encode_cursor("trips", (local, 3), digest), digest) == (AT, 3)


def test_digest_covers_kind_and_every_filter_value():
    digest = filter_digest("trips", FILTERS)
    assert filter_digest("trips", dict(reversed(FILTERS.items()))) == digest
    assert filter_digest("review", FILTERS) != digest
    for name in FILTERS:
        assert filter_digest("trips", {**FILTERS, name: "x"}) != digest, name


def test_cursor_rejects_other_filters_kinds_and_tampering():
    digest = filter_digest("trips", FILTERS)
    text = encode_cursor("trips", (AT, 42), digest)
    _invalid("trips", text, filter_digest("trips", {**FILTERS, "category": "personal"}))
    _invalid("review", text, digest)
    for index in range(len(text)):
        replacement = "A" if text[index] != "A" else "B"
        _invalid("trips", text[:index] + replacement + text[index + 1:], digest)
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    for forged in (b"\x02" + raw[1:], raw[:10] + bytes(8) + raw[18:]):
        _invalid("trips", base64.urlsafe_b64encode(forged).rstrip(b"=").decode(), digest)
    for junk in ("", "x", text + "A", text[:-1], text + "==", text.replace(text[0], "+"), "!" * len(text)):
        _invalid("trips", junk, digest)
