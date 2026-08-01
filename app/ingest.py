"""OwnTracks ingest endpoint.

Contract summary: bad auth -> 401 (429 once an IP trips the failure limiter),
garbage -> 200-and-drop so OwnTracks never retry-loops a poison payload,
bodies over INGEST_MAX_BODY_BYTES -> 200-and-drop the same way (real
OwnTracks payloads are tiny, so an oversized body is either poison or
someone abusing the credentials -- never something worth a retry loop),
genuine server errors -> 5xx so OwnTracks queues and redelivers.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, Response
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

MAX_FUTURE_SKEW = timedelta(minutes=5)


class FailedAuthLimiter:
    """Per-IP sliding-window counter for failed Basic-auth attempts.

    Only failures are counted; correct credentials are never throttled
    (a phone flushing an offline backlog is a legitimate burst).
    """

    def __init__(
        self,
        max_failures: int,
        window_s: float,
        clock=time.monotonic,
        prune_interval_s: float | None = None,
    ):
        self.max_failures = max_failures
        self.window_s = window_s
        self._clock = clock
        self._prune_interval_s = prune_interval_s or min(window_s, 60.0)
        self._next_global_prune = self._clock() + self._prune_interval_s
        self._failures: dict[str, deque] = defaultdict(deque)

    def _prune(self, ip: str, now: float) -> None:
        cutoff = now - self.window_s
        q = self._failures.get(ip)
        if q is None:
            return
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._failures.pop(ip, None)

    def _maybe_prune_all(self, now: float) -> None:
        if now < self._next_global_prune:
            return
        for ip in list(self._failures):
            self._prune(ip, now)
        self._next_global_prune = now + self._prune_interval_s

    def blocked(self, ip: str) -> bool:
        now = self._clock()
        self._maybe_prune_all(now)
        self._prune(ip, now)
        return len(self._failures.get(ip, ())) >= self.max_failures

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        self._maybe_prune_all(now)
        self._prune(ip, now)
        self._failures[ip].append(now)


def client_ip(request: Request) -> str:
    """Use only Uvicorn's trusted-proxy-normalized client address."""
    return request.client.host if request.client else "unknown"


def _check_basic_auth(request: Request) -> bool:
    cfg = request.app.state.config
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(username, cfg.ingest_username) & hmac.compare_digest(
        password, cfg.ingest_password
    )


def _ok() -> Response:
    # OwnTracks expects a JSON array of messages for the device; always empty.
    return JSONResponse(content=[])


async def _read_capped_body(request: Request, max_bytes: int) -> bytes | None:
    """Returns the request body, or None if it exceeds max_bytes.

    Checked against Content-Length first so a declared-oversized body is
    rejected without reading a single byte off the wire. Content-Length is
    absent for chunked transfer-encoding, so that alone isn't a complete
    guard -- the fallback accumulates via request.stream() and bails as soon
    as the running total exceeds the cap, rather than buffering an
    attacker-controlled stream to completion first.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.post("/ingest")
    async def ingest(request: Request):
        limiter: FailedAuthLimiter = request.app.state.ingest_limiter
        ip = client_ip(request)
        if not _check_basic_auth(request):
            if limiter.blocked(ip):
                return Response(status_code=429)
            limiter.record_failure(ip)
            return Response(
                status_code=401, headers={"WWW-Authenticate": 'Basic realm="ingest"'}
            )

        cfg = request.app.state.config
        body = await _read_capped_body(request, cfg.ingest_max_body_bytes)
        if body is None:
            log.warning(
                "ingest: dropping oversized body (> %d bytes)", cfg.ingest_max_body_bytes
            )
            return _ok()
        body = body.strip()
        if not body:
            return _ok()
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("ingest: dropping unparseable body (%d bytes)", len(body))
            return _ok()
        if not isinstance(payload, dict):
            log.warning("ingest: dropping non-object payload")
            return _ok()

        pool = request.app.state.pool
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO raw_messages (payload) VALUES (%s)", (json.dumps(payload),)
            )
            if payload.get("_type") != "location":
                return _ok()

            reason = _validate_location(payload)
            if reason:
                log.info("ingest: dropping location payload: %s", reason)
                return _ok()

            device = str(payload.get("tid") or "default")
            recorded_at = datetime.fromtimestamp(payload["tst"], tz=timezone.utc)
            await conn.execute(
                "INSERT INTO points (device, recorded_at, geom, accuracy_m, velocity_kmh,"
                " altitude_m, battery_pct, trigger) "
                "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
                " %s, %s, %s, %s, %s) "
                "ON CONFLICT (device, recorded_at) DO NOTHING",
                (
                    device,
                    recorded_at,
                    payload["lon"],
                    payload["lat"],
                    _num(payload.get("acc")),
                    _num(payload.get("vel")),
                    _num(payload.get("alt")),
                    _int(payload.get("batt")),
                    payload.get("t"),
                ),
            )

        request.app.state.detector_scheduler.poke()
        return _ok()

    return router


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _validate_location(payload: dict) -> str | None:
    """Returns a drop reason, or None if the payload is a usable fix."""
    lat, lon, tst = payload.get("lat"), payload.get("lon"), payload.get("tst")
    for name, v in (("lat", lat), ("lon", lon), ("tst", tst)):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return f"missing/non-numeric {name}"
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return "lat/lon out of range"
    if lat == 0 and lon == 0:
        return "null island (0,0)"
    if tst <= 0:
        return "non-positive tst"
    if datetime.fromtimestamp(tst, tz=timezone.utc) > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
        return "tst in the future (device clock skew)"
    return None
