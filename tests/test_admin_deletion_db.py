"""Deletion, protected purge and quiescence under real application roles."""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from psycopg import errors

from app.account_lifecycle import (
    AccountLifecycleUnavailable, cancel_account_deletion, purge_account,
    request_account_deletion, set_account_enabled,
)
from app.accounts import account_exists, create_admin
from app.application_roles import OWNED_TABLES
from app.local_auth import hash_password
from app.oidc_attempts import _digest, finish_oidc_reauth, start_oidc_attempt
from tests.test_admin_lifecycle_routes_db import _scenario
from tests.test_admin_routes_db import _app, _client

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires disposable PostGIS")


async def _activate(owner, *, email="member@example.invalid", admin=False, password="member-hash"):
    async with owner.connection() as conn:
        await conn.execute("DROP INDEX IF EXISTS accounts_singleton_idx")
        await conn.execute("ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_is_admin_check")
        target = (await (await conn.execute(
            "INSERT INTO accounts(email,password_hash,is_admin) VALUES (%s,%s,%s) RETURNING id",
            (email, password, admin),
        )).fetchone())[0]
        await conn.execute("INSERT INTO account_settings(account_id) VALUES (%s)", (target,))
    return target


async def _elapsed(owner, target):
    async with owner.connection() as conn:
        await conn.execute("UPDATE accounts SET deletion_deadline=clock_timestamp()-interval '1 second' WHERE id=%s", (target,))


def test_deletion_requires_exact_confirmation_cannot_bypass_grace_and_cancellation_revives_nothing():
    async def check(owner, pools, actor):
        target = await _activate(owner)
        async with owner.connection() as conn:
            await conn.execute(
                "INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,account_id,kind) "
                "VALUES ('target-key','target-user','secret-hash',%s,'legacy')", (target,),
            )
        async with pools.control.connection() as conn:
            for email, ack in [("wrong@example.invalid", True), ("member@example.invalid", False)]:
                with pytest.raises(AccountLifecycleUnavailable):
                    await request_account_deletion(conn, actor, target, email=email, acknowledge=ack)
            before = time.time()
            assert await request_account_deletion(conn, actor, target, email="member@example.invalid", acknowledge=True) == "scheduled"
            with pytest.raises(AccountLifecycleUnavailable):
                await set_account_enabled(conn, actor, target, enable=True)
            with pytest.raises(AccountLifecycleUnavailable):
                await request_account_deletion(conn, actor, target, email="member@example.invalid", acknowledge=True)
            with pytest.raises(AccountLifecycleUnavailable):
                await purge_account(conn, actor, target, email="member@example.invalid", confirm=True, verified_password_hash=actor["password_hash"])
            assert await cancel_account_deletion(conn, actor, target) == "cancelled"
        async with owner.connection() as conn:
            state = await (await conn.execute("SELECT is_enabled,auth_version,deletion_deadline FROM accounts WHERE id=%s", (target,))).fetchone()
            assert state == (True, 2, None)
            assert (await (await conn.execute("SELECT revoked_at IS NOT NULL,generation FROM ingest_credentials WHERE account_id=%s", (target,))).fetchone()) == (True, 2)
            deadline = (await (await conn.execute("SELECT occurred_at FROM account_security_audit WHERE action='request_deletion'", ())).fetchone())[0]
            assert before <= deadline.timestamp() <= time.time()
        async with pools.control.connection() as conn:
            await request_account_deletion(conn, actor, target, email="member@example.invalid", acknowledge=True)
        async with owner.connection() as conn:
            deadline = (await (await conn.execute("SELECT deletion_deadline FROM accounts WHERE id=%s", (target,))).fetchone())[0]
            assert 30 * 86400 - 5 < deadline.timestamp() - time.time() <= 30 * 86400
        await _elapsed(owner, target)
        async with pools.control.connection() as conn:
            with pytest.raises(AccountLifecycleUnavailable):
                await cancel_account_deletion(conn, actor, target)
    asyncio.run(_scenario(check))


