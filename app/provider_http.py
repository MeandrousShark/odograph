"""Whole-response network bounds shared by external providers."""
from __future__ import annotations

import asyncio
import json

import httpx

from app.capacity import current_owner, owned_thread

HTTP_DEADLINE_S = 15.0
SNAP_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
GEOCODE_RESPONSE_MAX_BYTES = 256 * 1024
NOTIFICATION_RESPONSE_MAX_BYTES = 64 * 1024


class ProviderResponseTooLarge(ValueError):
    pass


async def bounded_request(client, method, url, *, max_bytes, deadline_s=HTTP_DEADLINE_S,
                          raise_for_status=True, allowed_statuses=(), **kwargs):
    """Count decompressed chunks through completion, retaining socket timeouts."""
    try:
        async with asyncio.timeout(deadline_s):
            async with client.stream(method, url, **kwargs) as response:
                if raise_for_status and response.status_code not in allowed_statuses:
                    response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > max_bytes:
                        raise ProviderResponseTooLarge("provider response exceeds byte limit")
                    content.extend(chunk)
                return bytes(content)
    except TimeoutError as exc:
        raise httpx.TimeoutException("provider response deadline exceeded") from exc


async def bounded_json(client, method, url, *, max_bytes, deadline_s=HTTP_DEADLINE_S, **kwargs):
    content = await bounded_request(client, method, url, max_bytes=max_bytes,
                                    deadline_s=deadline_s, **kwargs)
    if current_owner() is not None:
        return await owned_thread(json.loads, content)
    # Direct library callers and offline probes have no serving admission.
    return json.loads(content)
