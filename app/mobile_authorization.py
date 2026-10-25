"""Mobile sign-in: the fixed client registry, pending authorization
requests, one-time codes and the PKCE check.

A browser starts a request at `/mobile/authorize`; its random id lives only
in a dedicated cookie, separate from the session so a login's
`session.clear()` cannot drop it. Approval issues a one-time code that the
app exchanges with its PKCE verifier for a grant (app.api_grants). The
database stores digests and decides every outcome (migration 049); callers
pass a control connection.

Nothing here logs, returns or raises with a code, verifier, challenge,
`state`, digest or cookie value.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

from app.account_context import control_connection
from app.api_grants import (
    ACTIVE_GRANT_LIMIT, IssuedTokens, create_grant, token_digest, valid_public_id,
)

log = logging.getLogger(__name__)

# One first-party client. A later client gets its own entry; there is no
# dynamic registration.
CLIENTS = {"odograph-ios": "odograph://auth/callback"}
AUTHORIZE_PATH = "/mobile/authorize"
CONFIRM_PATH = "/mobile/authorize/confirm"
COOKIE_NAME = "odograph_mobile_authorization"
# The database owns these; mirrored for responses and tests.
REQUEST_SECONDS = 10 * 60
CODE_SECONDS = 60
PENDING_REQUEST_CAP = 1000
STATE_MAX_LENGTH = 256
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
# Visible ASCII only, so the value can be echoed into the callback verbatim.
_STATE_RE = re.compile(r"[!-~]{1,%d}\Z" % STATE_MAX_LENGTH)
_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")


def mobile_signin_enabled(request) -> bool:
    return bool(getattr(request.app.state.config, "mobile_signin", False))


def registered_callback(client_id, redirect_uri) -> bool:
    return (isinstance(client_id, str) and isinstance(redirect_uri, str)
            and CLIENTS.get(client_id) == redirect_uri)


def valid_state(value) -> bool:
    return isinstance(value, str) and _STATE_RE.fullmatch(value) is not None


def valid_challenge(value) -> bool:
    return isinstance(value, str) and _CHALLENGE_RE.fullmatch(value) is not None


def pkce_matches(verifier, challenge) -> bool:
    """RFC 7636 S256: base64url(sha256(verifier)) without padding."""
    if not isinstance(verifier, str) or _VERIFIER_RE.fullmatch(verifier) is None:
        return False
    if not valid_challenge(challenge):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(computed.rstrip(b"="), challenge.encode("ascii"))


def callback_url(redirect_uri: str, **params: str) -> str:
    return f"{redirect_uri}?{urlencode(params)}"


@dataclass(frozen=True)
class StartedRequest:
    expires_at: datetime
    cookie_value: str = field(repr=False)


async def start_request(
    conn, *, previous_cookie, client_id: str, redirect_uri: str, code_challenge: str,
    state: str, device_label: str, replaces: str | None,
) -> StartedRequest | None:
    """Store a pending request, replacing the browser's previous one. None
    when the pending-request cap is reached."""
    value = secrets.token_urlsafe(32)
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT public.mobile_authorization_start(%s,%s,%s,%s,%s,%s,%s,%s)",
            (token_digest(value), token_digest(previous_cookie), client_id, redirect_uri,
             code_challenge, state, device_label, replaces if valid_public_id(replaces) else None),
        )
        expires_at = (await cur.fetchone())[0]
    if expires_at is None:
        log.warning("mobile authorization request refused: pending request cap reached")
        return None
    return StartedRequest(expires_at, value)


async def live_request(conn, cookie_value) -> datetime | None:
    digest = token_digest(cookie_value)
    if digest is None:
        return None
    async with conn.transaction():
        cur = await conn.execute("SELECT public.mobile_authorization_live(%s)", (digest,))
        return (await cur.fetchone())[0]


async def signed_in_destination(request, default: str = "/") -> str:
    """Where a successful browser login goes: the confirm page while mobile
    sign-in is on and this browser holds a live pending request, else
    `default`. Nothing in the URL selects it."""
    if not mobile_signin_enabled(request):
        return default
    value = request.cookies.get(COOKIE_NAME)
    if token_digest(value) is None:
        return default
    async with control_connection(request.app.state.control_pool) as conn:
        live = await live_request(conn, value)
    return CONFIRM_PATH if live is not None else default


@dataclass(frozen=True)
class ReplacedGrant:
    grant_id: str
    device_label: str
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class PendingReview:
    device_label: str
    expires_at: datetime
    active_grants: int
    replaces: ReplacedGrant | None
    at_limit: bool


async def review_request(conn, cookie_value, account_id: int, auth_version: int) -> PendingReview | None:
    """The live request as the signed-in account would approve it, or None."""
    digest = token_digest(cookie_value)
    if digest is None:
        return None
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result_device_label, result_expires_at, result_active_grants, result_replaces, "
            "result_replaces_label, result_replaces_created_at, result_replaces_last_used_at "
            "FROM public.mobile_authorization_review(%s,%s,%s)",
            (digest, account_id, auth_version),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    label, expires_at, active, replaced_id, replaced_label, replaced_created, replaced_used = row
    replaced = (ReplacedGrant(replaced_id, replaced_label, replaced_created, replaced_used)
                if replaced_id is not None else None)
    at_limit = active - (1 if replaced is not None else 0) >= ACTIVE_GRANT_LIMIT
    return PendingReview(label, expires_at, active, replaced, at_limit)


@dataclass(frozen=True)
class Completion:
    """How a finished request returns to the app: `code` on approval, else an
    `error`. Both carry the request's own `state`."""
    redirect_uri: str
    state: str = field(repr=False)
    code: str | None = field(default=None, repr=False)
    error: str | None = None

    def url(self) -> str:
        if self.code is not None:
            return callback_url(self.redirect_uri, code=self.code, state=self.state)
        return callback_url(self.redirect_uri, error=self.error, state=self.state)


