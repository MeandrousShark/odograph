"""Owned tracking streams and one-time, revocable Basic credentials."""
from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import string
from dataclasses import dataclass, field

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from app.account_context import AccountPrincipal, account_id, control_connection
from app.db import TRACKING_PROVISION_LOCK_KEY
from app.local_auth import hash_password, verify_password

# Readable Basic usernames such as "work-iphone-7k3q". `public_id` stays
# opaque because rotate and revoke URLs use it.
_SLUG_DASH_RUN = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 20
_SLUG_FALLBACK = "device"
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
_SUFFIX_LEN = 4
_BASIC_USERNAME_CONSTRAINT = "ingest_credentials_basic_username_key"
_MAX_USERNAME_ATTEMPTS = 5


def slugify_label(label: str) -> str:
    """Lowercase [a-z0-9] runs joined by single dashes, capped, or "device"."""
    slug = _SLUG_DASH_RUN.sub("-", label.lower()).strip("-")
    if len(slug) > _SLUG_MAX_LEN:
        slug = slug[:_SLUG_MAX_LEN].rstrip("-")
    return slug or _SLUG_FALLBACK


def _random_suffix() -> str:
    """Module-level so tests can force a collision."""
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LEN))


def _new_basic_username(label: str) -> str:
    return f"{slugify_label(label)}-{_random_suffix()}"


class TrackingUnavailable(ValueError):
    """An authenticated sender has no account yet."""


class TrackingNotFound(ValueError):
    """The requested stream or credential is unavailable to this account."""


@dataclass(frozen=True, slots=True)
class IngestPrincipal:
    account: AccountPrincipal
    public_id: str
    generation: int
    kind: str
    tracking_device_id: int | None
    device_generation: int | None


@dataclass(frozen=True, slots=True)
class TrackingStream:
    tracking_device_id: int
    label: str
    generation: int


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    public_id: str
    username: str
    secret: str = field(repr=False)
    tracking_device_id: int


async def authenticate_ingest(
    control_pool, username: str, password: str, *, legacy_username: str,
    legacy_password: str,
) -> IngestPrincipal | None:
    """Resolve an account before reading the request body.

    Environment credentials only recognize a pre-setup sender. Once an account
    exists, the durable credential record is the sole authentication authority.
    """
    async with control_connection(control_pool) as conn:
        cur = await conn.execute(
            "SELECT c.public_id, c.secret_hash, c.account_id, c.tracking_device_id, "
            "c.kind, c.generation, c.revoked_at, a.is_enabled, a.auth_version, "
            "d.enabled, d.generation, d.revoked_at "
            "FROM ingest_credentials c JOIN accounts a ON a.id = c.account_id "
            "LEFT JOIN tracking_devices d ON d.account_id = c.account_id "
            "AND d.id = c.tracking_device_id WHERE c.basic_username = %s",
            (username,),
        )
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM accounts)")
            account_exists = (await cur.fetchone())[0]
            if not account_exists and legacy_username and legacy_password and (
                hmac.compare_digest(username.encode(), legacy_username.encode())
                & hmac.compare_digest(password.encode(), legacy_password.encode())
            ):
                raise TrackingUnavailable("Complete account setup before tracking")
            return None
    if row[6] is not None or not row[7]:
        return None
    if row[3] is not None and (not row[9] or row[11] is not None):
        return None
    if not await asyncio.to_thread(verify_password, password, row[1]):
        return None
    return IngestPrincipal(
        AccountPrincipal(row[2], row[7], row[8]), row[0], row[5], row[4], row[3], row[10],
    )


async def _provision_lock(conn) -> None:
    account_id(conn)
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s)", (TRACKING_PROVISION_LOCK_KEY,)
    )


