"""Ordered account disablement barriers through the restricted roles."""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from psycopg import errors

from app.account_context import AccountPool, AccountPrincipal
from app.account_workers import enabled_principals
from app.accounts import create_admin
from app.application_roles import application_role_pools, prepare_application_roles
from app.db import make_pool
from app.email_challenges import (
    PURPOSE_CURRENT, consume_email_challenge, email_challenge_send_usable,
    issue_email_challenge,
)
from app.geocode import GeocodeWorker
from app.invitations import (
    InvitationUnavailable, invitation_mail_admission, issue_invitation_record,
    redeem_invitation,
)
from app.nudge import NudgeWorker
from app.password_reset import (
    INITIATOR_PUBLIC, consume_password_reset, issue_password_reset,
    password_reset_send_usable,
)
from app.tracking import authenticate_ingest, create_device
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        async with owner.connection() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS odograph_service CASCADE")
        await full_schema_reset(owner)
        await prepare_application_roles(TEST_DB)
        async with application_role_pools(TEST_DB) as pools:
            async with pools.control.connection() as conn:
                actor = await create_admin(conn, "actor@example.invalid", "actor-hash")
            async with owner.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_is_admin_check")
                row = await (await conn.execute(
                    "INSERT INTO accounts(email,password_hash,is_admin,email_verified_at) "
                    "VALUES ('target@example.invalid','target-hash',true,now()) "
                    "RETURNING id",
                )).fetchone()
                target_id = row[0]
                await conn.execute(
                    "INSERT INTO account_settings(account_id) VALUES (%s)", (target_id,),
                )
            await callback(owner, pools, actor["id"], target_id)
    finally:
        await owner.close()


async def _set_enabled(pools, actor_id, actor_version, target_id, enabled):
    async with pools.control.connection() as conn:
        row = await (await conn.execute(
            "SELECT public.admin_set_account_enabled(%s,%s,%s,%s)",
            (actor_id, actor_version, target_id, enabled),
        )).fetchone()
        return row[0]


async def _wait_for_lock(owner, pid, task):
    async def observe():
        while True:
            async with owner.connection() as conn:
                row = await (await conn.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,),
                )).fetchone()
            if row == ("Lock",):
                return
            assert not task.done(), "disablement bypassed the account lock"
            await asyncio.sleep(0.01)
    await asyncio.wait_for(observe(), 5)


def test_disable_waits_for_admitted_account_mutation_and_rejects_next_admission():
    async def check(owner, pools, actor_id, target_id):
        target = AccountPool(pools.runtime, AccountPrincipal(target_id, True, 1))
        async with target.connection() as active:
            await active.execute(
                "UPDATE account_settings SET ntfy_topic='committed' WHERE account_id=%s",
                (target_id,),
            )
            started = asyncio.Future()

            async def disable():
                async with pools.control.connection() as conn:
                    pid = (await (await conn.execute("SELECT pg_backend_pid()"))
                           .fetchone())[0]
                    started.set_result(pid)
                    row = await (await conn.execute(
                        "SELECT public.admin_set_account_enabled(%s,%s,%s,false)",
                        (actor_id, 1, target_id),
                    )).fetchone()
                    return row[0]

            task = asyncio.create_task(disable())
            await _wait_for_lock(owner, await started, task)
            assert not task.done()
        assert await task == "disabled"
        async with owner.connection() as conn:
            row = await (await conn.execute(
                "SELECT a.is_enabled,a.auth_version,s.ntfy_topic "
                "FROM accounts a JOIN account_settings s ON s.account_id=a.id "
                "WHERE a.id=%s", (target_id,),
            )).fetchone()
            assert row == (False, 2, "committed")
        with pytest.raises(errors.InsufficientPrivilege):
            async with target.connection():
                pytest.fail("stale account principal was admitted")

    asyncio.run(_scenario(check))


@pytest.mark.parametrize("kind", ["email", "reset", "invitation", "redemption"])
def test_proof_or_invitation_admission_waits_for_disable_commit(kind):
    async def check(owner, pools, actor_id, target_id):
        async with pools.control.connection() as conn:
            if kind == "email":
                token = await issue_email_challenge(
                    conn, target_id, 1, PURPOSE_CURRENT, "target@example.invalid",
                )
            elif kind == "reset":
                token = secrets.token_urlsafe(32)
                assert await issue_password_reset(
                    conn, token, initiator=INITIATOR_PUBLIC,
                    email="target@example.invalid",
                ) == "target@example.invalid"
            else:
                invitation_id, token = await issue_invitation_record(
                    conn,
                    {"id": target_id, "auth_version": 1,
                     "is_admin": True, "is_enabled": True},
                    "invitee@example.invalid",
                )
        assert token
        started = asyncio.Future()

        async def admit():
            async with pools.control.connection() as conn:
                pid = (await (await conn.execute("SELECT pg_backend_pid()"))
                       .fetchone())[0]
                started.set_result(pid)
                if kind == "email":
                    return await email_challenge_send_usable(
                        conn, target_id, 1, PURPOSE_CURRENT, token,
                    )
                if kind == "reset":
                    return await password_reset_send_usable(conn, token)
                if kind == "redemption":
                    try:
                        await redeem_invitation(conn, token, "new-password")
                        return True
                    except InvitationUnavailable:
                        return False
                try:
                    async with invitation_mail_admission(
                        conn,
                        {"id": target_id, "auth_version": 1,
                         "is_admin": True, "is_enabled": True},
                        invitation_id,
                    ):
                        return True
                except InvitationUnavailable:
                    return False

        async with pools.control.connection() as disabling:
            assert (await (await disabling.execute(
                "SELECT public.admin_set_account_enabled(%s,1,%s,false)",
                (actor_id, target_id),
            )).fetchone())[0] == "disabled"
            task = asyncio.create_task(admit())
            await _wait_for_lock(owner, await started, task)
        assert await task is False

    asyncio.run(_scenario(check))


