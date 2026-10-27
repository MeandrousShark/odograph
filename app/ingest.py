"""OwnTracks and Traccar Client ingest endpoints.

OwnTracks contract summary: bad auth -> 401,
temporary authentication limits -> 503 with Retry-After before any body reads,
garbage -> 200-and-drop so OwnTracks never retry-loops a poison payload,
bodies over INGEST_MAX_BODY_BYTES -> 200-and-drop the same way (real
OwnTracks payloads are tiny, so an oversized body is either poison or
someone abusing the credentials -- never something worth a retry loop),
a message type outside STORED_MESSAGE_TYPES -> 200-and-drop with no
raw_messages row (OwnTracks' Publish Settings button, and a remote dump
command, send _type "dump" with a configuration object carrying the
tracker's plaintext username, password and URL; raw_messages exists only so
points can be rebuilt from scratch, see app/retention.py, never to hold a
credential),
genuine server errors -> 5xx so OwnTracks queues and redelivers.

That last rule is why the admission boundary matters: a database privilege
error raised while *checking* the credential is a rejected sender and gets
401, but the same error raised while *storing* an already-admitted fix is a
server fault, and answering 4xx there would make iOS drop a good payload.

POST /ingest/traccar takes a device credential in the `username` and `key`
query parameters, because Traccar Client cannot send Basic auth, and a form
body. Traccar retries every non-2xx answer forever, head of line, so every
poison message there is answered 200 with an empty body and dropped.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response
from psycopg.errors import InsufficientPrivilege, LockNotAvailable

from app.account_context import AccountPool
from app.capacity import await_completion
from app.storage import is_storage_capacity_error
from app.tracking import (
    IngestPrincipal, TrackingNotFound, TrackingStream, TrackingUnavailable,
    admit_ingest, authenticate_ingest, resolve_ingest_stream,
)
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

MAX_FUTURE_SKEW = timedelta(minutes=5)

# A firmware/device bug that reports tst in milliseconds instead of seconds
# (seen in practice) lands around year 55000, and datetime.fromtimestamp
# raises OSError or ValueError well before then depending on platform. Cap at
# a fixed, comfortably-future date instead of relying on that exception: no
# real OwnTracks fix will ever carry a timestamp past it.
MAX_TST = datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp()

# Only these _type values are stored in raw_messages; see the module
# docstring for why everything else is acknowledged and discarded instead.
STORED_MESSAGE_TYPES = frozenset({"location", "transition", "waypoint", "waypoints"})


class FailedAuthLimiter:
    """Bound password work and count failed Basic-auth attempts per IP.

    Only failures count toward the sliding window. Once blocked, an IP must
    wait for that window before attempting another expensive verification.
    The key table is bounded: when it is full, recording a failure for a new
    IP evicts the least recently failed key. A new IP is never blocked only
    because the table is full.
    """

    def __init__(
        self,
        max_failures: int,
        window_s: float,
        clock=time.monotonic,
        prune_interval_s: float | None = None,
        max_concurrent_auth: int = 2,
        max_keys: int = 16384,
    ):
        if max_concurrent_auth < 1:
            raise ValueError("max_concurrent_auth must be positive")
        if max_keys < 1 or max_failures < 1 or window_s <= 0:
            raise ValueError("failure limiter bounds must be positive")
        self.max_keys = max_keys
        self.max_failures = max_failures
        self.window_s = window_s
        self._clock = clock
        self._prune_interval_s = prune_interval_s or min(window_s, 60.0)
        self._next_global_prune = self._clock() + self._prune_interval_s
        self._failures: OrderedDict[str, deque] = OrderedDict()
        self._max_concurrent_auth = max_concurrent_auth
        self._auth_tasks: set[asyncio.Task] = set()

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
        ip = sha256(ip.encode()).hexdigest()
        now = self._clock()
        self._maybe_prune_all(now)
        self._prune(ip, now)
        return len(self._failures.get(ip, ())) >= self.max_failures

    def record_failure(self, ip: str) -> None:
        ip = sha256(ip.encode()).hexdigest()
        now = self._clock()
        self._maybe_prune_all(now)
        self._prune(ip, now)
        if ip not in self._failures:
            if len(self._failures) >= self.max_keys:
                self._failures.popitem(last=False)
            self._failures[ip] = deque(maxlen=self.max_failures)
        self._failures[ip].append(now)
        self._failures.move_to_end(ip)

    async def run_bounded(self, operation):
        """Keep an auth slot until work ends, even if its caller leaves."""
        if len(self._auth_tasks) >= self._max_concurrent_auth:
            raise _AuthSaturated
        task = asyncio.create_task(operation())
        self._auth_tasks.add(task)
        task.add_done_callback(self._auth_finished)
        return await self._wait_owned(task)

    async def authenticate(self, ip: str, *args, **kwargs):
        if len(self._auth_tasks) >= self._max_concurrent_auth:
            raise _AuthSaturated

        async def verify():
            credential = await authenticate_ingest(*args, **kwargs)
            if credential is None:
                self.record_failure(ip)
            return credential

        task = asyncio.create_task(verify())
        self._auth_tasks.add(task)
        task.add_done_callback(self._auth_finished)
        # Cancelling a request does not stop scrypt's thread. Keep its slot
        # occupied, and count any failure, until the verification really ends.
        return await self._wait_owned(task)

    async def _wait_owned(self, task):
        # Repeated cancellation must not drop the shared owner while a child
        # is still doing identity lookup or finishing its password verification.
        return await await_completion(task)

    def _auth_finished(self, task: asyncio.Task) -> None:
        self._auth_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even when the request left.


class _AuthSaturated(Exception):
    pass


def client_ip(request: Request) -> str:
    """Use only the trusted-proxy-normalized client address.

    Uvicorn sets it from FORWARDED_ALLOW_IPS; under `*`,
    app.forwarded replaces it with the rightmost X-Forwarded-For entry.
    """
    return request.client.host if request.client else "unknown"


def _authorization_header_bytes(request: Request) -> bytes:
    for name, value in request.scope.get("headers", ()):
        if name.lower() == b"authorization":
            return value
    return b""


def _basic_header_oversized(request: Request) -> bool:
    from app.capacity_routes import capacity_setting
    return len(_authorization_header_bytes(request)) > capacity_setting(
        request.app.state.capacity, "capacity_basic_header_max_bytes",
    )


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    if _basic_header_oversized(request):
        return None
    header = _authorization_header_bytes(request)
    if not header.startswith(b"Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, separator, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return (username, password) if separator else None


# Device usernames and secrets are far shorter; the cap only bounds lookup work.
QUERY_CREDENTIAL_MAX_CHARS = 256


def _query_oversized(request: Request) -> bool:
    from app.capacity_routes import capacity_setting
    return len(request.scope.get("query_string", b"")) > capacity_setting(
        request.app.state.capacity, "capacity_basic_header_max_bytes",
    )


def _query_credentials(request: Request) -> tuple[str, str] | None:
    """Exactly one non-blank `username` and `key`; other parameters are ignored."""
    if _query_oversized(request):
        return None
    try:
        pairs = parse_qsl(
            request.scope.get("query_string", b"").decode("ascii"),
            keep_blank_values=True, encoding="utf-8", errors="strict",
        )
    except (UnicodeDecodeError, ValueError):
        return None
    username = [value for name, value in pairs if name == "username"]
    key = [value for name, value in pairs if name == "key"]
    if len(username) != 1 or len(key) != 1:
        return None
    if not all(0 < len(value) <= QUERY_CREDENTIAL_MAX_CHARS for value in (username[0], key[0])):
        return None
    return username[0], key[0]


@dataclass(frozen=True, slots=True)
class CredentialSource:
    """Where an ingest route reads its credential before the body."""
    oversized: Callable[[Request], bool]
    credentials: Callable[[Request], tuple[str, str] | None]
    # A device-only route never lets a migrated legacy label select a stream.
    device_only: bool = False


BASIC_CREDENTIALS = CredentialSource(_basic_header_oversized, _basic_credentials)
QUERY_CREDENTIALS = CredentialSource(_query_oversized, _query_credentials, device_only=True)
INGEST_CREDENTIAL_SOURCES = {"/ingest": BASIC_CREDENTIALS, "/ingest/traccar": QUERY_CREDENTIALS}


def ingest_credential_source(path: str) -> CredentialSource:
    return INGEST_CREDENTIAL_SOURCES[path]


def _ok() -> Response:
    # OwnTracks expects a JSON array of messages for the device; always empty.
    return JSONResponse(content=[])


def _blocked_auth_response(limiter: FailedAuthLimiter) -> Response:
    return Response(
        status_code=503, headers={"Retry-After": str(max(1, math.ceil(limiter.window_s)))},
    )


def _bad_auth_response() -> Response:
    return Response(
        status_code=401, headers={"WWW-Authenticate": 'Basic realm="ingest"'},
    )


def _storage_busy_response(retry_after: int = 60) -> Response:
    # A phone must retain and retry a fix refused by capacity or lock pressure.
    return Response(status_code=503, headers={"Retry-After": str(retry_after)})


def preflight_authentication_request(
    request: Request, source: CredentialSource = BASIC_CREDENTIALS,
) -> Response | None:
    """Reject blocked and oversized requests without taking a queue ticket."""
    limiter: FailedAuthLimiter = request.app.state.ingest_limiter
    ip = client_ip(request)
    if limiter.blocked(ip):
        return _blocked_auth_response(limiter)
    if source.oversized(request):
        limiter.record_failure(ip)
        return _bad_auth_response()
    return None


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
    body = bytearray()
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


async def authenticate_request(
    request: Request, *, preflight: bool = True,
    source: CredentialSource = BASIC_CREDENTIALS,
):
    limiter: FailedAuthLimiter = request.app.state.ingest_limiter
    ip = client_ip(request)
    if limiter.blocked(ip):
        # This check also runs after an auth ticket wait, because another
        # request from the same IP may have exhausted the failure window.
        return _blocked_auth_response(limiter)
    if preflight and source.oversized(request):
        limiter.record_failure(ip)
        return _bad_auth_response()
    cfg = request.app.state.config
    presented = source.credentials(request)
    credential = None
    if presented is not None:
        try:
            credential = await limiter.authenticate(
                ip, request.app.state.control_pool, *presented,
                legacy_username=cfg.ingest_username, legacy_password=cfg.ingest_password,
            )
        except _AuthSaturated:
            return Response(status_code=503, headers={"Retry-After": "1"})
        except TrackingUnavailable:
            if source.device_only:
                # The pre-setup environment login is a correct legacy secret.
                return _bad_auth_response()
            return Response(status_code=503, headers={"Retry-After": "60"})
    if credential is None:
        if presented is None:
            limiter.record_failure(ip)
        return _bad_auth_response()
    if source.device_only and credential.kind != "device":
        # The secret was correct, so this is not a failed attempt.
        return _bad_auth_response()

    return credential


class _WriteState:
    __slots__ = ("admitted", "should_poke")

    def __init__(self):
        self.admitted = False
        self.should_poke = False


async def _admitted_write(request: Request, credential: IngestPrincipal, write) -> Response:
    """Run one ingest transaction and map its failures across admission."""
    pool = AccountPool(request.app.state.runtime_pool, credential.account)
    state = _WriteState()
    try:
        async with pool.connection() as conn:
            response = await write(conn, state)
    except (TrackingNotFound, InsufficientPrivilege):
        # Rotation, revocation and legacy-alias conversion may commit while
        # the body is in flight. Final admission must fail before storage.
        if state.admitted:
            raise
        return _bad_auth_response()
    except LockNotAvailable:
        return _storage_busy_response(1)
    except Exception as exc:
        if state.admitted and is_storage_capacity_error(exc):
            return _storage_busy_response()
        raise

    if state.should_poke:
        request.app.state.detector_scheduler.poke()
    return response


async def _store_raw(
    conn, credential: IngestPrincipal, stream: TrackingStream | None, serialized: str,
) -> bool:
    """Store a raw message with its replay receipt; False for an exact replay."""
    canonical = (await (await conn.execute(
        "SELECT %s::jsonb::text", (serialized,),
    )).fetchone())[0]
    namespace_kind = "device" if stream is not None else "credential"
    namespace_key = (
        str(stream.tracking_device_id) if stream is not None else credential.public_id
    )
    digest = sha256(canonical.encode("utf-8")).digest()
    cur = await conn.execute(
        "SELECT public.storage_find_replay(%s, %s, %s, %s, %s)",
        (credential.account.account_id, namespace_kind, namespace_key, digest, canonical),
    )
    if (await cur.fetchone())[0] is not None:
        return False
    cur = await conn.execute(
        "INSERT INTO raw_messages (account_id, tracking_device_id, payload) "
        "VALUES (%s, %s, %s) RETURNING id",
        (credential.account.account_id,
         stream.tracking_device_id if stream is not None else None, serialized),
    )
    raw_id = (await cur.fetchone())[0]
    await conn.execute(
        "SELECT public.storage_record_replay(%s, %s, %s, %s)",
        (credential.account.account_id, raw_id, namespace_kind, namespace_key),
    )
    return True


async def _insert_point(
    conn, credential: IngestPrincipal, stream: TrackingStream, device: str, fix: dict,
) -> None:
    """Insert a fix that passed _validate_location, in OwnTracks field names."""
    recorded_at = datetime.fromtimestamp(fix["tst"], tz=timezone.utc)
    await conn.execute(
        "INSERT INTO points (account_id, tracking_device_id, device, recorded_at, "
        "geom, accuracy_m, velocity_kmh, altitude_m, battery_pct, trigger) "
        "VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
        " %s, %s, %s, %s, %s) "
        "ON CONFLICT (tracking_device_id, recorded_at) DO NOTHING",
        (
            credential.account.account_id, stream.tracking_device_id, device,
            recorded_at, fix["lon"], fix["lat"], _num(fix.get("acc")),
            _num(fix.get("vel")), _num(fix.get("alt")),
            _int(fix.get("batt")), fix.get("t"),
        ),
    )


def make_router() -> APIRouter:
    from app.capacity_routes import AdmissionRoute

    router = APIRouter(route_class=AdmissionRoute)

    @router.post("/ingest")
    async def ingest(request: Request):
        credential = getattr(request.state, "_capacity_ingest_credential", None)
        if credential is None:
            credential = await authenticate_request(request)
        if isinstance(credential, Response):
            return credential
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
        except (ValueError, RecursionError):
            # ValueError covers JSONDecodeError, UnicodeDecodeError and the
            # integer digit limit; RecursionError covers deep nesting.
            log.warning("ingest: dropping unparseable body (%d bytes)", len(body))
            return _ok()
        if not isinstance(payload, dict):
            log.warning("ingest: dropping non-object payload")
            return _ok()

        serialized, poison_reason = _jsonb_encode(payload)
        if poison_reason:
            # Whole message dropped, not just the offending field: a device
            # sending an unstorable value like this is malfunctioning enough
            # that its other readings in the same fix aren't trustworthy
            # either, and raw_messages is meant to be the verbatim record of
            # an accepted message -- silently rewriting the payload to make it
            # storable would make that record a lie.
            log.warning("ingest: dropping payload before storage: %s", poison_reason)
            return _ok()

        location = payload.get("_type") == "location"
        reason = _validate_location(payload) if location else None
        label = str(payload.get("tid") or "default")

        async def write(conn, state: _WriteState) -> Response:
            stream = None
            if location and reason is None:
                stream = await resolve_ingest_stream(conn, credential, label)
            elif credential.tracking_device_id is not None:
                stream = TrackingStream(
                    credential.tracking_device_id, label, credential.device_generation,
                )
            await admit_ingest(
                conn, credential, stream,
                legacy_label=label if stream is not None and credential.kind == "legacy" else None,
            )
            # This sender is now admitted, so every later 42501 is a
            # server-side grant/policy fault rather than a rejected
            # credential -- and OwnTracks iOS deletes the payload it is
            # holding on any 4xx. Past this line those errors must reach
            # the 5xx handler so the phone keeps the fix and redelivers.
            state.admitted = True
            # OwnTracks' Publish Settings button (and a remote dump
            # command) sends _type "dump" whose configuration object
            # holds the tracker's plaintext username, password and URL.
            # Nothing outside STORED_MESSAGE_TYPES reaches raw_messages,
            # and only the type -- safely rendered and truncated, never
            # the payload -- reaches the log.
            msg_type = payload.get("_type")
            if not (isinstance(msg_type, str) and msg_type in STORED_MESSAGE_TYPES):
                log.info(
                    "ingest: discarding message type %s", _discarded_type_for_log(payload)
                )
                return _ok()
            if not await _store_raw(conn, credential, stream, serialized):
                # A lost response may leave detector work pending. Wake
                # it only after this admission transaction has closed.
                state.should_poke = True
            elif location and reason:
                log.info("ingest: dropping location payload: %s", reason)
            elif location:
                await _insert_point(conn, credential, stream, label, payload)
                state.should_poke = True
            return _ok()

        return await _admitted_write(request, credential, write)

    @router.post("/ingest/traccar")
    async def ingest_traccar(request: Request):
        credential = getattr(request.state, "_capacity_ingest_credential", None)
        if credential is None:
            credential = await authenticate_request(request, source=QUERY_CREDENTIALS)
        if isinstance(credential, Response):
            return credential
        if credential.kind != "device":
            return _bad_auth_response()
        cfg = request.app.state.config

        body = await _read_capped_body(request, cfg.ingest_max_body_bytes)
        if body is None:
            log.warning(
                "ingest: dropping oversized Traccar body (> %d bytes)", cfg.ingest_max_body_bytes
            )
            return _traccar_ok()
        fields, poison_reason = parse_traccar_form(body)
        if poison_reason:
            log.warning("ingest: dropping Traccar message: %s", poison_reason)
            return _traccar_ok()
        if "lat" not in fields and "lon" not in fields:
            # A heartbeat with no location has nothing to rebuild a point from.
            return _traccar_ok()
        payload = {**fields, "_source": TRACCAR_SOURCE}
        serialized, poison_reason = _jsonb_encode(payload)
        if poison_reason:
            log.warning("ingest: dropping Traccar message before storage: %s", poison_reason)
            return _traccar_ok()

        fix = traccar_fix(fields)
        reason = _validate_location(fix)
        device = traccar_device(fields)
        stream = TrackingStream(
            credential.tracking_device_id, device, credential.device_generation,
        )

        async def write(conn, state: _WriteState) -> Response:
            await admit_ingest(conn, credential, stream)
            state.admitted = True
            if not await _store_raw(conn, credential, stream, serialized):
                state.should_poke = True
            elif reason:
                log.info("ingest: dropping Traccar position: %s", reason)
            else:
                await _insert_point(conn, credential, stream, device, fix)
                state.should_poke = True
            return _traccar_ok()

        return await _admitted_write(request, credential, write)

    return router


TRACCAR_SOURCE = "traccar"
# Traccar Client sends at most eleven fields.
TRACCAR_MAX_FIELDS = 32
# Matches the tracking device name limit.
TRACCAR_DEVICE_MAX_CHARS = 100
KNOTS_TO_KMH = 1.852
# Plain or exponent notation, as Kotlin's Double.toString can produce;
# float() alone would also accept nan, inf, underscores and whitespace.
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_DECIMAL_MAX_CHARS = 64


def _traccar_ok() -> Response:
    # Traccar reads only the status, and retries any non-2xx forever.
    return Response(status_code=200)


def parse_traccar_form(body: bytes) -> tuple[dict[str, str], str | None]:
    """Returns (fields, None), or ({}, reason) when the message must be dropped."""
    try:
        pairs = parse_qsl(
            body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
            encoding="utf-8", errors="strict", max_num_fields=TRACCAR_MAX_FIELDS,
        )
    except UnicodeDecodeError:
        return {}, "body is not URL-encoded UTF-8"
    except ValueError:
        return {}, "unparseable or too many form fields"
    fields: dict[str, str] = {}
    for name, value in pairs:
        if name in fields:
            return {}, "repeated form field"
        if name.startswith("_"):
            # Reserved for stored metadata such as _source, and OwnTracks _type
            # must never appear on a Traccar row.
            return {}, "reserved form field name"
        fields[name] = value
    return fields, None


def traccar_number(value: str | None) -> float | None:
    if value is None or len(value) > _DECIMAL_MAX_CHARS or not _DECIMAL.fullmatch(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def traccar_fix(fields: dict[str, str]) -> dict:
    """Normalize Traccar strings into the OwnTracks fix shape, absent when unusable."""
    fix = {
        key: number for key, name in (
            ("lat", "lat"), ("lon", "lon"), ("tst", "timestamp"), ("acc", "accuracy"),
            ("alt", "altitude"), ("batt", "batt"),
        ) if (number := traccar_number(fields.get(name))) is not None
    }
    speed = traccar_number(fields.get("speed"))
    if speed is not None:
        fix["vel"] = speed * KNOTS_TO_KMH
    return fix


def traccar_device(fields: dict[str, str]) -> str:
    return fields.get("id", "")[:TRACCAR_DEVICE_MAX_CHARS] or "default"


# Optional fix readings outside these bounds are stored as NULL. The real
# columns hold about 3.4e38; these limits stay far inside that and far
# beyond any physical reading, so the INSERT cannot fail on them.
MAX_ABS_READING = 1e9
BATTERY_RANGE = (0, 100)


def _num(v) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        value = float(v)
    except OverflowError:
        return None
    if not math.isfinite(value) or abs(value) > MAX_ABS_READING:
        return None
    return value


def _int(v) -> int | None:
    value = _num(v)
    if value is None or not BATTERY_RANGE[0] <= value <= BATTERY_RANGE[1]:
        return None
    return int(value)


def _discarded_type_for_log(payload: dict) -> str:
    """Safe-for-logs rendering of a dropped message's _type -- enough to
    tell discarded messages apart in the log, never enough to leak one.

    A string is repr'd and truncated to 40 characters. Anything else is
    rendered by its JSON kind only (<dict>, <list>, <int>, <float>, <bool>,
    or <null>): a _type that is itself an object, as in a malformed
    dump-shaped payload, must never have its contents -- a field like a
    password -- reach the log through this rendering. A missing key logs as
    "missing", distinct from an explicit JSON null.
    """
    if "_type" not in payload:
        return "missing"
    value = payload["_type"]
    if isinstance(value, str):
        return repr(value[:40])
    if value is None:
        return "<null>"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, dict):
        return "<dict>"
    if isinstance(value, list):
        return "<list>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float>"
    return f"<{type(value).__name__}>"


def _jsonb_encode(payload: dict) -> tuple[str | None, str | None]:
    """Serializes payload for the raw_messages jsonb column.

    Returns (text, None) if safe to store, or (None, reason) if it must be
    dropped. jsonb is strict RFC 8259, but json.loads tolerates shapes that
    then raise from inside the open transaction on INSERT: bare NaN/Infinity
    tokens (json.dumps re-emits whatever json.loads accepted), and strings
    containing a NUL or an unpaired UTF-16 surrogate (both valid JSON, but
    jsonb's parser rejects the resulting escape). Checking up front, before
    any statement runs, avoids ever raising inside the transaction -- an
    after-the-fact catch would leave the transaction aborted and need
    savepoint handling to recover cleanly.
    """
    try:
        text = json.dumps(payload, allow_nan=False)
        reason = _unstorable_char_reason(payload)
    except ValueError:
        return None, "non-finite number in payload"
    except RecursionError:
        # json.loads accepts deeper nesting than these encoders can walk.
        return None, "payload nested too deeply"
    if reason:
        return None, reason
    return text, None


def _unstorable_char_reason(value) -> str | None:
    """Returns a drop reason if a string anywhere in value has a character
    jsonb's UTF-8 encoder refuses, or None if value is safe to store.

    A lone surrogate (U+D800-U+DFFF) can only exist here as an unpaired
    \\uD800-\\uDFFF escape: json.loads decodes a valid surrogate PAIR
    straight into the single astral codepoint it represents (an emoji
    escape pair becomes one character outside the surrogate range), so by
    the time this runs there is no pairing left to check -- any codepoint
    still in that range is unpaired by definition, and a blunt per-character
    range check is enough, no pair-matching logic required.
    """
    if isinstance(value, str):
        for ch in value:
            if ch == "\x00":
                return "NUL character in payload"
            if "\ud800" <= ch <= "\udfff":
                return "unpaired surrogate in payload"
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            reason = _unstorable_char_reason(k) or _unstorable_char_reason(v)
            if reason:
                return reason
        return None
    if isinstance(value, list):
        for v in value:
            reason = _unstorable_char_reason(v)
            if reason:
                return reason
        return None
    return None


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
    try:
        finite = math.isfinite(tst)
    except OverflowError:
        return "tst out of range"
    if not finite:
        return "non-finite tst"
    if tst <= 0 or tst > MAX_TST:
        return "tst out of range"
    if datetime.fromtimestamp(tst, tz=timezone.utc) > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
        return "tst in the future (device clock skew)"
    t = payload.get("t")
    if t is not None and not isinstance(t, str):
        return "non-string t"
    return None