async def admit_ingest(
    conn, credential: IngestPrincipal, stream: TrackingStream | None,
    *, legacy_label: str | None = None,
) -> None:
    if account_id(conn) != credential.account.account_id:
        raise TrackingNotFound("Tracking credential is unavailable")
    await conn.execute(
        "SELECT public.assert_tracking_credential(%s, %s, %s, %s, %s)",
        (
            credential.public_id, credential.generation,
            stream.tracking_device_id if stream is not None else None,
            stream.generation if stream is not None else None, legacy_label,
        ),
    )


async def resolve_ingest_stream(
    conn, credential: IngestPrincipal, label: str,
) -> TrackingStream:
    """A label can resolve only the migrated adapter's own alias map."""
    owner = account_id(conn)
    if owner != credential.account.account_id:
        raise TrackingNotFound("Tracking credential is unavailable")
    if credential.kind == "device":
        cur = await conn.execute(
            "SELECT id, label, generation FROM tracking_devices "
            "WHERE account_id = %s AND id = %s AND enabled AND revoked_at IS NULL",
            (owner, credential.tracking_device_id),
        )
        row = await cur.fetchone()
        if row is None or row[2] != credential.device_generation:
            raise TrackingNotFound("Tracking device is unavailable")
        return TrackingStream(*row)

    await _provision_lock(conn)
    # Check the adapter before provisioning. Final admission also checks the
    # alias/device and holds their row locks through the raw/point transaction.
    await admit_ingest(conn, credential, None)
    cur = await conn.execute(
        "SELECT d.id, d.label, d.generation, a.enabled, d.enabled, d.revoked_at "
        "FROM tracking_device_aliases a JOIN tracking_devices d "
        "ON d.account_id = a.account_id AND d.id = a.tracking_device_id "
        "WHERE a.account_id = %s AND a.original_label = %s",
        (owner, label),
    )
    row = await cur.fetchone()
    if row is not None:
        if not row[3] or not row[4] or row[5] is not None:
            raise TrackingNotFound("Tracking alias is unavailable")
        return TrackingStream(*row[:3])
    cur = await conn.execute(
        "INSERT INTO tracking_devices (account_id, label) VALUES (%s, %s) "
        "RETURNING id, label, generation", (owner, label),
    )
    stream = TrackingStream(*(await cur.fetchone()))
    await conn.execute(
        "INSERT INTO tracking_device_aliases "
        "(account_id, original_label, tracking_device_id) VALUES (%s, %s, %s)",
        (owner, label, stream.tracking_device_id),
    )
    await conn.execute(
        "INSERT INTO detector_state (account_id, tracking_device_id) VALUES (%s, %s)",
        (owner, stream.tracking_device_id),
    )
    return stream


async def _issue_credential(conn, tracking_device_id: int, label: str) -> IssuedCredential:
    """Issue a device credential, retrying a username collision.

    Each attempt runs in a savepoint. Usernames are unique across accounts,
    which RLS hides from a SELECT, so the insert itself is the check. When
    attempts run out, the caller's transaction rolls back the new device.
    """
    owner = account_id(conn)
    public_id = "odograph_" + secrets.token_urlsafe(18)
    secret = secrets.token_urlsafe(32)
    secret_hash = await asyncio.to_thread(hash_password, secret)
    for attempt in range(_MAX_USERNAME_ATTEMPTS):
        basic_username = _new_basic_username(label)
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO ingest_credentials "
                    "(public_id, basic_username, secret_hash, account_id, tracking_device_id, kind) "
                    "VALUES (%s, %s, %s, %s, %s, 'device')",
                    (public_id, basic_username, secret_hash, owner, tracking_device_id),
                )
            return IssuedCredential(public_id, basic_username, secret, tracking_device_id)
        except UniqueViolation as exc:
            if getattr(exc.diag, "constraint_name", None) != _BASIC_USERNAME_CONSTRAINT:
                raise
            if attempt == _MAX_USERNAME_ATTEMPTS - 1:
                raise ValueError("Could not create a tracking credential. Try again.") from exc
    raise AssertionError("unreachable: the loop above always returns or raises")


