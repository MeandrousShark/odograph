from __future__ import annotations

from psycopg.rows import dict_row


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(email and email.isascii())


async def get_account(conn, account_id: int = 1) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at FROM accounts WHERE id = %s",
        (account_id,),
    )
    return await cur.fetchone()


async def get_sole_account(conn) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at FROM accounts ORDER BY id LIMIT 1"
    )
    return await cur.fetchone()


async def account_exists(conn) -> bool:
    cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM accounts)")
    return bool((await cur.fetchone())[0])


async def create_admin(conn, email: str, password_hash: str) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "INSERT INTO accounts (email, password_hash, is_admin) "
        "VALUES (%s, %s, true) "
        "RETURNING id, email, password_hash, is_admin, is_enabled, auth_version, "
        "created_at, updated_at",
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
    query += " RETURNING id, email, password_hash, is_admin, is_enabled, auth_version"
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(query, params)
    return await cur.fetchone()
