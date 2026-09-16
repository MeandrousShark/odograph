"""Managed credentials, authenticated pools and narrow bootstrap acceptance."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo

from app import role_setup as roles
from app.account_context import account_connection, control_connection
from tests.test_account_context_db import (
    ACCOUNT_A, CONTROL_ROLE, RUNTIME_ROLE, TEST_DB, drop_fixture, expect_denied,
    make_admin_pool, run_scenario,
)

pytestmark = pytest.mark.skipif(not TEST_DB, reason='set TEST_DATABASE_URL')


async def state_and_verifiers(admin):
    async with admin.connection() as conn:
        state = await roles._load_state(conn)
        cur = await conn.execute('SELECT rolname,rolpassword FROM pg_authid WHERE rolname=ANY(%s) ORDER BY rolname',([CONTROL_ROLE,RUNTIME_ROLE],))
        return state, await cur.fetchall()


def test_managed_restart_reuses_secrets_and_verifiers_and_closes_setup():
    async def scenario(admin):
        before, verifiers = await state_and_verifiers(admin)
        url = make_conninfo(TEST_DB, application_name='p0-managed-lifecycle')
        async with roles.managed_role_pools(url) as pools:
            async with control_connection(pools.control) as conn:
                assert (await (await conn.execute('SELECT count(*) FROM account_context_p0.accounts')).fetchone())[0] == 2
            async with account_connection(pools.runtime, ACCOUNT_A) as conn:
                assert await (await conn.execute('SELECT note FROM account_context_p0.ledger')).fetchall() == [('a note',)]
            async with admin.connection() as conn:
                rows = await (await conn.execute("SELECT usename FROM pg_stat_activity WHERE application_name='p0-managed-lifecycle'")).fetchall()
                assert rows and {row[0] for row in rows} <= {CONTROL_ROLE,RUNTIME_ROLE}
        after, after_verifiers = await state_and_verifiers(admin)
        assert before == after and verifiers == after_verifiers
        assert before.runtime_password not in repr(before)
        assert before.control_password not in repr(before)
        assert pools.control.closed and pools.runtime.closed
    asyncio.run(run_scenario(scenario))


def test_initialization_failure_rolls_back_roles_state_and_fixture(monkeypatch):
    async def scenario():
        admin = make_admin_pool()
        await admin.open(wait=True)
        original = roles._validate_contract
        try:
            async with admin.connection() as conn:
                await drop_fixture(conn)
            async def fail(*args):
                raise ValueError('injected initialization interruption')
            monkeypatch.setattr(roles,'_validate_contract',fail)
            with pytest.raises(roles.RoleSetupError,match='creation failed'):
                await roles.create_p0_fixture(TEST_DB)
            async with admin.connection() as conn:
                assert await (await conn.execute("SELECT to_regnamespace('odograph_internal')")).fetchone() == (None,)
                assert await (await conn.execute('SELECT count(*) FROM pg_roles WHERE rolname=ANY(%s)',(list(roles.ALL_ROLES),))).fetchone() == (0,)
            monkeypatch.setattr(roles,'_validate_contract',original)
            state=await roles.create_p0_fixture(TEST_DB)
            assert state == await roles.prepare_fixture_roles(TEST_DB)
        finally:
            async with admin.connection() as conn:
                await drop_fixture(conn)
            await admin.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('role',[CONTROL_ROLE,RUNTIME_ROLE])
@pytest.mark.parametrize('bad_password',['', 'incorrect-managed-credential'])
def test_missing_or_wrong_passwords_fail_safely_and_close_partial_pools(monkeypatch,caplog,role,bad_password):
    async def scenario(admin):
        state,_=await state_and_verifiers(admin)
        altered=replace(state,**{('control_password' if role==CONTROL_ROLE else 'runtime_password'):bad_password})
        async def stale_state(url):
            return altered
        monkeypatch.setattr(roles,'prepare_fixture_roles',stale_state)
        opened=[]
        original=roles.AsyncConnectionPool
        def record(*args,**kwargs):
            pool=original(*args,**kwargs)
            opened.append(pool)
            return pool
        monkeypatch.setattr(roles,'AsyncConnectionPool',record)
        with pytest.raises(roles.RoleSetupError) as exc:
            async with roles.managed_role_pools(TEST_DB):
                pytest.fail('invalid credential was accepted')
        assert all(pool.closed for pool in opened)
        if role==RUNTIME_ROLE:
            assert len(opened)==1
        transcript=caplog.text+str(exc.value)
        assert state.runtime_password not in transcript and state.control_password not in transcript
        assert TEST_DB not in transcript
        assert 'incorrect-managed-credential' not in transcript
    asyncio.run(run_scenario(scenario))


def test_pool_body_exception_and_cancellation_are_preserved():
    async def scenario(admin):
        for exception in (ValueError('caller failure'),asyncio.CancelledError()):
            with pytest.raises(type(exception)) as caught:
                async with roles.managed_role_pools(TEST_DB) as pools:
                    raise exception
            assert caught.value is exception
            assert pools.control.closed and pools.runtime.closed
    asyncio.run(run_scenario(scenario))


def test_actual_identity_database_and_installation_are_validated():
    async def scenario(admin):
        state,_=await state_and_verifiers(admin)
        async with await psycopg.AsyncConnection.connect(roles.role_conninfo(TEST_DB,state,RUNTIME_ROLE)) as conn:
            for expected,role in ((state,CONTROL_ROLE),(replace(state,database_name='wrong'),RUNTIME_ROLE),(replace(state,installation_id=uuid4()),RUNTIME_ROLE)):
                with pytest.raises(roles.RoleSetupError):
                    await roles.validate_role_connection(conn,expected,role)
        async with admin.connection() as conn:
            with pytest.raises(roles.RoleSetupError):
                await roles.validate_role_connection(conn, state, conn.info.user)
    asyncio.run(run_scenario(scenario))


@pytest.mark.parametrize('drift',[
    'GRANT SELECT(note) ON account_context_p0.ledger TO PUBLIC',
    'GRANT SELECT ON account_context_p0.accounts TO odograph_control WITH GRANT OPTION',
    'CREATE POLICY extra_permissive ON account_context_p0.ledger USING(true)',
    'GRANT odograph_migrate TO odograph_control',
    'ALTER FUNCTION account_context_p0.bootstrap_first_account(text) SET search_path=public',
])
def test_contract_rejects_drift_and_setup_repairs_it(drift):
    async def scenario(admin):
        state,_=await state_and_verifiers(admin)
        async with admin.connection() as conn:
            await conn.execute(drift)
        async with await psycopg.AsyncConnection.connect(roles.role_conninfo(TEST_DB,state,RUNTIME_ROLE)) as conn:
            with pytest.raises(roles.RoleSetupError):
                await roles.validate_role_connection(conn,state,RUNTIME_ROLE)
        await roles.prepare_fixture_roles(TEST_DB)
        async with roles.managed_role_pools(TEST_DB):
            pass
    asyncio.run(run_scenario(scenario))


def test_outsider_membership_in_privileged_roles_is_removed():
    async def scenario(admin):
        async with admin.connection() as conn:
            await conn.execute('CREATE ROLE p0_outsider NOLOGIN')
            await conn.execute('GRANT odograph_migrate,odograph_bootstrap TO p0_outsider')
        try:
            state,_=await state_and_verifiers(admin)
            async with admin.connection() as conn:
                with pytest.raises(roles.RoleSetupError):
                    await roles._validate_contract(conn,state)
            await roles.prepare_fixture_roles(TEST_DB)
            async with admin.connection() as conn:
                assert await (await conn.execute("SELECT count(*) FROM pg_auth_members WHERE member='p0_outsider'::regrole")).fetchone()==(0,)
        finally:
            async with admin.connection() as conn:
                await conn.execute('DROP ROLE p0_outsider')
    asyncio.run(run_scenario(scenario))


def test_bootstrap_and_preauthorized_new_account_are_narrow_and_atomic():
    async def scenario(admin):
        grant_id=uuid4()
        async with roles.managed_role_pools(TEST_DB) as pools:
            async with control_connection(pools.control) as conn:
                for statement in (
                    "INSERT INTO account_context_p0.accounts(email) VALUES('bypass@example.com')",
                    'DELETE FROM account_context_p0.bootstrap_state',
                    'UPDATE account_context_p0.bootstrap_state SET first_account_id=NULL',
                    "INSERT INTO account_context_p0.vehicles(account_id,name) VALUES(1,'bypass')",
                    "SELECT odograph_internal.create_new_account('bypass@example.com',true)",
                    f"INSERT INTO odograph_internal.provision_authorizations(id) VALUES('{grant_id}')",
                ):
                    await expect_denied(conn,statement)
                first=await (await conn.execute("SELECT account_context_p0.bootstrap_first_account('first@example.com')")).fetchone()
                assert first[0] > 0
            async with control_connection(pools.control) as conn:
                async with conn.transaction(force_rollback=True):
                    with pytest.raises(errors.RaiseException):
                        await conn.execute("SELECT account_context_p0.provision_authorized_account(%s,'absent@example.com')",(grant_id,))
            async with admin.connection() as conn:
                await conn.execute('INSERT INTO odograph_internal.provision_authorizations(id) VALUES(%s)',(grant_id,))
            with pytest.raises(ValueError):
                async with control_connection(pools.control) as conn:
                    await conn.execute("SELECT account_context_p0.provision_authorized_account(%s,'rolled-back@example.com')",(grant_id,))
                    raise ValueError('rollback identity, default and grant consumption')
            async with admin.connection() as conn:
                assert await (await conn.execute('SELECT account_id FROM odograph_internal.provision_authorizations WHERE id=%s',(grant_id,))).fetchone()==(None,)
                assert await (await conn.execute('SELECT count(*) FROM account_context_p0.accounts')).fetchone()==(1,)
            async def attempt(email):
                async with control_connection(pools.control) as conn:
                    return (await (await conn.execute('SELECT account_context_p0.provision_authorized_account(%s,%s)',(grant_id,email))).fetchone())[0]
            result=await asyncio.gather(attempt('second@example.com'),attempt('third@example.com'),return_exceptions=True)
            assert sum(isinstance(value,int) for value in result)==1
            assert sum(isinstance(value,errors.RaiseException) for value in result)==1
            async with admin.connection() as conn:
                assert await (await conn.execute('SELECT is_admin,count(*) FROM account_context_p0.accounts GROUP BY is_admin ORDER BY is_admin')).fetchall()==[(False,1),(True,1)]
                assert await (await conn.execute('SELECT count(*) FROM account_context_p0.vehicles')).fetchone()==(2,)
    asyncio.run(run_scenario(scenario,seed=False))


def test_scoped_connection_cannot_end_its_transaction_early():
    async def scenario(admin):
        async with roles.managed_role_pools(TEST_DB) as pools:
            for method in ('commit','rollback'):
                async with account_connection(pools.runtime,ACCOUNT_A) as conn:
                    with pytest.raises(psycopg.ProgrammingError):
                        await getattr(conn,method)()
                    assert await (await conn.execute('SELECT account_id FROM account_context_p0.ledger')).fetchall()==[(1,)]
    asyncio.run(run_scenario(scenario))


def test_preauthorized_account_requires_completed_initial_bootstrap():
    async def scenario(admin):
        grant_id = uuid4()
        async with admin.connection() as conn:
            await conn.execute(
                'INSERT INTO odograph_internal.provision_authorizations(id) VALUES(%s)',
                (grant_id,),
            )
        async with roles.managed_role_pools(TEST_DB) as pools:
            with pytest.raises(errors.RaiseException, match='incomplete'):
                async with control_connection(pools.control) as conn:
                    await conn.execute(
                        "SELECT account_context_p0.provision_authorized_account(%s,'early@example.com')",
                        (grant_id,),
                    )
        async with admin.connection() as conn:
            assert await (await conn.execute(
                'SELECT count(*) FROM account_context_p0.accounts'
            )).fetchone() == (0,)
            assert await (await conn.execute(
                'SELECT account_id FROM odograph_internal.provision_authorizations WHERE id=%s',
                (grant_id,),
            )).fetchone() == (None,)
    asyncio.run(run_scenario(scenario, seed=False))
