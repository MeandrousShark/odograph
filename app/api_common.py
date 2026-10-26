"""Request rules shared by the API v1 resource routes: the years every date,
instant and year parameter accepts, idempotency keys on creates, and the
error responses documented for an operation."""
from __future__ import annotations

import re
import uuid

from app.api_errors import FIELD_INVALID, ApiError, ApiErrorEnvelope

# Dates and instants in these years survive conversion to UTC and to any
# display zone, so every one the API stores or queries renders.
MIN_YEAR, MAX_YEAR = 1900, 9998
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENT_REPLAYED_HEADER = "Idempotent-Replayed"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def year_in_range(year: int) -> bool:
    return MIN_YEAR <= year <= MAX_YEAR


def idempotency_key(value: str) -> uuid.UUID:
    """The create's `Idempotency-Key` header as a UUID, or `validation_failed`."""
    if not _UUID.fullmatch(value):
        raise ApiError("validation_failed", fields={IDEMPOTENCY_KEY_HEADER: FIELD_INVALID})
    return uuid.UUID(value)


def error_responses(*statuses: int) -> dict:
    """OpenAPI `responses` documenting the error envelope for each status."""
    return {status: {"model": ApiErrorEnvelope} for status in statuses}
