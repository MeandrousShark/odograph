"""Schema-29 upgrade preservation and restricted-role email-change integration."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from psycopg import errors

from app import application_roles, db as db_module
from app.accounts import get_account_by_email
from app.application_roles import application_role_pools, prepare_application_roles
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.email_challenges import (
    PURPOSE_CHANGE,
    PURPOSE_CURRENT,
    consume_email_challenge,
    is_current_email_verified,
    issue_email_challenge,
)
from conftest import drop_and_recreate_schema, full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

ADMIN_ID = 41
OLD_EMAIL = "admin-before@example.invalid"
NEW_EMAIL = "admin-after@example.invalid"
PASSWORD_HASH = "preserved-admin-password-hash"


def _migrations_through(tmp_path: Path, version: int) -> Path:
    target = tmp_path / f"schema_{version}"
    target.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name.split("_", 1)[0]) <= version:
            shutil.copy(path, target / path.name)
    return target


async def _provision_schema_29_roles() -> None:
    """Provision the actual 029 contract before applying migration 030."""
    owned_tables = application_roles.OWNED_TABLES
    functions = {
        function: owner
        for function, owner in application_roles.FUNCTIONS.items()
        if function not in application_roles.EMAIL_CHALLENGE_FUNCTIONS
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(application_roles, "OWNED_TABLES", owned_tables)
        patch.setattr(application_roles, "PROTECTED_TABLES", ())
        patch.setattr(
            application_roles,
            "TABLES",
            owned_tables + application_roles.CONTROL_TABLES + application_roles.REFERENCE_TABLES,
        )
        patch.setattr(application_roles, "FUNCTIONS", functions)
        patch.setattr(application_roles, "EMAIL_CHALLENGE_FUNCTIONS", ())
        await prepare_application_roles(TEST_DB)


async def _schema_29_upgrade_and_email_change(tmp_path):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    original_migrations_dir = db_module.MIGRATIONS_DIR
    try:
        await drop_and_recreate_schema(owner)
        db_module.MIGRATIONS_DIR = _migrations_through(tmp_path, 29)
        await run_migrations(owner)

        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts (id,email,password_hash,is_admin) "
                "VALUES (%s,%s,%s,true)",
                (ADMIN_ID, OLD_EMAIL, PASSWORD_HASH),
            )
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('accounts','id'), %s, true)",
                (ADMIN_ID,),
            )
            await conn.execute(
                "INSERT INTO account_settings (account_id,display_tz,email_to) "
                "VALUES (%s,'America/Los_Angeles','notification@example.invalid')",
                (ADMIN_ID,),
            )

        await _provision_schema_29_roles()
        # Schema 29 granted control a table-wide UPDATE on accounts. Restore
        # that historical grant before migration 030 narrows it.
        async with owner.connection() as conn:
            await conn.execute("GRANT UPDATE ON public.accounts TO odograph_control")
            assert await (await conn.execute(
                "SELECT bool_or(privilege_type='UPDATE') FROM pg_class c, "
                "LATERAL aclexplode(c.relacl) acl JOIN pg_roles r ON r.oid=acl.grantee "
                "WHERE c.oid='public.accounts'::regclass AND r.rolname='odograph_control'"
            )).fetchone() == (True,)
        db_module.MIGRATIONS_DIR = original_migrations_dir
        await run_migrations(owner)
        await prepare_application_roles(TEST_DB)

        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT coalesce(bool_or(privilege_type='UPDATE'),false) FROM pg_class c, "
                "LATERAL aclexplode(c.relacl) acl JOIN pg_roles r ON r.oid=acl.grantee "
                "WHERE c.oid='public.accounts'::regclass AND r.rolname='odograph_control'"
            )).fetchone() == (False,)
            assert await (await conn.execute(
                "SELECT max(version) FROM schema_migrations"
            )).fetchone() == (30,)
            assert await (await conn.execute(
                "SELECT id,email,password_hash,is_admin,auth_version "
                "FROM accounts WHERE id=%s", (ADMIN_ID,),
            )).fetchone() == (ADMIN_ID, OLD_EMAIL, PASSWORD_HASH, True, 1)
            assert await (await conn.execute(
                "SELECT display_tz,email_to FROM account_settings WHERE account_id=%s",
                (ADMIN_ID,),
            )).fetchone() == ("America/Los_Angeles", "notification@example.invalid")
            assert await (await conn.execute(
                "SELECT to_regclass('public.accounts_singleton_idx'), "
                "EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.accounts'::regclass "
                "AND conname='accounts_is_admin_check')"
            )).fetchone() == ("accounts_singleton_idx", True)

            with pytest.raises(errors.UniqueViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO accounts (email,password_hash,is_admin) "
                        "VALUES ('second@example.invalid','other-hash',true)"
                    )
            with pytest.raises(errors.CheckViolation):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE accounts SET is_admin=false WHERE id=%s", (ADMIN_ID,)
                    )

        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE accounts SET email='bypass@example.invalid' WHERE id=%s",
                            (ADMIN_ID,),
                        )
                old_session_version = 1
                before = await get_account_by_email(conn, OLD_EMAIL)
                assert before is not None
                assert before["id"] == ADMIN_ID
                assert before["password_hash"] == PASSWORD_HASH
                assert await get_account_by_email(conn, NEW_EMAIL) is None
                assert not await is_current_email_verified(conn, ADMIN_ID)

                current_token = await issue_email_challenge(
                    conn, ADMIN_ID, old_session_version, PURPOSE_CURRENT, OLD_EMAIL,
                )
                assert current_token
                assert await get_account_by_email(conn, OLD_EMAIL) is not None
                assert await get_account_by_email(conn, NEW_EMAIL) is None
                verified = await consume_email_challenge(
                    conn, ADMIN_ID, old_session_version, PURPOSE_CURRENT, current_token,
                )
                assert verified is not None
                assert verified["email"] == OLD_EMAIL
                assert verified["auth_version"] == old_session_version
                assert await is_current_email_verified(conn, ADMIN_ID)

            async with owner.connection() as conn:
                await conn.execute(
                    "UPDATE email_challenges SET created_at=now()-interval '2 minutes' "
                    "WHERE account_id=%s", (ADMIN_ID,),
                )

            async with pools.control.connection() as conn:
                change_token = await issue_email_challenge(
                    conn, ADMIN_ID, old_session_version, PURPOSE_CHANGE, NEW_EMAIL,
                )
                assert change_token
                old_login = await get_account_by_email(conn, OLD_EMAIL)
                assert old_login is not None
                assert old_login["id"] == ADMIN_ID
                assert await get_account_by_email(conn, NEW_EMAIL) is None
                assert await is_current_email_verified(conn, ADMIN_ID)

                changed = await consume_email_challenge(
                    conn, ADMIN_ID, old_session_version, PURPOSE_CHANGE, change_token,
                )
                assert changed is not None
                assert changed["email"] == NEW_EMAIL
                assert changed["password_hash"] == PASSWORD_HASH
                assert changed["auth_version"] == old_session_version + 1
                assert changed["auth_version"] != old_session_version
                assert await get_account_by_email(conn, OLD_EMAIL) is None
                new_login = await get_account_by_email(conn, NEW_EMAIL)
                assert new_login is not None
                assert new_login["id"] == ADMIN_ID
                assert new_login["password_hash"] == PASSWORD_HASH
                assert await is_current_email_verified(conn, ADMIN_ID)

            async with owner.connection() as conn:
                assert await (await conn.execute(
                    "SELECT email_to FROM account_settings WHERE account_id=%s",
                    (ADMIN_ID,),
                )).fetchone() == ("notification@example.invalid",)
    finally:
        db_module.MIGRATIONS_DIR = original_migrations_dir
        await full_schema_reset(owner)
        await owner.close()


def test_schema_29_upgrade_preserves_admin_and_email_change_switches_login(tmp_path):
    asyncio.run(_schema_29_upgrade_and_email_change(tmp_path))
