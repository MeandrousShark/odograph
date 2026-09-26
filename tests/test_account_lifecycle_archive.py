"""Current-schema deletion state and authentication through real archives."""
from __future__ import annotations

import asyncio

import psycopg
import pytest

from app.account_lifecycle import cancel_account_deletion, request_account_deletion, purge_account
from app.accounts import create_admin
from app.application_roles import (
    application_role_pools,
    finalize_application_restore,
    prepare_application_restore,
    prepare_application_roles,
)
from app.db import make_pool, run_migrations
from app.oidc_attempts import _digest
from tests.auth_db_fixtures import auth_config
from tests.test_account_context_archive import (
    _Clusters, _assert_pg16_clients, _dump_archive, _restore_archive,
)
from tests.test_admin_routes_db import _app, _client

pytestmark = pytest.mark.ops


async def _seed_source(database_url):
    pool = make_pool(database_url)
    await pool.open(wait=True)
    try:
        await run_migrations(pool)
        await prepare_application_roles(database_url)
        async with application_role_pools(database_url) as roles:
            async with roles.control.connection() as conn:
                first = await create_admin(conn, "first@example.invalid", "first-hash")
            async with pool.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                ids = {}
                for email, password, admin in (
                    ("oidc@example.invalid", None, True),
                    ("dual@example.invalid", "dual-hash", False),
                    ("password@example.invalid", "password-hash", False),
                ):
                    ids[email] = (await (await conn.execute(
                        "INSERT INTO accounts(email,password_hash,is_admin) VALUES(%s,%s,%s) RETURNING id",
                        (email, password, admin),
                    )).fetchone())[0]
                    await conn.execute(
                        "INSERT INTO account_settings(account_id,display_tz) VALUES(%s,'UTC')",
                        (ids[email],),
                    )
                actor_id = ids["oidc@example.invalid"]
                for account_id, subject in (
                    (actor_id, "oidc-subject"), (ids["dual@example.invalid"], "dual-subject"),
                ):
                    await conn.execute(
                        "INSERT INTO oidc_identities(account_id,issuer,subject) VALUES(%s,%s,%s)",
                        (account_id, "https://provider.example", subject),
                    )
                await conn.execute(
                    "INSERT INTO oidc_action_proofs(browser_digest,account_id,auth_version,action,target,"
                    "created_at,expires_at) VALUES(%s,%s,1,'add_password','password',now(),now()+interval '5 minutes')",
                    ("a" * 64, actor_id),
                )
                await conn.execute(
                    "INSERT INTO oidc_attempts(state_digest,nonce_digest,browser_digest,action,account_id,"
                    "auth_version,target,created_at,expires_at) "
                    "VALUES(%s,%s,%s,'link',%s,1,'link',now(),now()+interval '5 minutes')",
                    ("b" * 64, "c" * 64, "d" * 64, actor_id),
                )
            actor = {"id": actor_id, "auth_version": 1, "is_admin": True, "is_enabled": True}
            async with roles.control.connection() as conn:
                assert await request_account_deletion(
                    conn, actor, first["id"], email=first["email"], acknowledge=True,
                ) == "scheduled"
            async with await _client(_app(roles, config=auth_config(database_url))) as client:
                await client.post(f"/test/session/{actor_id}/1")
                cookie = client.cookies.get("session")
                assert (await client.get("/admin/accounts")).status_code == 200
            return first, actor, cookie
    finally:
        await pool.close()


async def _purge_source(database_url, first, actor):
    pool = make_pool(database_url)
    await pool.open(wait=True)
    try:
        async with application_role_pools(database_url) as roles:
            async with roles.control.connection() as conn:
                assert (await (await conn.execute(
                    "SELECT public.sign_out_account_everywhere(%s,1)", (actor["id"],),
                )).fetchone())[0] is True
            actor = {**actor, "auth_version": 2}
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE accounts SET deletion_deadline=now()-interval '1 second' WHERE id=%s",
                    (first["id"],),
                )
                await conn.execute(
                    "INSERT INTO oidc_action_proofs(browser_digest,account_id,auth_version,action,target,"
                    "created_at,expires_at) VALUES(%s,%s,2,'purge_account',%s,now(),now()+interval '5 minutes')",
                    (_digest("archive-purge-proof"), actor["id"], str(first["id"])),
                )
            async with roles.control.connection() as conn:
                assert await purge_account(
                    conn, actor, first["id"], email=first["email"], confirm=True,
                    browser_nonce="archive-purge-proof",
                ) == "purged"
    finally:
        await pool.close()


