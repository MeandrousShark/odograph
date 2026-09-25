"""Protected, single-use proof of a local account's login email."""
from __future__ import annotations

import hashlib
import re
import secrets

from app.accounts import get_account, normalize_email, valid_email

PURPOSE_CURRENT = "verify_current"
PURPOSE_CHANGE = "change_email"
PURPOSES = (PURPOSE_CURRENT, PURPOSE_CHANGE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _digest(token: str) -> str | None:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def issue_email_challenge(
    conn, account_id: int, expected_auth_version: int, purpose: str, target_email: str,
) -> str | None:
    if purpose not in PURPOSES or not isinstance(target_email, str) or not valid_email(normalize_email(target_email)):
        return None
    token = secrets.token_urlsafe(32)
    digest = _digest(token)
    cur = await conn.execute(
        "SELECT public.issue_email_challenge(%s,%s,%s,%s,%s)",
        (account_id, expected_auth_version, purpose, normalize_email(target_email), digest),
    )
    return token if (await cur.fetchone())[0] else None


async def revoke_email_challenge(conn, account_id: int, purpose: str, token: str) -> None:
    digest = _digest(token)
    if purpose not in PURPOSES or digest is None:
        return
    await conn.execute(
        "SELECT public.revoke_email_challenge(%s,%s,%s)",
        (account_id, purpose, digest),
    )


async def consume_email_challenge(
    conn, account_id: int, expected_auth_version: int, purpose: str, token: str,
) -> dict | None:
    digest = _digest(token)
    if purpose not in PURPOSES or digest is None:
        return None
    cur = await conn.execute(
        "SELECT public.consume_email_challenge(%s,%s,%s,%s)",
        (account_id, expected_auth_version, purpose, digest),
    )
    if not (await cur.fetchone())[0]:
        return None
    return await get_account(conn, account_id)


async def is_current_email_verified(conn, account_id: int) -> bool:
    cur = await conn.execute(
        "SELECT email_verified_at IS NOT NULL FROM public.accounts WHERE id = %s AND is_enabled",
        (account_id,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])