def test_disable_and_reenable_leave_old_proofs_invitations_and_credentials_dead():
    async def check(owner, pools, actor_id, target_id):
        target = AccountPool(pools.runtime, AccountPrincipal(target_id, True, 1))
        async with target.connection() as conn:
            credential = await create_device(conn, "Target phone")
        async with pools.control.connection() as conn:
            challenge = await issue_email_challenge(
                conn, target_id, 1, PURPOSE_CURRENT, "target@example.invalid",
            )
            reset = secrets.token_urlsafe(32)
            address = await issue_password_reset(
                conn, reset, initiator=INITIATOR_PUBLIC,
                email="target@example.invalid",
            )
            invitation_id, invite = await issue_invitation_record(
                conn,
                {"id": target_id, "auth_version": 1,
                 "is_admin": True, "is_enabled": True},
                "invitee@example.invalid",
            )
        assert challenge and address == "target@example.invalid"
        assert await authenticate_ingest(
            pools.control, credential.username, credential.secret,
            legacy_username="unused", legacy_password="unused",
        ) is not None

        assert await _set_enabled(pools, actor_id, 1, target_id, False) == "disabled"
        assert await _set_enabled(pools, actor_id, 1, target_id, False) == "already_disabled"
        async with owner.connection() as conn:
            account = await (await conn.execute(
                "SELECT is_enabled,auth_version,password_hash FROM accounts WHERE id=%s",
                (target_id,),
            )).fetchone()
            assert account == (False, 2, "target-hash")
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL FROM ingest_credentials WHERE public_id=%s",
                (credential.public_id,),
            )).fetchone() == (True,)
            assert await (await conn.execute(
                "SELECT revoked_at IS NOT NULL FROM invitations WHERE id=%s",
                (invitation_id,),
            )).fetchone() == (True,)
        async with pools.control.connection() as conn:
            assert not await email_challenge_send_usable(
                conn, target_id, 1, PURPOSE_CURRENT, challenge,
            )
            assert await consume_email_challenge(
                conn, target_id, 1, PURPOSE_CURRENT, challenge,
            ) is None
            assert not await password_reset_send_usable(conn, reset)
            assert await consume_password_reset(conn, reset, "replacement-hash") is None
            with pytest.raises(InvitationUnavailable):
                async with invitation_mail_admission(
                    conn,
                    {"id": target_id, "auth_version": 1,
                     "is_admin": True, "is_enabled": True},
                    invitation_id,
                ):
                    pytest.fail("disabled issuer was admitted to mail")
            with pytest.raises(InvitationUnavailable):
                await redeem_invitation(conn, invite, "new-password")
        assert await authenticate_ingest(
            pools.control, credential.username, credential.secret,
            legacy_username="unused", legacy_password="unused",
        ) is None

        assert await _set_enabled(pools, actor_id, 1, target_id, True) == "enabled"
        assert await _set_enabled(pools, actor_id, 1, target_id, True) == "already_enabled"
        fresh = AccountPool(pools.runtime, AccountPrincipal(target_id, True, 2))
        async with fresh.connection() as conn:
            assert (await (await conn.execute(
                "SELECT count(*) FROM account_settings WHERE account_id=%s",
                (target_id,),
            )).fetchone())[0] == 1
        async with pools.control.connection() as conn:
            assert not await email_challenge_send_usable(
                conn, target_id, 1, PURPOSE_CURRENT, challenge,
            )
            assert not await password_reset_send_usable(conn, reset)
            with pytest.raises(InvitationUnavailable):
                await redeem_invitation(conn, invite, "new-password")
        assert await authenticate_ingest(
            pools.control, credential.username, credential.secret,
            legacy_username="unused", legacy_password="unused",
        ) is None
        with pytest.raises(errors.InsufficientPrivilege):
            async with target.connection():
                pytest.fail("old account session was admitted after re-enablement")

    asyncio.run(_scenario(check))


