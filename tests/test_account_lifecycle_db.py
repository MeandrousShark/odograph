from __future__ import annotations

import asyncio
import os

import pytest
from psycopg import errors

from app.application_roles import application_role_pools, prepare_application_roles
from app.db import make_pool
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


async def _scenario(check):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        async with owner.connection() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
        await full_schema_reset(owner)
        await prepare_application_roles(TEST_DB)
        async with application_role_pools(TEST_DB) as pools:
            async with owner.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin) VALUES "
                    "('actor@example.invalid','hash',true),"
                    "('target@example.invalid',NULL,true),"
                    "('member@example.invalid','hash',false)"
                )
                rows = await (await conn.execute(
                    "SELECT id,email FROM accounts ORDER BY id"
                )).fetchall()
                ids = {email: account_id for account_id, email in rows}
                await conn.execute(
                    "INSERT INTO oidc_identities(account_id,issuer,subject) "
                    "VALUES(%s,'https://provider.example','target-subject')",
                    (ids["target@example.invalid"],),
                )
            await check(owner, pools, ids)
    finally:
        await full_schema_reset(owner)
        await owner.close()


def test_disable_revokes_every_proof_and_tracking_credential_and_reenable_revives_none():
    async def check(owner, pools, ids):
        actor = ids["actor@example.invalid"]
        target = ids["target@example.invalid"]
        digest = "a" * 64
        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO email_challenges(account_id,purpose,initiator,target_email,"
                "issued_email,issued_auth_version,token_digest,expires_at) "
                "VALUES(%s,'reset_password','admin','target@example.invalid',"
                "'target@example.invalid',1,%s,now()+interval '30 minutes')",
                (target, digest),
            )
            await conn.execute(
                "INSERT INTO oidc_attempts(state_digest,nonce_digest,browser_digest,"
                "action,account_id,auth_version,target,created_at,expires_at) "
                "VALUES(%s,%s,%s,'link',%s,1,'link',now(),now()+interval '5 minutes')",
                ("b" * 64, "c" * 64, "d" * 64, target),
            )
            await conn.execute(
                "INSERT INTO oidc_action_proofs(browser_digest,account_id,auth_version,"
                "action,target,created_at,expires_at) "
                "VALUES(%s,%s,1,'add_password','password',now(),now()+interval '5 minutes')",
                ("e" * 64, target),
            )
            await conn.execute(
                "INSERT INTO invitations(token_digest,email,issued_by,expires_at) "
                "VALUES(%s,'invitee@example.invalid',%s,now()+interval '48 hours')",
                ("f" * 64, target),
            )
            await conn.execute(
                "INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,"
                "account_id,kind) VALUES('credential','user','secret',%s,'legacy')",
                (target,),
            )
        async with pools.control.connection() as conn:
            row = await (await conn.execute(
                "SELECT public.admin_set_account_enabled(%s,1,%s,false)", (actor, target)
            )).fetchone()
            assert row == ("disabled",)
            assert await (await conn.execute(
                "SELECT public.admin_set_account_enabled(%s,1,%s,false)", (actor, target)
            )).fetchone() == ("already_disabled",)
            assert await (await conn.execute(
                "SELECT public.admin_set_account_enabled(%s,1,%s,true)", (actor, target)
            )).fetchone() == ("enabled",)
            rows = await (await conn.execute(
                "SELECT action,outcome FROM public.list_account_security_audit(%s,1)", (actor,)
            )).fetchall()
            assert rows == [
                ("enable_account", "enabled"),
                ("disable_account", "already_disabled"),
                ("disable_account", "disabled"),
            ]
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT is_enabled,auth_version FROM accounts WHERE id=%s", (target,)
            )).fetchone() == (True, 2)
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL FROM email_challenges WHERE account_id=%s", (target,)
            )).fetchone() == (True,)
            assert await (await conn.execute(
                "SELECT count(*) FROM oidc_attempts WHERE account_id=%s", (target,)
            )).fetchone() == (0,)
            assert await (await conn.execute(
                "SELECT count(*) FROM oidc_action_proofs WHERE account_id=%s", (target,)
            )).fetchone() == (0,)
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL FROM invitations WHERE issued_by=%s", (target,)
            )).fetchone() == (True,)
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL,generation FROM ingest_credentials "
                "WHERE account_id=%s", (target,)
            )).fetchone() == (True, 2)
            assert await (await conn.execute(
                "SELECT count(*) FROM oidc_identities WHERE account_id=%s", (target,)
            )).fetchone() == (1,)

    asyncio.run(_scenario(check))


def test_actor_proof_self_action_last_usable_admin_and_audit_privileges():
    async def check(owner, pools, ids):
        actor = ids["actor@example.invalid"]
        target = ids["target@example.invalid"]
        member = ids["member@example.invalid"]
        async with pools.control.connection() as conn:
            for attempted_actor, version, attempted_target in (
                (member, 1, target), (actor, 2, target), (actor, 1, actor)
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT public.admin_set_account_enabled(%s,%s,%s,false)",
                            (attempted_actor, version, attempted_target),
                        )
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT * FROM account_security_audit")
            for statement in (
                "UPDATE accounts SET is_enabled=false WHERE id=%s",
                "UPDATE accounts SET auth_version=auth_version+1 WHERE id=%s",
                "UPDATE accounts SET password_hash='bypass' WHERE id=%s",
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(statement, (target,))
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET password_hash=NULL WHERE id=%s", (actor,))
        async with pools.control.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT public.admin_set_account_enabled(%s,1,%s,false)", (actor, target)
                    )
            assert await (await conn.execute(
                "SELECT public.admin_set_account_enabled(%s,1,%s,false)", (target, actor)
            )).fetchone() == ("disabled",)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM account_security_audit"
            )).fetchone() == (1,)
            assert await (await conn.execute(
                "SELECT is_enabled FROM accounts WHERE id=%s", (target,)
            )).fetchone() == (True,)
        async with pools.runtime.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT public.admin_set_account_enabled(%s,1,%s,false)", (target, member)
                    )

    asyncio.run(_scenario(check))


def test_audit_pruning_is_bounded_and_keeps_account_ids_without_foreign_keys():
    async def check(owner, pools, ids):
        actor = ids["actor@example.invalid"]
        member = ids["member@example.invalid"]
        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO account_security_audit(occurred_at,actor_account_id,"
                "target_account_id,action,outcome) "
                "SELECT now()-interval '366 days',%s,%s,'disable_account','disabled' "
                "FROM generate_series(1,1005)", (actor, member),
            )
            await conn.execute(
                "INSERT INTO account_security_audit(actor_account_id,target_account_id,action,outcome) "
                "VALUES(%s,%s,'enable_account','enabled')", (actor, member),
            )
            await conn.execute("DELETE FROM accounts WHERE id=%s", (member,))
        async with pools.control.connection() as conn:
            assert await (await conn.execute(
                "SELECT public.prune_account_security_audit()"
            )).fetchone() == (1000,)
            rows = await (await conn.execute(
                "SELECT target_account_id FROM public.list_account_security_audit(%s,1)", (actor,)
            )).fetchall()
            assert rows and all(row == (member,) for row in rows)
            assert await (await conn.execute(
                "SELECT public.prune_account_security_audit()"
            )).fetchone() == (5,)
            assert await (await conn.execute(
                "SELECT public.prune_account_security_audit()"
            )).fetchone() == (0,)
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM account_security_audit"
            )).fetchone() == (1,)

    asyncio.run(_scenario(check))