def test_first_admin_purge_cleans_dependency_graph_preserves_other_account_and_bootstrap_marker():
    async def check(owner, pools, first):
        second = await _activate(owner, email="second@example.invalid", admin=True, password="second-hash")
        async with owner.connection() as conn:
            await conn.execute("INSERT INTO oidc_identities(account_id,issuer,subject) VALUES (%s,'https://idp.example','first')", (first["id"],))
            device = (await (await conn.execute("INSERT INTO tracking_devices(account_id,label) VALUES (%s,'phone') RETURNING id", (first["id"],))).fetchone())[0]
            vehicle = (await (await conn.execute("SELECT id FROM vehicles WHERE account_id=%s", (first["id"],))).fetchone())[0]
            await conn.execute("INSERT INTO tracking_device_aliases(account_id,original_label,tracking_device_id) VALUES (%s,'phone',%s)", (first["id"], device))
            await conn.execute("INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,account_id,tracking_device_id,kind) VALUES ('purge-key','purge-user','hash',%s,%s,'device')", (first["id"],device))
            point = (await (await conn.execute("INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom) VALUES (%s,%s,'phone',now(),ST_SetSRID(ST_MakePoint(1,2),4326)) RETURNING id", (first["id"],device))).fetchone())[0]
            await conn.execute("INSERT INTO trip_boundary_overrides(account_id,tracking_device_id,device,kind,point_id) VALUES (%s,%s,'phone','force',%s)", (first["id"],device,point))
            await conn.execute("INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment) VALUES (%s,%s,current_date,'fuel',20,'fully_business')", (first["id"],vehicle))
            await conn.execute("INSERT INTO invitations(email,token_digest,issued_by,expires_at) VALUES ('invite@example.invalid',%s,%s,now()+interval '1 day')", ('a'*64, first["id"]))
            other_vehicle = (await (await conn.execute("INSERT INTO vehicles(account_id,name) VALUES (%s,'Survivor') RETURNING id", (second,))).fetchone())[0]
        actor = dict(first, id=second, email="second@example.invalid", password_hash="second-hash")
        async with pools.control.connection() as conn:
            await request_account_deletion(conn, actor, first["id"], email=first["email"], acknowledge=True)
        await _elapsed(owner, first["id"])
        async with pools.control.connection() as conn:
            assert await purge_account(conn, actor, first["id"], email=first["email"], confirm=True, verified_password_hash="second-hash") == "purged"
            assert await account_exists(conn)
            with pytest.raises(errors.UniqueViolation):
                async with conn.transaction():
                    await create_admin(conn, "again@example.invalid", "hash")
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("DELETE FROM accounts WHERE id=%s", (second,))
        async with owner.connection() as conn:
            for table in OWNED_TABLES:
                assert (await (await conn.execute(f"SELECT count(*) FROM {table} WHERE account_id=%s", (first["id"],))).fetchone())[0] == 0
            marker = await (await conn.execute("SELECT first_account_id,bootstrap_completed_at IS NOT NULL FROM instance_state")).fetchone()
            assert marker == (None, True)
            assert (await (await conn.execute("SELECT name FROM vehicles WHERE id=%s", (other_vehicle,))).fetchone())[0] == "Survivor"
            assert (await (await conn.execute("SELECT target_account_id,outcome FROM account_security_audit WHERE action='purge_account'")).fetchone()) == (first["id"], "purged")
            assert (await (await conn.execute("SELECT count(*) FROM oidc_identities WHERE account_id=%s", (first["id"],))).fetchone())[0] == 0
            assert (await (await conn.execute("SELECT count(*) FROM invitations WHERE issued_by=%s", (first["id"],))).fetchone())[0] == 0
    asyncio.run(_scenario(check))


def test_purge_fails_closed_for_admitted_work_and_cancelled_lock_wait_rolls_back():
    async def check(owner, pools, actor):
        target = await _activate(owner)
        async with pools.control.connection() as conn:
            await request_account_deletion(conn, actor, target, email="member@example.invalid", acknowledge=True)
        await _elapsed(owner, target)
        async with owner.connection() as holder:
            async with holder.transaction():
                await holder.execute("SELECT 1 FROM accounts WHERE id=%s FOR SHARE", (target,))
                async with pools.control.connection() as conn:
                    before = time.monotonic()
                    with pytest.raises(AccountLifecycleUnavailable):
                        await purge_account(conn, actor, target, email="member@example.invalid", confirm=True, verified_password_hash=actor["password_hash"])
                    assert time.monotonic() - before < 2
            async with holder.transaction():
                await holder.execute("SELECT pg_advisory_xact_lock(901412,1)")
                async with pools.control.connection() as conn:
                    task = asyncio.create_task(purge_account(conn, actor, target, email="member@example.invalid", confirm=True, verified_password_hash=actor["password_hash"]))
                    await asyncio.sleep(.1)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert (await (await conn.execute("SELECT count(*) FROM accounts WHERE id=%s", (target,))).fetchone())[0] == 1
        async with pools.control.connection() as conn:
            assert await purge_account(conn, actor, target, email="member@example.invalid", confirm=True, verified_password_hash=actor["password_hash"]) == "purged"
    asyncio.run(_scenario(check))


