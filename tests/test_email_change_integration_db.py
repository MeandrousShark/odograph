"""Schema-29 upgrade preservation and restricted-role email-change integration."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from psycopg import errors

from app import application_roles, db as db_module
from app import auth
from app.account_context import AccountPool, AccountPrincipal
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
from app.oidc_identities import resolve_identity_account
from app.tracking import authenticate_ingest, create_device
from conftest import LATEST_SCHEMA_VERSION, close_restricted_role_pools, drop_and_recreate_schema, full_schema_reset, restricted_role_pools
from tests.auth_db_fixtures import auth_config

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
    """Provision the schema-29 contract before later migrations."""
    owned_tables = application_roles.OWNED_TABLES
    control_tables = tuple(table for table in application_roles.CONTROL_TABLES
                           if table not in (
                               "oidc_attempts", "oidc_action_proofs", "account_security_audit",
                           ))
    functions = {
        function: owner
        for function, owner in application_roles.FUNCTIONS.items()
        if function not in application_roles.EMAIL_CHALLENGE_FUNCTIONS
        and function not in application_roles.PASSWORD_RESET_FUNCTIONS
        and function not in application_roles.OIDC_ATTEMPT_FUNCTIONS
        and function not in application_roles.OIDC_METHOD_FUNCTIONS
        and function not in application_roles.ACCOUNT_LIFECYCLE_FUNCTIONS
        and function not in application_roles.IMPORT_ADMISSION_FUNCTIONS
        and function != application_roles.INVITATION_FUNCTIONS[2]
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(application_roles, "OWNED_TABLES", owned_tables)
        patch.setattr(application_roles, "PROTECTED_TABLES", ())
        patch.setattr(
            application_roles,
            "TABLES",
            owned_tables + control_tables + application_roles.REFERENCE_TABLES,
        )
        patch.setattr(application_roles, "CONTROL_TABLES", control_tables)
        patch.setattr(application_roles, "FUNCTIONS", functions)
        patch.setattr(application_roles, "FUNCTION_FILES", application_roles.FUNCTION_FILES[:4])
        patch.setattr(application_roles, "INVITATION_FUNCTIONS", application_roles.INVITATION_FUNCTIONS[:2])
        patch.setattr(application_roles, "EMAIL_CHALLENGE_FUNCTIONS", ())
        patch.setattr(application_roles, "PASSWORD_RESET_FUNCTIONS", ())
        patch.setattr(application_roles, "OIDC_ATTEMPT_FUNCTIONS", ())
        patch.setattr(application_roles, "OIDC_METHOD_FUNCTIONS", ())
        patch.setattr(application_roles, "ACCOUNT_LIFECYCLE_FUNCTIONS", ())
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
            )).fetchone() == (LATEST_SCHEMA_VERSION,)
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


async def _schema_29_populated_upgrade(tmp_path):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    original_migrations_dir = db_module.MIGRATIONS_DIR
    account_id = 41
    issuer = "https://upgrade-idp.example"
    subject = "pre-037-stable-subject"
    try:
        await drop_and_recreate_schema(owner)
        db_module.MIGRATIONS_DIR = _migrations_through(tmp_path, 29)
        await run_migrations(owner)
        config = auth_config(TEST_DB, initial_admin_signup=False, dev_no_auth=False)

        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts(id,email,password_hash,is_admin) "
                "VALUES (%s,%s,%s,true)",
                (account_id, OLD_EMAIL, PASSWORD_HASH),
            )
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('accounts','id'), %s, true)",
                (account_id,),
            )
            await conn.execute(
                "INSERT INTO account_settings(account_id,display_tz,email_to) "
                "VALUES (%s,'America/Los_Angeles','upgrade-notices@example.invalid')",
                (account_id,),
            )
            await conn.execute(
                "INSERT INTO oidc_identities(account_id,issuer,subject,provider_email) "
                "VALUES (%s,%s,%s,'provider-contact@example.invalid')",
                (account_id, issuer, subject),
            )
            vehicle_id = (await (await conn.execute(
                "INSERT INTO vehicles(account_id,name,is_default) VALUES (%s,'Upgrade car',true) "
                "RETURNING id", (account_id,),
            )).fetchone())[0]
            trip_id = (await (await conn.execute(
                "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,notes) "
                "VALUES (%s,'upgrade-phone','manual','2026-08-01T10:00:00Z',"
                "'2026-08-01T11:00:00Z',4321,'schema-29-trip') RETURNING id", (account_id,),
            )).fetchone())[0]
            expense_id = (await (await conn.execute(
                "INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment,notes) "
                "VALUES (%s,%s,'2026-08-01','fuel',45.67,'business_use_allocated',"
                "'schema-29-expense') RETURNING id", (account_id, vehicle_id),
            )).fetchone())[0]

        await _provision_schema_29_roles()
        pools = await restricted_role_pools(owner)
        try:
            bound = AccountPool(pools.runtime, AccountPrincipal(account_id, True, 1))
            async with bound.connection() as conn:
                credential = await create_device(conn, "Upgrade phone")

            # Capture a real signed browser session at the old schema/version.
            from tests.test_admin_routes_db import _app, _client

            old_app = _app(pools, config=config)
            async with await _client(old_app) as old_client:
                assert (await old_client.post(f"/test/session/{account_id}/1")).status_code == 204
                old_cookie = old_client.cookies.get("session")
                assert old_cookie
        finally:
            await close_restricted_role_pools(owner)

        async with owner.connection() as conn:
            await conn.execute("GRANT UPDATE ON public.accounts TO odograph_control")
        db_module.MIGRATIONS_DIR = original_migrations_dir
        await run_migrations(owner)
        await prepare_application_roles(TEST_DB)

        async with owner.connection() as conn:
            assert await (await conn.execute("SELECT max(version) FROM schema_migrations")).fetchone() == (LATEST_SCHEMA_VERSION,)
            assert await (await conn.execute(
                "SELECT id,email,password_hash,is_admin,auth_version FROM accounts WHERE id=%s",
                (account_id,),
            )).fetchone() == (account_id, OLD_EMAIL, PASSWORD_HASH, True, 1)
            assert await (await conn.execute(
                "SELECT display_tz,email_to FROM account_settings WHERE account_id=%s",
                (account_id,),
            )).fetchone() == ("America/Los_Angeles", "upgrade-notices@example.invalid")
            assert await (await conn.execute(
                "SELECT issuer,subject,provider_email FROM oidc_identities WHERE account_id=%s",
                (account_id,),
            )).fetchone() == (issuer, subject, "provider-contact@example.invalid")
            assert await (await conn.execute(
                "SELECT account_id,device,source,distance_m,notes FROM trips WHERE id=%s", (trip_id,),
            )).fetchone() == (account_id, "upgrade-phone", "manual", 4321, "schema-29-trip")
            assert await (await conn.execute(
                "SELECT account_id,vehicle_id,amount::text,notes FROM expenses WHERE id=%s", (expense_id,),
            )).fetchone() == (account_id, vehicle_id, "45.67", "schema-29-expense")
            assert await (await conn.execute(
                "SELECT to_regclass('public.accounts_singleton_idx'), "
                "EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.accounts'::regclass "
                "AND conname='accounts_is_admin_check')",
            )).fetchone() == ("accounts_singleton_idx", True)
            with pytest.raises(errors.UniqueViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO accounts(email,password_hash,is_admin) "
                        "VALUES ('upgrade-second@example.invalid','other-hash',true)"
                    )
            with pytest.raises(errors.CheckViolation):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE accounts SET is_admin=false WHERE id=%s", (account_id,)
                    )

        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                linked = await resolve_identity_account(conn, issuer, subject)
                assert linked is not None and linked["id"] == account_id
                assert linked["email"] == OLD_EMAIL
            ingest = await authenticate_ingest(
                pools.control, credential.username, credential.secret,
                legacy_username="", legacy_password="",
            )
            assert ingest is not None
            assert ingest.account.account_id == account_id
            assert ingest.tracking_device_id == credential.tracking_device_id

            app = _app(pools, config=config)
            app.include_router(auth.make_router())
            async with await _client(app) as client:
                client.cookies.set("session", old_cookie, domain="testserver.local", path="/")
                response = await client.get("/settings/account")
                assert response.status_code == 200
                assert OLD_EMAIL in response.text

    finally:
        db_module.MIGRATIONS_DIR = original_migrations_dir
        await full_schema_reset(owner)
        await owner.close()


def test_populated_schema_29_upgrade_preserves_oidc_device_ledger_settings_session_and_singleton(tmp_path):
    asyncio.run(_schema_29_populated_upgrade(tmp_path))
