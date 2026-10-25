"""Revocable API grants and their opaque bearer tokens.

One grant per signed-in app installation. Tokens are `token_urlsafe(32)`
values that exist in plaintext only in the response that issues them; the
database stores SHA-256 digests and decides every outcome under its own row
locks (migration 048). Callers pass a control connection.

Nothing here logs, returns or raises with a token or digest. Grant events
log the grant's public id and a fixed reason.
"""
from __future__ import annotations

import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.accounts import _DISALLOWED_DISPLAY_NAME_CATEGORIES
from app.email_challenges import _digest

log = logging.getLogger(__name__)

# The database owns these lifetimes and the limit; the values are mirrored
# here for responses and tests.
ACCESS_TOKEN_SECONDS = 15 * 60
IDLE_EXPIRY_DAYS = 60
REFRESH_RETRY_SECONDS = 60
ACTIVE_GRANT_LIMIT = 10
DEVICE_LABEL_MAX_LENGTH = 64
DEFAULT_DEVICE_LABEL = "Mobile app"
_PUBLIC_ID_RE = re.compile(r"[A-Za-z0-9_-]{22}\Z")


class GrantLimitReached(Exception):
    """The account already holds the maximum number of active grants."""


@dataclass(frozen=True)
class IssuedTokens:
    grant_id: str
    access_expires_at: datetime
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


@dataclass(frozen=True)
class GrantPrincipal:
    account_id: int
    auth_version: int
    grant_id: str


def token_digest(token) -> str | None:
    return _digest(token)


def valid_public_id(value) -> bool:
    return isinstance(value, str) and _PUBLIC_ID_RE.fullmatch(value) is not None


def normalize_device_label(value) -> str:
    """Untrusted display text: NFC, without control, format, separator,
    surrogate or private-use characters, trimmed to 64 characters."""
    if not isinstance(value, str):
        return DEFAULT_DEVICE_LABEL
    # Strip first so a removed character cannot block composition.
    text = "".join(char for char in value
                   if unicodedata.category(char) not in _DISALLOWED_DISPLAY_NAME_CATEGORIES)
    text = unicodedata.normalize("NFC", text).strip()[:DEVICE_LABEL_MAX_LENGTH].strip()
    return unicodedata.normalize("NFC", text) or DEFAULT_DEVICE_LABEL


def format_instant(value: datetime) -> str:
    """RFC 3339 UTC with fractional seconds and `Z`."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_pair() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


async def create_grant(
    conn, account_id: int, auth_version: int, device_label, *, replaces: str | None = None,
) -> IssuedTokens | None:
    """Create a grant, revoking `replaces` when it names this account's active
    grant. Returns None when the account is unavailable or its version moved;
    raises GrantLimitReached at the limit, revoking nothing."""
    public_id = secrets.token_urlsafe(16)
    access, refresh = _new_pair()
    replaced = replaces if valid_public_id(replaces) else None
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result, result_access_expires_at FROM "
            "public.api_grant_create(%s,%s,%s,%s,%s,%s,%s)",
            (account_id, auth_version, public_id, normalize_device_label(device_label),
             token_digest(access), token_digest(refresh), replaced),
        )
        result, expires_at = await cur.fetchone()
    if result == "grant_limit_reached":
        log.info("api grant refused: limit reached")
        raise GrantLimitReached()
    if result != "created":
        return None
    log.info("api grant %s created", public_id)
    return IssuedTokens(public_id, expires_at, access, refresh)


@dataclass(frozen=True)
class RefreshOutcome:
    tokens: IssuedTokens | None
    reuse_detected: bool = False


async def refresh_grant(conn, refresh_token) -> RefreshOutcome:
    """Rotate, retry or detect reuse per the stored generations. Any outcome
    without tokens is an `invalid_grant` for the caller."""
    digest = token_digest(refresh_token)
    if digest is None:
        return RefreshOutcome(None)
    access, refresh = _new_pair()
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result, result_public_id, result_access_expires_at FROM "
            "public.api_grant_refresh(%s,%s,%s)",
            (digest, token_digest(access), token_digest(refresh)),
        )
        result, public_id, expires_at = await cur.fetchone()
    if result in ("rotated", "retried"):
        log.info("api grant %s refreshed: %s", public_id, result)
        return RefreshOutcome(IssuedTokens(public_id, expires_at, access, refresh))
    if result == "reuse_revoked":
        log.warning("api grant %s revoked: refresh token reuse", public_id)
        return RefreshOutcome(None, reuse_detected=True)
    return RefreshOutcome(None)


@dataclass(frozen=True)
class AccessCheck:
    principal: GrantPrincipal | None
    # The token belongs to a grant, current or previous generation, even
    # though it no longer authenticates: an outdated token, not a guess.
    known: bool


async def check_access_token(conn, access_token) -> AccessCheck:
    digest = token_digest(access_token)
    if digest is None:
        return AccessCheck(None, False)
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result, result_account_id, result_auth_version, result_public_id "
            "FROM public.api_grant_authenticate(%s)",
            (digest,),
        )
        result, account_id, auth_version, public_id = await cur.fetchone()
    if result == "valid":
        return AccessCheck(GrantPrincipal(account_id, auth_version, public_id), True)
    return AccessCheck(None, result == "invalid")


async def authenticate_access_token(conn, access_token) -> GrantPrincipal | None:
    return (await check_access_token(conn, access_token)).principal


async def revoke_grant(conn, account_id: int, public_id: str) -> bool:
    """Revoke one grant of this account. Idempotent; True only when this call
    revoked it."""
    if not valid_public_id(public_id):
        return False
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT public.api_grant_revoke(%s,%s)", (account_id, public_id))
        revoked = bool((await cur.fetchone())[0])
    if revoked:
        log.info("api grant %s revoked: owner request", public_id)
    return revoked