def test_concurrent_admin_disablement_keeps_one_usable_admin():
    async def check(owner, pools, first_id, second_id):
        release = asyncio.Event()
        ready = [asyncio.Event(), asyncio.Event()]

        async def disable(index, actor_id, target_id):
            async with pools.control.connection() as conn:
                ready[index].set()
                await release.wait()
                row = await (await conn.execute(
                    "SELECT public.admin_set_account_enabled(%s,1,%s,false)",
                    (actor_id, target_id),
                )).fetchone()
                return row[0]

        attempts = [
            asyncio.create_task(disable(0, first_id, second_id)),
            asyncio.create_task(disable(1, second_id, first_id)),
        ]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 5)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*attempts, return_exceptions=True), 5)
        assert sum(result == "disabled" for result in results) == 1
        failures = [result for result in results if isinstance(result, BaseException)]
        assert len(failures) == 1
        assert isinstance(failures[0], errors.InsufficientPrivilege)
        async with owner.connection() as conn:
            row = await (await conn.execute(
                "SELECT count(*) FROM accounts WHERE is_admin AND is_enabled "
                "AND password_hash <> ''",
            )).fetchone()
            assert row == (1,)

    asyncio.run(_scenario(check))


def test_last_admin_counts_an_oidc_only_method_but_not_an_unusable_account():
    async def check(owner, pools, actor_id, target_id):
        async with owner.connection() as conn:
            await conn.execute(
                "UPDATE accounts SET password_hash=NULL WHERE id=%s", (actor_id,),
            )
        with pytest.raises(errors.InsufficientPrivilege):
            await _set_enabled(pools, actor_id, 1, target_id, False)
        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO oidc_identities(account_id,issuer,subject) "
                "VALUES (%s,'https://issuer.example.invalid','actor-subject')",
                (actor_id,),
            )
        assert await _set_enabled(pools, actor_id, 1, target_id, False) == "disabled"
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM accounts WHERE is_admin AND is_enabled",
            )).fetchone() == (1,)

    asyncio.run(_scenario(check))


def test_delayed_provider_result_cannot_write_after_disable():
    async def check(owner, pools, actor_id, target_id):
        target = AccountPool(pools.runtime, AccountPrincipal(target_id, True, 1))
        ended = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
        async with target.connection() as conn:
            await conn.execute(
                "INSERT INTO trips "
                "(account_id,device,source,started_at,ended_at,distance_m,start_geom,point_count) "
                "VALUES (%s,'phone','manual',%s,%s,1000,"
                "ST_SetSRID(ST_MakePoint(20,10),4326)::geography,2)",
                (target_id, ended - timedelta(days=1), ended),
            )

        started = asyncio.Event()
        release = asyncio.Event()

        class Provider:
            async def reverse(self, client, lat, lon):
                started.set()
                await release.wait()
                return "stale address"

        task = asyncio.create_task(GeocodeWorker(target, None, Provider(), 0).run_once())
        await asyncio.wait_for(started.wait(), 5)
        try:
            assert await _set_enabled(pools, actor_id, 1, target_id, False) == "disabled"
        finally:
            release.set()
        with pytest.raises(errors.InsufficientPrivilege):
            await task
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM geocode_cache WHERE account_id=%s", (target_id,),
            )).fetchone() == (0,)

    asyncio.run(_scenario(check))


def test_disabled_account_is_not_enumerated_or_admitted_to_notification_send():
    async def check(owner, pools, actor_id, target_id):
        target = AccountPool(pools.runtime, AccountPrincipal(target_id, True, 1))
        window_end = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)
        async with target.connection() as conn:
            await conn.execute(
                "UPDATE account_settings SET ntfy_topic='target' WHERE account_id=%s",
                (target_id,),
            )
            await conn.execute(
                "INSERT INTO trips "
                "(account_id,device,source,started_at,ended_at,distance_m,start_geom,point_count) "
                "VALUES (%s,'phone','manual',%s,%s,1000,"
                "ST_SetSRID(ST_MakePoint(20,10),4326)::geography,2)",
                (target_id, window_end - timedelta(days=2), window_end - timedelta(days=2)),
            )
        assert target_id in {
            principal.account_id for principal in await enabled_principals(pools.control)
        }
        assert await _set_enabled(pools, actor_id, 1, target_id, False) == "disabled"
        assert target_id not in {
            principal.account_id for principal in await enabled_principals(pools.control)
        }
        posted = []

        def send(request):
            posted.append(request)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            worker = NudgeWorker(
                target, client, "https://ntfy.example.invalid", "target",
                "", "", "", "", ZoneInfo("UTC"), 18,
            )
            with pytest.raises(errors.InsufficientPrivilege):
                await worker.run_once(window_end)
        assert posted == []
        async with owner.connection() as conn:
            assert await (await conn.execute(
                "SELECT count(*) FROM nudge_delivery_windows WHERE account_id=%s",
                (target_id,),
            )).fetchone() == (0,)

    asyncio.run(_scenario(check))