def test_purge_exact_fresh_oidc_proof_is_target_bound_one_use_and_version_bound():
    async def check(owner, pools, actor):
        target = await _activate(owner)
        other = await _activate(owner, email="other@example.invalid")
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET password_hash=NULL WHERE id=%s", (actor["id"],))
            await conn.execute("INSERT INTO oidc_identities(account_id,issuer,subject) VALUES (%s,'https://idp.example','actor-subject')", (actor["id"],))
        actor["password_hash"] = None
        async with pools.control.connection() as conn:
            for target_id, email in [(target,"member@example.invalid"),(other,"other@example.invalid")]:
                await request_account_deletion(conn, actor, target_id, email=email, acknowledge=True)
        await _elapsed(owner,target)
        await _elapsed(owner,other)
        nonce = "n"*43
        async with pools.control.connection() as conn:
            assert await start_oidc_attempt(conn, action="reauth", state="s"*43, nonce="o"*43, browser_nonce=nonce, account_id=actor["id"], auth_version=1, proof_action="purge_account", target=str(target))
            assert not await finish_oidc_reauth(conn,state="s"*43,nonce="o"*43,browser_nonce=nonce,account_id=actor["id"],auth_version=1,issuer="https://idp.example",subject="wrong-subject",auth_time=time.time())
            assert await start_oidc_attempt(conn, action="reauth", state="t"*43, nonce="o"*43, browser_nonce=nonce, account_id=actor["id"], auth_version=1, proof_action="purge_account", target=str(target))
            assert not await finish_oidc_reauth(conn,state="t"*43,nonce="o"*43,browser_nonce=nonce,account_id=actor["id"],auth_version=1,issuer="https://idp.example",subject="actor-subject",auth_time=time.time()-300)
            assert await start_oidc_attempt(conn, action="reauth", state="u"*43, nonce="o"*43, browser_nonce=nonce, account_id=actor["id"], auth_version=1, proof_action="purge_account", target=str(target))
            assert await finish_oidc_reauth(conn,state="u"*43,nonce="o"*43,browser_nonce=nonce,account_id=actor["id"],auth_version=1,issuer="https://idp.example",subject="actor-subject",auth_time=time.time())
            for target_id,email,actor_user in [(other,"other@example.invalid",actor),(target,"member@example.invalid",dict(actor,auth_version=2))]:
                with pytest.raises(AccountLifecycleUnavailable):
                    await purge_account(conn,actor_user,target_id,email=email,confirm=True,browser_nonce=nonce)
            with pytest.raises(AccountLifecycleUnavailable):
                await purge_account(conn,actor,target,email="member@example.invalid",confirm=False,browser_nonce=nonce)
            assert await purge_account(conn,actor,target,email="member@example.invalid",confirm=True,browser_nonce=nonce) == "purged"
            with pytest.raises(AccountLifecycleUnavailable):
                await purge_account(conn,actor,other,email="other@example.invalid",confirm=True,browser_nonce=nonce)
        async with owner.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM oidc_action_proofs WHERE browser_digest=%s", (_digest(nonce),))).fetchone())[0] == 0
    asyncio.run(_scenario(check))


def test_deletion_routes_auth_csrf_confirmation_and_fresh_password():
    async def check(owner, pools, actor):
        password = "A strong administrator password!"
        async with owner.connection() as conn:
            await conn.execute("UPDATE accounts SET password_hash=%s WHERE id=%s", (hash_password(password), actor["id"]))
        target = await _activate(owner)
        app = _app(pools)
        async with await _client(app) as client, await _client(app) as member:
            await client.post(f"/test/session/{actor['id']}/1")
            await member.post(f"/test/session/{target}/1")
            base = f"/admin/accounts/{target}"
            form = {"csrf_token":"route-csrf", "target_email":"member@example.invalid", "acknowledge":"1"}
            assert (await member.post(base+"/deletion",data=form)).status_code == 403
            assert (await client.post(base+"/deletion",data=dict(form,csrf_token="wrong"))).status_code == 403
            assert (await client.post(base+"/deletion",data=dict(form,actor_id=str(target)))).status_code == 400
            assert (await client.post(base+"/deletion",data={k:v for k,v in form.items() if k!="acknowledge"})).status_code == 400
            assert (await client.post(base+"/deletion",data=form)).status_code == 200
            await _elapsed(owner,target)
            purge = {"csrf_token":"route-csrf", "target_email":"member@example.invalid", "confirm_purge":"1", "current_password":password}
            assert (await client.post(base+"/purge",data={k:v for k,v in purge.items() if k!="confirm_purge"})).status_code == 400
            assert (await client.post(base+"/purge",data=dict(purge,current_password="wrong"))).status_code == 401
            assert (await client.post(base+"/purge",data=dict(purge,current_password=""))).status_code == 409
            done = await client.post(base+"/purge",data=purge)
            assert done.status_code == 200
            assert "permanently removed" in done.text
    asyncio.run(_scenario(check))


