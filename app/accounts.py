from __future__ import annotations

from psycopg.rows import dict_row


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(email and email.isascii())


async def get_account(conn, account_id: int = 1) -> dict | None:
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


async def get_sole_account(conn) -> dict | None:
    # Same hot-path constraint as get_account: avatar_mime/avatar_updated_at
    # only, never avatar_bytes.
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at, avatar_mime, avatar_updated_at "
        "FROM accounts ORDER BY id LIMIT 1"
    )
    return await cur.fetchone()


async def get_account_avatar(conn, account_id: int) -> dict | None:
    """The only query allowed to select avatar_bytes; used solely by the
    /account/avatar serving route, kept off the get_account/get_sole_account
    hot path."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT avatar_bytes, avatar_mime, avatar_updated_at "
        "FROM accounts WHERE id = %s",
        (account_id,),
    )
    return await cur.fetchone()


async def set_account_avatar(
    conn, account_id: int, avatar_bytes: bytes, avatar_mime: str
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
        "RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "avatar_mime, avatar_updated_at",
        (avatar_bytes, avatar_mime, account_id),
    )
    return await cur.fetchone()


async def clear_account_avatar(conn, account_id: int) -> dict | None:
    """Clears all three avatar columns together, as
    migrations/024_account_avatar.sql's all-or-nothing CHECK requires.
    Succeeds harmlessly (still returns the account row) whether or not an
    avatar was set.
    """
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "UPDATE accounts SET avatar_bytes = NULL, avatar_mime = NULL, "
        "avatar_updated_at = NULL WHERE id = %s "
        "RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "avatar_mime, avatar_updated_at",
        (account_id,),
    )
    return await cur.fetchone()


async def account_exists(conn) -> bool:
    cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM accounts)")
    return bool((await cur.fetchone())[0])


async def create_admin(conn, email: str, password_hash: str) -> dict:
    # Not a hot path (signup and legacy-OIDC admin establishment run once per
    # instance), so the row shape can match get_account/get_sole_account for
    # consistency rather than trimming avatar_mime/avatar_updated_at here too.
    # A freshly inserted account always has both NULL.
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "INSERT INTO accounts (email, password_hash, is_admin) "
        "VALUES (%s, %s, true) "
        "RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at, avatar_mime, avatar_updated_at",
        (normalize_email(email), password_hash),
    )
    return await cur.fetchone()


async def replace_password(
    conn, account_id: int, password_hash: str, *, expected_auth_version: int | None = None
) -> dict | None:
    query = (
        "UPDATE accounts SET password_hash = %s, auth_version = auth_version + 1, "
        "updated_at = now() WHERE id = %s"
    )
    params: tuple = (password_hash, account_id)
    if expected_auth_version is not None:
        query += " AND auth_version = %s"
        params += (expected_auth_version,)
    # Its result is fed straight into _account_user (app/auth.py), which now
    # reads avatar_mime/avatar_updated_at -- both must be RETURNING here or
    # that lookup KeyErrors.
    query += (
        " RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "avatar_mime, avatar_updated_at"
    )
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(query, params)
    return await cur.fetchone()