async def approve_request(conn, cookie_value, account_id: int, auth_version: int) -> Completion | None:
    """End the request and issue a code, or report the limit. None when the
    request is missing or expired or the account changed."""
    digest = token_digest(cookie_value)
    if digest is None:
        return None
    code = secrets.token_urlsafe(32)
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result, result_redirect_uri, result_state "
            "FROM public.mobile_authorization_approve(%s,%s,%s,%s)",
            (digest, account_id, auth_version, token_digest(code)),
        )
        result, redirect_uri, state = await cur.fetchone()
    if result == "approved":
        log.info("mobile authorization approved")
        return Completion(redirect_uri, state, code=code)
    if result == "grant_limit_reached":
        log.info("mobile authorization refused: grant limit reached")
        return Completion(redirect_uri, state, error="grant_limit_reached")
    return None


async def deny_request(conn, cookie_value, account_id: int, auth_version: int) -> Completion | None:
    """End the request without a code. At the active-grant limit the error is
    `grant_limit_reached`, the only way such a request can end; otherwise
    `access_denied`. None when the request is missing or expired."""
    digest = token_digest(cookie_value)
    if digest is None:
        return None
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result, result_redirect_uri, result_state "
            "FROM public.mobile_authorization_deny(%s,%s,%s)",
            (digest, account_id, auth_version),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    result, redirect_uri, state = row
    log.info("mobile authorization ended without approval: %s", result)
    return Completion(redirect_uri, state, error=result)


async def exchange_code(conn, code, code_verifier) -> IssuedTokens | None:
    """Consume the code and create its grant in one transaction.

    None (an `invalid_grant`) for a consumed, expired or unknown code, a wrong
    verifier, or an account whose version moved since approval; the code stays
    consumed in each case. Raises GrantLimitReached after rolling back, so the
    code stays unconsumed and nothing is revoked.
    """
    digest = token_digest(code)
    if digest is None:
        return None
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT result_account_id, result_auth_version, result_code_challenge, "
            "result_device_label, result_replaces "
            "FROM public.mobile_authorization_code_consume(%s)",
            (digest,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        account_id, auth_version, challenge, label, replaces = row
        if not pkce_matches(code_verifier, challenge):
            log.info("mobile authorization code refused: verifier mismatch")
            return None
        return await create_grant(conn, account_id, auth_version, label, replaces=replaces)
