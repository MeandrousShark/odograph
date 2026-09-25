from __future__ import annotations

from psycopg.rows import dict_row

from app.accounts import create_admin, get_account


class IdentityLinkRejectedError(Exception):
    pass


def normalize_issuer(issuer: str) -> str:
    normalized = issuer.rstrip("/")
    if not normalized:
        raise ValueError("OIDC issuer is required")
    return normalized


def _validate_subject(subject: str) -> str:
    if not subject:
        raise ValueError("OIDC subject is required")
    return subject


async def identity_login_available(conn, issuer: str) -> bool:
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM oidc_identities i JOIN accounts a ON a.id = i.account_id "
        "WHERE i.issuer = %s AND a.is_enabled)",
        (normalize_issuer(issuer),),
    )
    return (await cur.fetchone())[0]


async def get_identity_for_account(conn, account_id: int, issuer: str) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, account_id, issuer, subject, provider_email, "
        "provider_display_name, linked_at, last_used_at "
        "FROM oidc_identities WHERE account_id = %s AND issuer = %s",
        (account_id, normalize_issuer(issuer)),
    )
    return await cur.fetchone()


async def resolve_identity_account(conn, issuer: str, subject: str) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT a.id, a.email, a.password_hash, a.is_admin, a.is_enabled, "
        "a.auth_version, a.created_at, a.updated_at "
        "FROM oidc_identities AS i "
        "JOIN accounts AS a ON a.id = i.account_id "
        "WHERE i.issuer = %s AND i.subject = %s AND a.is_enabled",
        (normalize_issuer(issuer), _validate_subject(subject)),
    )
    return await cur.fetchone()


async def create_identity_link(
    conn,
    account_id: int,
    issuer: str,
    subject: str,
    *,
    provider_email: str | None = None,
    provider_display_name: str | None = None,
    expected_auth_version: int | None = None,
) -> dict | None:
    normalized_issuer = normalize_issuer(issuer)
    exact_subject = _validate_subject(subject)
    if expected_auth_version is not None:
        cur = await conn.execute(
            "SELECT public.link_oidc_identity(%s,%s,%s,%s,%s,%s)",
            (account_id, expected_auth_version, normalized_issuer, exact_subject,
             provider_email, provider_display_name),
        )
        if not (await cur.fetchone())[0]:
            return None
        return await get_account(conn, account_id)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "INSERT INTO oidc_identities "
        "(account_id, issuer, subject, provider_email, provider_display_name) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING "
        "RETURNING id, account_id, issuer, subject, provider_email, "
        "provider_display_name, linked_at, last_used_at",
        (
            account_id,
            normalized_issuer,
            exact_subject,
            provider_email,
            provider_display_name,
        ),
    )
    identity = await cur.fetchone()
    if identity is not None:
        return identity
    return None


async def touch_identity_last_used(
    conn,
    issuer: str,
    subject: str,
    *,
    provider_email: str | None = None,
    provider_display_name: str | None = None,
) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "UPDATE oidc_identities SET provider_email = %s, "
        "provider_display_name = %s, last_used_at = now() "
        "WHERE issuer = %s AND subject = %s "
        "RETURNING id, account_id, issuer, subject, provider_email, "
        "provider_display_name, linked_at, last_used_at",
        (
            provider_email,
            provider_display_name,
            normalize_issuer(issuer),
            _validate_subject(subject),
        ),
    )
    return await cur.fetchone()


async def unlink_identity(
    conn, account_id: int, issuer: str, subject: str, *, expected_auth_version: int
) -> dict | None:
    cur = await conn.execute(
        "SELECT public.unlink_oidc_identity(%s,%s,%s,%s)",
        (account_id, expected_auth_version, normalize_issuer(issuer),
         _validate_subject(subject)),
    )
    if not (await cur.fetchone())[0]:
        return None
    return await get_account(conn, account_id)


async def establish_legacy_admin_identity(
    conn,
    *,
    email: str,
    password_hash: str,
    issuer: str,
    subject: str,
    display_timezone: str = "UTC",
    provider_email: str | None = None,
    provider_display_name: str | None = None,
) -> tuple[dict, dict]:
    async with conn.transaction():
        account = await create_admin(conn, email, password_hash, display_timezone=display_timezone)
        identity = await create_identity_link(
            conn,
            account["id"],
            issuer,
            subject,
            provider_email=provider_email,
            provider_display_name=provider_display_name,
        )
        if identity is None:
            raise IdentityLinkRejectedError()
    return account, identity
