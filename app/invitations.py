"""Restricted control operations for member invitations."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Mapping
from contextlib import asynccontextmanager

from psycopg import Error

from app.accounts import normalize_email, valid_email
from app.local_auth import hash_password

MIN_LOCAL_PASSWORD_LENGTH = 8


class InvitationUnavailable(ValueError):
    def __init__(self):
        super().__init__("Invitation unavailable.")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _admin_identity(admin_user: Mapping) -> tuple[int, int]:
    if (not isinstance(admin_user, Mapping)
        or admin_user.get("is_admin") is not True
        or admin_user.get("is_enabled") is not True
        or type(admin_user.get("id")) is not int
        or not 1 <= admin_user["id"] <= 2**63 - 1
        or type(admin_user.get("auth_version")) is not int
        or not 0 <= admin_user["auth_version"] <= 2**63 - 1):
        raise InvitationUnavailable()
    return admin_user["id"], admin_user["auth_version"]


async def issue_invitation_record(conn, admin_user: Mapping, email: str) -> tuple[int, str]:
    """Return the nonsecret invitation ID and new bearer token once."""
    admin_id, auth_version = _admin_identity(admin_user)
    if not isinstance(email, str):
        raise InvitationUnavailable()
    target = normalize_email(email)
    if not valid_email(target) or "@" not in target:
        raise InvitationUnavailable()
    token = secrets.token_urlsafe(32)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.issue_member_invitation(%s,%s,%s,%s)",
                (admin_id, auth_version, target, _digest(token)),
            )
            invitation_id = (await cur.fetchone())[0]
    except Error:
        raise InvitationUnavailable() from None
    return invitation_id, token


async def issue_invitation(conn, admin_user: Mapping, email: str) -> str:
    """Return a new bearer token once; storage keeps only its digest."""
    _, token = await issue_invitation_record(conn, admin_user, email)
    return token


async def resend_invitation_record(
    conn, admin_user: Mapping, old_invitation_id: int,
) -> tuple[int, str, str]:
    """Rotate one still-outstanding invitation and return its fresh token once."""
    admin_id, auth_version = _admin_identity(admin_user)
    if type(old_invitation_id) is not int or not 1 <= old_invitation_id <= 2**63 - 1:
        raise InvitationUnavailable()
    token = secrets.token_urlsafe(32)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM public.resend_member_invitation(%s,%s,%s,%s)",
                (admin_id, auth_version, old_invitation_id, _digest(token)),
            )
            invitation_id, target_email = await cur.fetchone()
    except Error:
        raise InvitationUnavailable() from None
    return invitation_id, target_email, token


async def revoke_invitation(conn, admin_user: Mapping, invitation_id: int) -> None:
    admin_id, auth_version = _admin_identity(admin_user)
    if type(invitation_id) is not int or not 1 <= invitation_id <= 2**63 - 1:
        raise InvitationUnavailable()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT public.revoke_member_invitation(%s,%s,%s)",
                (admin_id, auth_version, invitation_id),
            )
    except Error:
        raise InvitationUnavailable() from None


async def list_invitations(conn, admin_user: Mapping) -> list[dict]:
    admin_id, auth_version = _admin_identity(admin_user)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM public.list_member_invitations(%s,%s)",
                (admin_id, auth_version),
            )
            columns = [column.name for column in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in await cur.fetchall()]
    except Error:
        raise InvitationUnavailable() from None


@asynccontextmanager
async def invitation_mail_admission(conn, admin_user: Mapping, invitation_id: int):
    """Hold the final authorization locks through non-waiting mail admission."""
    admin_id, auth_version = _admin_identity(admin_user)
    if type(invitation_id) is not int or not 1 <= invitation_id <= 2**63 - 1:
        raise InvitationUnavailable()
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.admit_member_invitation_send(%s,%s,%s)",
                (admin_id, auth_version, invitation_id),
            )
            target_email = (await cur.fetchone())[0]
            yield target_email
    except Error:
        raise InvitationUnavailable() from None


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


async def redeem_oidc_invitation_by_digest(
    conn, token_digest: str, issuer: str, subject: str, *,
    provider_email: str | None = None,
    provider_display_name: str | None = None,
    display_timezone: str = "UTC",
) -> int:
    """Provision an invited member after a validated OIDC callback."""
    if (not isinstance(token_digest, str) or len(token_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in token_digest)
        or not isinstance(issuer, str) or not issuer
        or not isinstance(subject, str) or not subject):
        raise InvitationUnavailable()
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT public.redeem_oidc_member_invitation(%s,%s,%s,%s,%s,%s)",
                (token_digest, issuer.rstrip("/"), subject, provider_email,
                 provider_display_name, display_timezone),
            )
            return (await cur.fetchone())[0]
    except Error:
        raise InvitationUnavailable() from None