def _restore_current_archive(target, archive):
    with psycopg.connect(target.database_url) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    asyncio.run(prepare_application_restore(target.database_url))
    result = _restore_archive(target, archive)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    asyncio.run(finalize_application_restore(target.database_url))


async def _assert_restored_state(database_url, first, actor, cookie, *, purged):
    async with application_role_pools(database_url) as roles:
        async with roles.control.connection() as conn:
            restored_version = (await (await conn.execute(
                "SELECT auth_version FROM accounts WHERE id=%s", (actor["id"],),
            )).fetchone())[0]
        async with await _client(_app(roles, config=auth_config(database_url))) as client:
            client.cookies.set("session", cookie, domain="testserver.local", path="/")
            assert (await client.get("/admin/accounts")).status_code == 303
            await client.post(f"/test/session/{actor['id']}/{restored_version}")
            assert (await client.get("/admin/accounts")).status_code == 200
        async with roles.control.connection() as conn:
            from psycopg.errors import UniqueViolation

            with pytest.raises(UniqueViolation):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT public.bootstrap_first_account('replacement@example.invalid','hash','UTC')"
                    )
        with psycopg.connect(database_url) as conn:
            state = conn.execute(
                "SELECT first_account_id,bootstrap_completed_at IS NOT NULL FROM instance_state"
            ).fetchone()
            assert state == (None if purged else first["id"], True)
            assert conn.execute(
                "SELECT count(*) FROM accounts WHERE id=%s", (first["id"],)
            ).fetchone() == (0 if purged else 1,)
            if not purged:
                assert conn.execute(
                    "SELECT is_enabled,deletion_deadline IS NOT NULL FROM accounts WHERE id=%s",
                    (first["id"],),
                ).fetchone() == (False, True)
                assert conn.execute(
                    "SELECT count(*) FROM vehicles WHERE account_id=%s", (first["id"],)
                ).fetchone() == (1,)
            method_shapes = conn.execute(
                "SELECT a.email,a.password_hash IS NOT NULL,EXISTS(SELECT 1 FROM oidc_identities i "
                "WHERE i.account_id=a.id) FROM accounts a WHERE a.id<>%s ORDER BY a.email",
                (first["id"],),
            ).fetchall()
            assert method_shapes == [
                ("dual@example.invalid", True, True),
                ("oidc@example.invalid", False, True),
                ("password@example.invalid", True, False),
            ]
            assert conn.execute("SELECT count(*) FROM oidc_action_proofs").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM oidc_attempts").fetchone() == (0,)
            assert conn.execute(
                "SELECT auth_version FROM accounts WHERE id=%s", (actor["id"],)
            ).fetchone()[0] > 2
            if purged:
                assert conn.execute(
                    "SELECT count(*) FROM account_security_audit WHERE target_account_id=%s "
                    "AND action='purge_account'", (first["id"],),
                ).fetchone() == (1,)
        if not purged:
            async with roles.control.connection() as conn:
                assert await cancel_account_deletion(
                    conn, {**actor, "auth_version": restored_version}, first["id"],
                ) == "cancelled"
            with psycopg.connect(database_url) as conn:
                assert conn.execute(
                    "SELECT is_enabled,deletion_deadline FROM accounts WHERE id=%s", (first["id"],),
                ).fetchone() == (True, None)


def test_real_archive_restores_deletion_state_without_reviving_sessions_or_signup(tmp_path):
    clusters = _Clusters()
    try:
        source = clusters.start()
        before_target = clusters.start()
        after_target = clusters.start()
        _assert_pg16_clients(source)
        first, actor, cookie = asyncio.run(_seed_source(source.database_url))
        before = tmp_path / "before-purge.dump"
        after = tmp_path / "after-purge.dump"
        result = _dump_archive(source, user="mileage", password="testpw", archive=before)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        asyncio.run(_purge_source(source.database_url, first, actor))
        result = _dump_archive(source, user="mileage", password="testpw", archive=after)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        for target, archive, purged in (
            (before_target, before, False), (after_target, after, True),
        ):
            _restore_current_archive(target, archive)
            asyncio.run(_assert_restored_state(
                target.database_url, first, actor, cookie, purged=purged,
            ))
    finally:
        clusters.close()
