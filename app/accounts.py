from __future__ import annotations

import re

from psycopg.rows import dict_row


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(email and email.isascii())


_EMAIL_LOCAL_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z")
_EMAIL_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")


def safe_delivery_email(email: str) -> bool:
    """Accept one bounded mailbox, never a header or address list."""
    if not valid_email(email) or len(email) > 254 or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if not _EMAIL_LOCAL_RE.fullmatch(local):
        return False
    labels = domain.split(".")
    return bool(labels and all(len(label) <= 63 and _EMAIL_LABEL_RE.fullmatch(label) for label in labels))


async def get_account(conn, account_id: int) -> dict | None:
    # Runs on essentially every authenticated request via require_user, so
    # this must never select avatar_bytes -- see get_account_avatar for the
    # only query allowed to touch that column.
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at, avatar_mime, avatar_updated_at "
        "FROM accounts WHERE id = %s",
        (account_id,),
    )
    return await cur.fetchone()


async def get_account_by_email(conn, email: str) -> dict | None:
    """Identity lookup by normalized login email; never a personal-data owner fallback."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at, avatar_mime, avatar_updated_at "
        "FROM accounts WHERE email = %s",
        (normalize_email(email),),
    )
    return await cur.fetchone()


async def get_account_avatar(conn, account_id: int) -> dict | None:
    """The only query allowed to select avatar_bytes; used solely by the
    /account/avatar serving route, kept off the get_account hot path."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT avatar_bytes, avatar_mime, avatar_updated_at "
        "FROM accounts WHERE id = %s",
        (account_id,),
    )
    return await cur.fetchone()


async def set_account_avatar(
    conn, account_id: int, avatar_bytes: bytes, avatar_mime: str,
    *, expected_auth_version: int | None = None,
) -> dict | None:
    """Stores a newly uploaded avatar, replacing any previous one. Its
    result is fed straight into _account_user and _render_account (app/
    auth.py), which read avatar_mime/avatar_updated_at -- both must be
    RETURNING here, matching replace_password's shape, though (unlike
    replace_password) this never touches auth_version: an avatar change
    isn't an authentication change and doesn't sign out other sessions.
    """
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "UPDATE accounts SET avatar_bytes = %s, avatar_mime = %s, "
        "avatar_updated_at = now() WHERE id = %s "
        "AND (%s::bigint IS NULL OR (is_enabled AND auth_version = %s)) "
        "RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "avatar_mime, avatar_updated_at",
        (avatar_bytes, avatar_mime, account_id, expected_auth_version, expected_auth_version),
    )
    return await cur.fetchone()


async def clear_account_avatar(conn, account_id: int, *, expected_auth_version: int | None = None) -> dict | None:
    """Clears all three avatar columns together, as
    migrations/024_account_avatar.sql's all-or-nothing CHECK requires.
    Succeeds harmlessly (still returns the account row) whether or not an
    avatar was set.
    """
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "UPDATE accounts SET avatar_bytes = NULL, avatar_mime = NULL, "
        "avatar_updated_at = NULL WHERE id = %s "
        "AND (%s::bigint IS NULL OR (is_enabled AND auth_version = %s)) "
        "RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "avatar_mime, avatar_updated_at",
        (account_id, expected_auth_version, expected_auth_version),
    )
    return await cur.fetchone()


async def account_exists(conn) -> bool:
    cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM accounts)")
    return bool((await cur.fetchone())[0])


async def create_admin(
    conn, email: str, password_hash: str, *, display_timezone: str = "UTC"
) -> dict:
    """Create first-account identity and owned defaults in one guarded operation."""
    cur = await conn.execute(
        "SELECT public.bootstrap_first_account(%s, %s, %s)",
        (normalize_email(email), password_hash, display_timezone),
    )
    owner = (await cur.fetchone())[0]
    return await get_account(conn, owner)


async def replace_password(
    conn, account_id: int, password_hash: str, *, expected_auth_version: int | None = None
) -> dict | None:
    if expected_auth_version is None:
        account = await get_account(conn, account_id)
        if account is None:
            return None
        expected_auth_version = account["auth_version"]
    cur = await conn.execute(
        "SELECT public.replace_account_password(%s,%s,%s)",
        (account_id, expected_auth_version, password_hash),
    )
    if not (await cur.fetchone())[0]:
        return None
    return await get_account(conn, account_id)


async def sign_out_everywhere(
    conn, account_id: int, *, expected_auth_version: int,
) -> bool:
    cur = await conn.execute(
        "SELECT public.sign_out_account_everywhere(%s,%s)",
        (account_id, expected_auth_version),
    )
    return bool((await cur.fetchone())[0])