async def create_device(conn, label: str) -> IssuedCredential:
    owner = account_id(conn)
    label = label.strip()
    if not label or len(label) > 100 or any(ord(c) < 32 for c in label):
        raise ValueError("Use a device name between 1 and 100 characters")
    await _provision_lock(conn)
    cur = await conn.execute(
        "INSERT INTO tracking_devices (account_id, label) VALUES (%s, %s) RETURNING id",
        (owner, label),
    )
    device_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO detector_state (account_id, tracking_device_id) VALUES (%s, %s)",
        (owner, device_id),
    )
    return await _issue_credential(conn, device_id, label)


async def convert_legacy_device(conn, tracking_device_id: int) -> IssuedCredential:
    """Retire every legacy alias for this stream without moving its history."""
    owner = account_id(conn)
    await _provision_lock(conn)
    cur = await conn.execute(
        "SELECT label FROM tracking_devices WHERE account_id = %s AND id = %s "
        "AND enabled AND revoked_at IS NULL", (owner, tracking_device_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise TrackingNotFound("No such tracking device")
    label = row[0]
    cur = await conn.execute(
        "UPDATE tracking_device_aliases SET enabled = false "
        "WHERE account_id = %s AND tracking_device_id = %s AND enabled RETURNING original_label",
        (owner, tracking_device_id),
    )
    if not await cur.fetchall():
        raise TrackingNotFound("No active legacy aliases for this device")
    return await _issue_credential(conn, tracking_device_id, label)


async def rotate_credential(conn, public_id: str) -> IssuedCredential:
    owner = account_id(conn)
    await _provision_lock(conn)
    cur = await conn.execute(
        "SELECT basic_username, tracking_device_id FROM ingest_credentials "
        "WHERE account_id = %s AND public_id = %s AND kind = 'device' "
        "FOR UPDATE", (owner, public_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise TrackingNotFound("No such tracking credential")
    cur = await conn.execute(
        "SELECT id FROM tracking_devices WHERE account_id = %s AND id = %s "
        "AND enabled AND revoked_at IS NULL FOR SHARE", (owner, row[1]),
    )
    if await cur.fetchone() is None:
        raise TrackingNotFound("No such tracking device")
    secret = secrets.token_urlsafe(32)
    secret_hash = await asyncio.to_thread(hash_password, secret)
    await conn.execute(
        "UPDATE ingest_credentials SET secret_hash = %s, revoked_at = NULL, generation = generation + 1, "
        "updated_at = now() WHERE account_id = %s AND public_id = %s",
        (secret_hash, owner, public_id),
    )
    return IssuedCredential(public_id, row[0], secret, row[1])


async def revoke_credential(conn, public_id: str) -> None:
    owner = account_id(conn)
    await _provision_lock(conn)
    cur = await conn.execute(
        "UPDATE ingest_credentials SET revoked_at = COALESCE(revoked_at, now()), "
        "generation = generation + 1, updated_at = now() "
        "WHERE account_id = %s AND public_id = %s RETURNING public_id", (owner, public_id),
    )
    if await cur.fetchone() is None:
        raise TrackingNotFound("No such tracking credential")


async def list_tracking(conn) -> tuple[list[dict], list[dict]]:
    owner = account_id(conn)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT d.id, d.label, d.enabled, d.revoked_at, "
        "EXISTS (SELECT 1 FROM tracking_device_aliases a "
        "WHERE a.account_id = d.account_id AND a.tracking_device_id = d.id AND a.enabled) "
        "AS has_legacy_alias, max(p.received_at) AS last_received_at "
        "FROM tracking_devices d LEFT JOIN points p "
        "ON p.account_id = d.account_id AND p.tracking_device_id = d.id "
        "WHERE d.account_id = %s GROUP BY d.id ORDER BY d.created_at, d.id", (owner,),
    )
    devices = await cur.fetchall()
    await cur.execute(
        "SELECT public_id, basic_username, tracking_device_id, kind, revoked_at "
        "FROM ingest_credentials WHERE account_id = %s ORDER BY created_at, public_id", (owner,),
    )
    return devices, await cur.fetchall()