def test_purge_lease_survives_cancelled_security_mail_waiter_and_disable_commit():
    import threading
    from contextlib import asynccontextmanager
    from app.account_work import external_account_work
    from app.mailer import Mailer
    from app.password_reset import SecurityMailAdmission

    async def check(owner, pools, actor):
        target = await _activate(owner)
        started, release = threading.Event(), threading.Event()

        def blocking_transport(mailer, message):
            started.set()
            release.wait(10)

        mailer = Mailer("",25,"","","none",False,"from@example.invalid","to@example.invalid",transport=blocking_transport)
        admission = SecurityMailAdmission(limit=1)

        @asynccontextmanager
        async def admit():
            async with pools.control.connection() as conn:
                async with conn.transaction():
                    cur = await conn.execute("SELECT 1 FROM accounts WHERE id=%s AND is_enabled FOR SHARE", (target,))
                    yield await cur.fetchone() is not None

        send = asyncio.create_task(admission.send(
            mailer, mailer.compose("Test","No private data"), admit=admit,
            lease=lambda: external_account_work(pools.control,target),
        ))
        try:
            assert await asyncio.to_thread(started.wait,3)
            send.cancel()
            with pytest.raises(asyncio.CancelledError):
                await send
            async with pools.control.connection() as conn:
                await request_account_deletion(conn,actor,target,email="member@example.invalid",acknowledge=True)
            await _elapsed(owner,target)
            async with pools.control.connection() as conn:
                with pytest.raises(AccountLifecycleUnavailable):
                    await purge_account(conn,actor,target,email="member@example.invalid",confirm=True,verified_password_hash=actor["password_hash"])
            assert admission._slots.locked()
        finally:
            release.set()
            await admission.drain()
        async with pools.control.connection() as conn:
            assert await purge_account(conn,actor,target,email="member@example.invalid",confirm=True,verified_password_hash=actor["password_hash"]) == "purged"
        assert not admission._tasks
    asyncio.run(_scenario(check))


def test_direct_mail_cancellation_retains_admitted_account_transaction_until_thread_finishes():
    import threading
    from app.account_context import AccountPool, AccountPrincipal
    from app.mailer import Mailer

    async def check(owner, pools, actor):
        target = await _activate(owner)
        started, release = threading.Event(), threading.Event()
        mailer = Mailer("",25,"","","none",False,"from@example.invalid","to@example.invalid",
                        transport=lambda *_: (started.set(), release.wait(10)))

        async def worker():
            async with AccountPool(pools.runtime,AccountPrincipal(target,True,1)).connection():
                await mailer.send(mailer.compose("Test","Body"))

        task = asyncio.create_task(worker())
        try:
            assert await asyncio.to_thread(started.wait,3)
            task.cancel()
            await asyncio.sleep(.05)
            assert not task.done()
            async with owner.connection() as conn:
                with pytest.raises(errors.LockNotAvailable):
                    async with conn.transaction():
                        await conn.execute("SELECT 1 FROM accounts WHERE id=%s FOR UPDATE NOWAIT", (target,))
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with pools.control.connection() as conn:
            await request_account_deletion(conn,actor,target,email="member@example.invalid",acknowledge=True)
    asyncio.run(_scenario(check))


def test_admitted_delayed_provider_job_blocks_purge_and_cannot_write_after_disable():
    from types import SimpleNamespace
    from app.account_workers import AccountWorker
    from tests.auth_db_fixtures import auth_config

    async def check(owner,pools,actor):
        target = await _activate(owner)
        admitted, release = asyncio.Event(), asyncio.Event()
        wrote = False

        def factory(pool, config):
            if pool.principal.account_id != target:
                return None

            async def run_once():
                nonlocal wrote
                admitted.set()
                await release.wait()
                async with pool.connection() as conn:
                    await conn.execute("UPDATE account_settings SET display_tz='US/Pacific' WHERE account_id=%s", (target,))
                    wrote = True
            return SimpleNamespace(run_once=run_once)

        worker = AccountWorker(pools,auth_config(os.environ["TEST_DATABASE_URL"]),factory,
                               label="purge-provider-test",debounce_s=1,sweep_s=60)
        job = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(admitted.wait(),3)
            async with pools.control.connection() as conn:
                await request_account_deletion(conn,actor,target,email="member@example.invalid",acknowledge=True)
            await _elapsed(owner,target)
            async with pools.control.connection() as conn:
                with pytest.raises(AccountLifecycleUnavailable):
                    await purge_account(conn,actor,target,email="member@example.invalid",confirm=True,verified_password_hash=actor["password_hash"])
        finally:
            release.set()
            await job
        assert not wrote
        assert worker.status.last_failure_type == "InsufficientPrivilege"
        async with pools.control.connection() as conn:
            assert await purge_account(conn,actor,target,email="member@example.invalid",confirm=True,verified_password_hash=actor["password_hash"]) == "purged"
    asyncio.run(_scenario(check))
