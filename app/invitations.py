"""Restricted control operations for password-based member invitations."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Mapping

from psycopg import Error

from app.accounts import normalize_email, valid_email
from app.local_auth import hash_password

MIN_LOCAL_PASSWORD_LENGTH = 8


class InvitationUnavailable(ValueError):
    def __init__(self):
        super().__init__("Invitation unavailable.")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def issue_invitation(conn, admin_user: Mapping, email: str) -> str:
    """Return a new bearer token once; storage keeps only its digest."""
    if (not isinstance(admin_user, Mapping)
        or admin_user.get("is_admin") is not True
        or admin_user.get("is_enabled") is not True
        or type(admin_user.get("id")) is not int
        or not 1 <= admin_user["id"] <= 2**63 - 1):
        raise InvitationUnavailable()
    if not isinstance(email, str):
        raise InvitationUnavailable()
    target = normalize_email(email)
    if not valid_email(target) or "@" not in target:
        raise InvitationUnavailable()
    token = secrets.token_urlsafe(32)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT public.issue_member_invitation(%s,%s,%s)",
                (admin_user["id"], target, _digest(token)),
            )
    except Error:
        raise InvitationUnavailable() from None
    return token


async def redeem_invitation(conn, token: str, password: str, *, display_timezone: str = "UTC") -> int:
    """Provision a member and consume the invitation in one SQL statement."""
    if not isinstance(token, str) or not token.isascii() or len(token) > 256:
        raise InvitationUnavailable()
    if not isinstance(password, str) or len(password) < MIN_LOCAL_PASSWORD_LENGTH:
        raise InvitationUnavailable()
    password_hash = await asyncio.to_thread(hash_password, password)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.redeem_member_invitation(%s,%s,%s)",
                (_digest(token), password_hash, display_timezone),
            )
            return (await cur.fetchone())[0]
    except Error:
        raise InvitationUnavailable() from None
