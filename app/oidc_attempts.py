"""Protected, one-use OIDC browser transactions and action proofs."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from psycopg import Error
from psycopg.rows import dict_row


def _digest(value: str) -> str:
    if not isinstance(value, str) or not value.isascii() or not 1 <= len(value) <= 512:
        raise ValueError("invalid OIDC attempt value")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


async def start_oidc_attempt(
    conn, *, action: str, state: str, nonce: str, browser_nonce: str,
    account_id: int | None = None, auth_version: int | None = None,
    invite_token: str | None = None, proof_action: str | None = None,
    target: str | None = None,
) -> bool:
    try:
        invitation_digest = _digest(invite_token) if invite_token is not None else None
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.start_oidc_attempt(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (action, _digest(state), _digest(nonce), _digest(browser_nonce),
                 account_id, auth_version, invitation_digest, proof_action, target),
            )
            return (await cur.fetchone())[0]
    except (Error, ValueError):
        return False


async def consume_oidc_attempt(
    conn, *, action: str, state: str, nonce: str, browser_nonce: str,
    account_id: int | None = None, auth_version: int | None = None,
) -> dict | None:
    try:
        async with conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT * FROM public.consume_oidc_attempt(%s,%s,%s,%s,%s,%s)",
                (action, _digest(state), _digest(nonce), _digest(browser_nonce),
                 account_id, auth_version),
            )
            return await cur.fetchone()
    except (Error, ValueError):
        return None


async def finish_oidc_reauth(
    conn, *, state: str, nonce: str, browser_nonce: str,
    account_id: int, auth_version: int, issuer: str, subject: str,
    auth_time: datetime | int | float | None,
) -> bool:
    if isinstance(auth_time, (int, float)) and not isinstance(auth_time, bool):
        try:
            auth_time = datetime.fromtimestamp(auth_time, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return False
    if not isinstance(auth_time, datetime) or auth_time.tzinfo is None:
        return False
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.finish_oidc_reauth(%s,%s,%s,%s,%s,%s,%s,%s)",
                (_digest(state), _digest(nonce), _digest(browser_nonce), account_id,
                 auth_version, issuer, subject, auth_time),
            )
            return (await cur.fetchone())[0]
    except (Error, ValueError):
        return False


async def consume_action_proof(
    conn, *, account_id: int, auth_version: int, action: str,
    target: str, browser_nonce: str,
) -> bool:
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.consume_oidc_action_proof(%s,%s,%s,%s,%s)",
                (account_id, auth_version, action, target, _digest(browser_nonce)),
            )
            return (await cur.fetchone())[0]
    except (Error, ValueError):
        return False
