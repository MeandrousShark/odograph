from __future__ import annotations

import logging

import pytest

from app.access_log import QueryStringRedactionFilter


@pytest.mark.parametrize(
    ("request_target", "expected_path"),
    [
        (
            "/auth/callback?code=synthetic-code&state=synthetic-state",
            "/auth/callback",
        ),
        ("/trips?filter=synthetic-value", "/trips"),
    ],
)
def test_access_log_redacts_query_and_preserves_request_fields(
    request_target: str, expected_path: str
):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.10", "GET", request_target, "1.1", 302),
        None,
    )

    assert QueryStringRedactionFilter().filter(record)
    assert record.getMessage() == (
        f'192.0.2.10 - "GET {expected_path} HTTP/1.1" 302'
    )
    assert "synthetic-code" not in record.getMessage()
    assert "synthetic-state" not in record.getMessage()


def test_access_log_without_query_is_unchanged():
    args = ("192.0.2.10", "GET", "/healthz", "1.1", 200)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, "%s %s %s %s %d", args, None
    )

    assert QueryStringRedactionFilter().filter(record)
    assert record.args is args
    assert record.getMessage() == "192.0.2.10 GET /healthz 1.1 200"
