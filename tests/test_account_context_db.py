"""DB-backed proof of the account context and the database role boundary.

Nothing here touches schema public. The fixture builds its own schema, its
own representative parent/child tables, and the four managed roles from
app/role_setup.py, then drops all of it again. That matters
twice over: tests/conftest.py's reset machinery truncates public and raises
on schema objects left behind there, and roles are cluster-wide, so they
survive every reset in this suite and would follow a leak into every later
test.

The admin connection (`TEST_DATABASE_URL`, a superuser in the disposable
container) is used for setup and for assertions about raw table contents.
A superuser ignores row-level security entirely, which is exactly why the
runtime role must never be one, and is why every isolation assertion below
runs on a connection authenticated as `odograph_runtime` instead.
"""
from __future__ import annotations

import asyncio
import os

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool

from app.account_context import (
    ACCOUNT_CONTEXT_SETTING,
    AccountContextError,
    AccountPrincipal,
    RuntimePrivilegeError,
    account_connection,
    check_runtime_privileges,
    control_connection,
    current_account_context,
    runtime_privilege_problems,
)
from app.role_setup import ALL_ROLES, SQL_DIR, _prepare
from conftest import full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)

SCHEMA = "account_context_p0"
OWNER_ROLE = "odograph_migrate"
CONTROL_ROLE = "odograph_control"
RUNTIME_ROLE = "odograph_runtime"

# The setup helper supplies persisted fixture credentials for each scenario.
PASSWORDS: dict[str, str] = {}

ACCOUNT_A = AccountPrincipal(account_id=1, enabled=True, auth_version=1)
ACCOUNT_B = AccountPrincipal(account_id=2, enabled=True, auth_version=1)

# Representative parent/child shape for the ownership work, deliberately
# small: an identity table, a singleton bootstrap marker, an account-owned
# parent, and an account-owned child that references the parent through a
# composite key so a cross-account reference is a foreign key violation
# rather than a policy question. ON DELETE SET NULL names the optional
# column only, so deleting a vehicle clears the reference and leaves the
# required account owner in place.
FIXTURE_DDL = (SQL_DIR / "p0_fixture.sql").read_text()

FIXTURE_TABLES = ("accounts", "bootstrap_state", "vehicles", "ledger")


class _ScenarioFailure(RuntimeError):
    """Raised on purpose inside a scenario to force a rollback."""


async def expect_denied(conn, statement) -> None:
    """Assert a statement is refused, leaving the connection usable.

    The refusal aborts whatever transaction the statement ran in, so it is
    run inside a nested block of its own: the failure rolls that block back
    to its savepoint and the caller can keep using the connection instead of
    hitting "current transaction is aborted" on the next statement.
    """
    with pytest.raises(errors.InsufficientPrivilege):
        async with conn.transaction():
            await conn.execute(statement)


def role_url(role: str) -> str:
    parameters = conninfo_to_dict(TEST_DB)
    parameters["user"] = role
    parameters["password"] = PASSWORDS[role]
    return make_conninfo(**parameters)


def qualified(table: str) -> str:
    return f"{SCHEMA}.{table}"


# ---------------------------------------------------------------------------
# Fixture construction and teardown.
# ---------------------------------------------------------------------------


async def _public_may_create_database(conn) -> bool:
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_database d, aclexplode(d.datacl) a "
        "WHERE d.datname = current_database() AND a.grantee = 0 "
        "  AND a.privilege_type = 'CREATE')"
    )
    return (await cur.fetchone())[0]


async def _role_exists(conn, role: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return await cur.fetchone() is not None


async def drop_fixture(conn) -> None:
    """Remove the schema and the roles, in that order.

    The schema goes first so its policies and grants disappear with it;
    DROP OWNED BY then clears anything left pointing at each role, which is
    what a bare DROP ROLE would refuse over.
    """
    await conn.execute(
        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
    )
    await conn.execute("DROP SCHEMA IF EXISTS odograph_internal CASCADE")
    for role in ALL_ROLES:
        if not await _role_exists(conn, role):
            continue
        await conn.execute(
            sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
        )
        await conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def provision(conn, **overrides) -> None:
    if overrides:
        raise ValueError("the P0 contract does not accept table/schema overrides")
    state = await _prepare(conn)
    PASSWORDS[RUNTIME_ROLE] = state.runtime_password
    PASSWORDS[CONTROL_ROLE] = state.control_password


async def build_fixture(admin_pool) -> None:
    async with admin_pool.connection() as conn:
        await drop_fixture(conn)
        await conn.execute(FIXTURE_DDL)
        await provision(conn)


async def seed_two_accounts(admin_pool) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            f"INSERT INTO {qualified('accounts')} (id, email) "
            "VALUES (1, 'a@example.com'), (2, 'b@example.com')"
        )
        await conn.execute(
            f"INSERT INTO {qualified('vehicles')} (account_id, id, name) "
            "VALUES (1, 10, 'A car'), (2, 20, 'B car')"
        )
        await conn.execute(
            f"INSERT INTO {qualified('ledger')} (account_id, id, vehicle_id, note) "
            "VALUES (1, 100, 10, 'a note'), (2, 200, 20, 'b note')"
        )


def make_admin_pool(**kwargs) -> AsyncConnectionPool:
    return AsyncConnectionPool(TEST_DB, min_size=1, max_size=2, open=False, **kwargs)


def make_role_pool(role: str, **kwargs) -> AsyncConnectionPool:
    kwargs.setdefault("min_size", 1)
    kwargs.setdefault("max_size", 1)
    return AsyncConnectionPool(role_url(role), open=False, **kwargs)


async def run_scenario(scenario, *, seed=True) -> None:
    """Build the fixture, run one scenario, and always tear it down."""
    admin_pool = make_admin_pool()
    await admin_pool.open(wait=True)
    public_create = None
    try:
        # Live ownership tests use these same cluster-wide role names. Remove
        # their disposable application objects before P0's role teardown, so
        # DROP OWNED cannot leave a partially destroyed application schema.
        await full_schema_reset(admin_pool)
        async with admin_pool.connection() as conn:
            public_create = await _public_may_create_database(conn)
        await build_fixture(admin_pool)
        if seed:
            await seed_two_accounts(admin_pool)
        await scenario(admin_pool)
    finally:
        async with admin_pool.connection() as conn:
            await drop_fixture(conn)
            # Provisioning revokes CREATE on the database from PUBLIC. It
            # is not granted by default, but restore it if this cluster
            # had it so the disposable database is left as it was found.
            if public_create:
                await conn.execute(
                    sql.SQL("GRANT CREATE ON DATABASE {} TO PUBLIC").format(
                        sql.Identifier(conninfo_to_dict(TEST_DB)["dbname"])
                    )
                )
        await admin_pool.close()


# ---------------------------------------------------------------------------
# No context, and broken context.
# ---------------------------------------------------------------------------


async def _no_context_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            assert await current_account_context(conn) is None
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('vehicles')}")
            assert (await cur.fetchone())[0] == 0

            # A write with no context is refused by the policy's WITH CHECK
            # rather than silently landing in some default account.
            await expect_denied(
                conn,
                f"INSERT INTO {qualified('ledger')} (account_id, note) "
                "VALUES (1, 'smuggled')",
            )

            cur = await conn.execute(
                f"UPDATE {qualified('ledger')} SET note = 'changed'"
            )
            assert cur.rowcount == 0
            cur = await conn.execute(f"DELETE FROM {qualified('ledger')}")
            assert cur.rowcount == 0
    finally:
        await pool.close()

    async with admin_pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
        assert (await cur.fetchone())[0] == 2


def test_runtime_role_without_context_cannot_read_or_write_personal_rows():
    asyncio.run(run_scenario(_no_context_scenario))


async def _broken_context_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        # Missing: never set at all. current_setting returns NULL, the
        # comparison is NULL, the policy denies without raising.
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
                assert (await cur.fetchone())[0] == 0

        # Empty: what PostgreSQL leaves behind once a transaction-local
        # setting has been set and reverted. NULLIF turns it back into
        # NULL; without that the bigint cast would raise on every query
        # made on a recycled connection.
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config(%s, '', true)", (ACCOUNT_CONTEXT_SETTING,)
                )
                cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
                assert (await cur.fetchone())[0] == 0

        # Malformed: the cast raises, which denies loudly instead of
        # quietly. Either way no row is returned.
        async with pool.connection() as conn:
            with pytest.raises(errors.InvalidTextRepresentation):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config(%s, 'not-an-id', true)",
                        (ACCOUNT_CONTEXT_SETTING,),
                    )
                    await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")

        # The same malformed value reaches the helper as a corrupted
        # context rather than an absent one.
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config(%s, 'not-an-id', true)",
                    (ACCOUNT_CONTEXT_SETTING,),
                )
                with pytest.raises(AccountContextError):
                    await current_account_context(conn)

        # An account that does not exist is still a well-formed context,
        # and still matches no row.
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config(%s, '9999', true)", (ACCOUNT_CONTEXT_SETTING,)
                )
                cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
                assert (await cur.fetchone())[0] == 0
    finally:
        await pool.close()


def test_missing_malformed_and_empty_context_all_fail_closed():
    asyncio.run(run_scenario(_broken_context_scenario))


# ---------------------------------------------------------------------------
# Correct contexts.
# ---------------------------------------------------------------------------


async def _isolation_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE, max_size=2)
    await pool.open(wait=True)
    try:
        for principal, expected_note, expected_vehicle in (
            (ACCOUNT_A, "a note", "A car"),
            (ACCOUNT_B, "b note", "B car"),
        ):
            async with account_connection(pool, principal) as conn:
                assert await current_account_context(conn) == principal.account_id
                cur = await conn.execute(
                    f"SELECT account_id, note FROM {qualified('ledger')}"
                )
                assert await cur.fetchall() == [
                    (principal.account_id, expected_note)
                ]
                cur = await conn.execute(
                    f"SELECT account_id, name FROM {qualified('vehicles')}"
                )
                assert await cur.fetchall() == [
                    (principal.account_id, expected_vehicle)
                ]

        # A write lands in the writer's account and stays invisible to the
        # other one.
        async with account_connection(pool, ACCOUNT_A) as conn:
            await conn.execute(
                f"INSERT INTO {qualified('ledger')} (account_id, note) "
                "VALUES (%s, 'new a note')",
                (ACCOUNT_A.account_id,),
            )
        async with account_connection(pool, ACCOUNT_B) as conn:
            cur = await conn.execute(
                f"SELECT count(*) FROM {qualified('ledger')}"
            )
            assert (await cur.fetchone())[0] == 1

        # Writing another account's id is refused by the policy's WITH
        # CHECK even though the context itself is valid.
        async with pool.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config(%s, %s, true)",
                        (ACCOUNT_CONTEXT_SETTING, str(ACCOUNT_A.account_id)),
                    )
                    await conn.execute(
                        f"INSERT INTO {qualified('ledger')} (account_id, note) "
                        "VALUES (%s, 'cross account')",
                        (ACCOUNT_B.account_id,),
                    )
    finally:
        await pool.close()


def test_each_account_context_exposes_only_that_accounts_rows():
    asyncio.run(run_scenario(_isolation_scenario))


# ---------------------------------------------------------------------------
# The autocommit no-op, and why SET LOCAL is the only acceptable form.
# ---------------------------------------------------------------------------


async def _autocommit_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.set_autocommit(True)
            try:
                # set_config reports the value it just set, so the call
                # site looks like it worked. It did not: the implicit
                # single-statement transaction ended and took the setting
                # with it.
                cur = await conn.execute(
                    "SELECT set_config(%s, %s, true)",
                    (ACCOUNT_CONTEXT_SETTING, str(ACCOUNT_A.account_id)),
                )
                assert (await cur.fetchone())[0] == str(ACCOUNT_A.account_id)

                assert await current_account_context(conn) is None
                cur = await conn.execute(
                    f"SELECT count(*) FROM {qualified('ledger')}"
                )
                assert (await cur.fetchone())[0] == 0

                # The helper refuses this rather than letting it look like
                # a successful, and total, loss of access.
                from app.account_context import apply_account_context

                with pytest.raises(AccountContextError) as error:
                    await apply_account_context(conn, ACCOUNT_A)
                assert "transaction" in str(error.value)

                # Wrapping the same autocommit connection in an explicit
                # transaction block makes the identical call correct.
                async with conn.transaction():
                    await apply_account_context(conn, ACCOUNT_A)
                    cur = await conn.execute(
                        f"SELECT count(*) FROM {qualified('ledger')}"
                    )
                    assert (await cur.fetchone())[0] == 1
            finally:
                await conn.set_autocommit(False)
    finally:
        await pool.close()


def test_transaction_local_setting_outside_a_transaction_block_denies_access():
    asyncio.run(run_scenario(_autocommit_scenario))


async def _session_set_leaks_scenario(admin_pool):
    """A session-level SET survives the return to the pool; the helper's
    transaction-local one does not.

    psycopg_pool only rolls back an open transaction when a connection
    comes back. It issues no DISCARD ALL, so nothing else would undo a
    session setting. This is the whole reason SET LOCAL is mandatory.
    """
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT pg_backend_pid()")
            first_pid = (await cur.fetchone())[0]
            # Session-level: is_local false. Deliberately the wrong form.
            await conn.execute(
                "SELECT set_config(%s, %s, false)",
                (ACCOUNT_CONTEXT_SETTING, str(ACCOUNT_A.account_id)),
            )

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT pg_backend_pid()")
            assert (await cur.fetchone())[0] == first_pid, "expected one pooled backend"
            assert await current_account_context(conn) == ACCOUNT_A.account_id
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
            assert (await cur.fetchone())[0] == 1
            # Clear it before proving the correct form on the same backend.
            await conn.execute(
                "SELECT set_config(%s, '', false)", (ACCOUNT_CONTEXT_SETTING,)
            )

        async with account_connection(pool, ACCOUNT_A) as conn:
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
            assert (await cur.fetchone())[0] == 1

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT pg_backend_pid()")
            assert (await cur.fetchone())[0] == first_pid
            assert await current_account_context(conn) is None
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
            assert (await cur.fetchone())[0] == 0
    finally:
        await pool.close()


def test_session_level_set_leaks_to_the_next_borrower_but_set_local_does_not():
    asyncio.run(run_scenario(_session_set_leaks_scenario))


# ---------------------------------------------------------------------------
# Pooled reuse across every exit path.
# ---------------------------------------------------------------------------


async def _assert_pool_is_clean(pool, *, backend_pid=None) -> int:
    """Borrow again and prove the previous account's context is gone.

    Returns the backend pid, and asserts it against `backend_pid` when one
    is given: on a single-connection pool the next borrower has to be the
    same physical backend for this to prove anything, rather than a fresh
    connection the pool quietly opened because the old one was discarded.
    """
    async with pool.connection() as conn:
        assert await current_account_context(conn) is None
        cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
        assert (await cur.fetchone())[0] == 0
        cur = await conn.execute("SELECT pg_backend_pid()")
        pid = (await cur.fetchone())[0]
        if backend_pid is not None:
            assert pid == backend_pid, "expected the same pooled backend"
    async with control_connection(pool) as conn:
        # Used here as an assertion, not as a deployment shape: the helper
        # raises on a connection that still carries a context, so reaching
        # this point is the check.
        assert await current_account_context(conn) is None
    return pid


async def _reuse_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        backend_pid = await _assert_pool_is_clean(pool)

        # Success.
        async with account_connection(pool, ACCOUNT_A) as conn:
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
            assert (await cur.fetchone())[0] == 1
        await _assert_pool_is_clean(pool, backend_pid=backend_pid)

        # Explicit rollback of the helper's own transaction.
        async with account_connection(pool, ACCOUNT_A) as conn:
            await conn.execute(
                f"INSERT INTO {qualified('ledger')} (account_id, note) "
                "VALUES (%s, 'rolled back')",
                (ACCOUNT_A.account_id,),
            )
            raise psycopg.Rollback
        await _assert_pool_is_clean(pool, backend_pid=backend_pid)

        # Raised exception.
        with pytest.raises(_ScenarioFailure):
            async with account_connection(pool, ACCOUNT_B) as conn:
                await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
                raise _ScenarioFailure("boom")
        await _assert_pool_is_clean(pool, backend_pid=backend_pid)

        # Cancellation while a statement is in flight. The pool recycles
        # this backend rather than discarding it, so the next borrower is
        # the same session that was carrying account B a moment ago.
        started = asyncio.Event()

        async def cancelled_borrower():
            async with account_connection(pool, ACCOUNT_B) as conn:
                started.set()
                await conn.execute("SELECT pg_sleep(30)")

        task = asyncio.create_task(cancelled_borrower())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_pool_is_clean(pool, backend_pid=backend_pid)

        # The rolled-back insert never reached the table.
        async with admin_pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT count(*) FROM {qualified('ledger')} WHERE note = 'rolled back'"
            )
            assert (await cur.fetchone())[0] == 0
    finally:
        await pool.close()


def test_pooled_connection_never_retains_a_previous_accounts_context():
    asyncio.run(run_scenario(_reuse_scenario))


async def _concurrent_borrowers_scenario(admin_pool):
    """Three overlapping account transactions on a three-connection pool.

    A barrier, not a sleep: every borrower holds its context open until all
    of them have one, so the transactions genuinely overlap instead of
    merely running in sequence quickly enough to look like they did.
    """
    pool = make_role_pool(RUNTIME_ROLE, min_size=3, max_size=3)
    await pool.open(wait=True)
    try:
        principals = [ACCOUNT_A, ACCOUNT_B, ACCOUNT_A]
        barrier = asyncio.Barrier(len(principals))

        async def borrower(principal):
            async with account_connection(pool, principal) as conn:
                assert await current_account_context(conn) == principal.account_id
                await barrier.wait()
                cur = await conn.execute(
                    f"SELECT DISTINCT account_id FROM {qualified('ledger')}"
                )
                rows = await cur.fetchall()
                assert rows == [(principal.account_id,)]
                await barrier.wait()
                return await current_account_context(conn)

        results = await asyncio.gather(*(borrower(p) for p in principals))
        assert results == [p.account_id for p in principals]

        for _ in range(3):
            await _assert_pool_is_clean(pool)
    finally:
        await pool.close()


def test_concurrent_borrowers_each_keep_their_own_context():
    asyncio.run(run_scenario(_concurrent_borrowers_scenario))


# ---------------------------------------------------------------------------
# Nested and borrowed connections.
# ---------------------------------------------------------------------------


async def _nesting_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE, min_size=3, max_size=3)
    await pool.open(wait=True)
    try:
        async with account_connection(pool, ACCOUNT_A) as conn_a:
            assert await current_account_context(conn_a) == ACCOUNT_A.account_id

            # A second borrow from the same pool is a different connection
            # and inherits nothing.
            async with pool.connection() as borrowed:
                assert await current_account_context(borrowed) is None
                cur = await borrowed.execute(
                    f"SELECT count(*) FROM {qualified('ledger')}"
                )
                assert (await cur.fetchone())[0] == 0

            # A nested account connection gets its own context on its own
            # connection, and does not disturb the outer one.
            async with account_connection(pool, ACCOUNT_B) as conn_b:
                assert conn_b is not conn_a
                assert await current_account_context(conn_b) == ACCOUNT_B.account_id
                cur = await conn_b.execute(
                    f"SELECT DISTINCT account_id FROM {qualified('ledger')}"
                )
                assert await cur.fetchall() == [(ACCOUNT_B.account_id,)]

            assert await current_account_context(conn_a) == ACCOUNT_A.account_id
            cur = await conn_a.execute(
                f"SELECT DISTINCT account_id FROM {qualified('ledger')}"
            )
            assert await cur.fetchall() == [(ACCOUNT_A.account_id,)]

            # A nested transaction on the *same* connection is a savepoint.
            # PostgreSQL restores the setting when the savepoint aborts, so
            # the outer context survives an inner failure.
            with pytest.raises(_ScenarioFailure):
                async with conn_a.transaction():
                    await conn_a.execute(
                        "SELECT set_config(%s, %s, true)",
                        (ACCOUNT_CONTEXT_SETTING, str(ACCOUNT_B.account_id)),
                    )
                    assert (
                        await current_account_context(conn_a) == ACCOUNT_B.account_id
                    )
                    raise _ScenarioFailure("abandon the savepoint")
            assert await current_account_context(conn_a) == ACCOUNT_A.account_id
    finally:
        await pool.close()


def test_nested_and_borrowed_connections_each_need_their_own_context():
    asyncio.run(run_scenario(_nesting_scenario))


# ---------------------------------------------------------------------------
# Composite keys and optional references.
# ---------------------------------------------------------------------------


async def _composite_key_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        # Account B cannot attach its ledger row to account A's vehicle:
        # the composite reference has no matching (account_id, id) pair, so
        # this is a foreign key violation rather than something the policy
        # has to catch after the fact.
        with pytest.raises(errors.ForeignKeyViolation):
            async with account_connection(pool, ACCOUNT_B) as conn:
                await conn.execute(
                    f"INSERT INTO {qualified('ledger')} "
                    "(account_id, vehicle_id, note) VALUES (%s, 10, 'stolen')",
                    (ACCOUNT_B.account_id,),
                )

        # Its own vehicle is fine.
        async with account_connection(pool, ACCOUNT_B) as conn:
            await conn.execute(
                f"INSERT INTO {qualified('ledger')} "
                "(account_id, vehicle_id, note) VALUES (%s, 20, 'own vehicle')",
                (ACCOUNT_B.account_id,),
            )

        # Re-pointing an existing row at the other account's vehicle is the
        # same violation on UPDATE.
        with pytest.raises(errors.ForeignKeyViolation):
            async with account_connection(pool, ACCOUNT_B) as conn:
                await conn.execute(
                    f"UPDATE {qualified('ledger')} SET vehicle_id = 10"
                )
    finally:
        await pool.close()

    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT count(*) FROM {qualified('ledger')} WHERE note = 'stolen'"
        )
        assert (await cur.fetchone())[0] == 0


def test_composite_foreign_key_rejects_a_child_row_from_another_account():
    asyncio.run(run_scenario(_composite_key_scenario))


async def _optional_reference_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        async with account_connection(pool, ACCOUNT_A) as conn:
            await conn.execute(f"DELETE FROM {qualified('vehicles')} WHERE id = 10")
            cur = await conn.execute(
                f"SELECT account_id, id, vehicle_id, note FROM {qualified('ledger')}"
            )
            # ON DELETE SET NULL names vehicle_id only, so the optional
            # reference is cleared and the required account owner, which is
            # also part of the same composite foreign key, is untouched.
            assert await cur.fetchall() == [
                (ACCOUNT_A.account_id, 100, None, "a note")
            ]

        # The other account's row and vehicle are entirely unaffected.
        async with account_connection(pool, ACCOUNT_B) as conn:
            cur = await conn.execute(
                f"SELECT account_id, id, vehicle_id FROM {qualified('ledger')}"
            )
            assert await cur.fetchall() == [(ACCOUNT_B.account_id, 200, 20)]
    finally:
        await pool.close()


def test_optional_reference_delete_clears_only_the_optional_reference():
    asyncio.run(run_scenario(_optional_reference_scenario))


# ---------------------------------------------------------------------------
# Identity-only grants.
# ---------------------------------------------------------------------------


async def _control_grants_scenario(admin_pool):
    control_pool = make_role_pool(CONTROL_ROLE)
    runtime_pool = make_role_pool(RUNTIME_ROLE)
    await control_pool.open(wait=True)
    await runtime_pool.open(wait=True)
    try:
        async with control_connection(control_pool) as conn:
            # Login: look one account up by its identifier.
            cur = await conn.execute(
                f"SELECT id, is_enabled, auth_version FROM {qualified('accounts')} "
                "WHERE email = %s",
                ("a@example.com",),
            )
            assert await cur.fetchone() == (1, True, 1)

            # Enumeration: see every account, which is why identity tables
            # carry no row-level security.
            cur = await conn.execute(
                f"SELECT count(*) FROM {qualified('accounts')}"
            )
            assert (await cur.fetchone())[0] == 2

            # Credential maintenance.
            await conn.execute(
                f"UPDATE {qualified('accounts')} SET auth_version = auth_version + 1 "
                "WHERE id = 1"
            )

            # Bootstrap state is readable, but only the definer function may change it.
            cur = await conn.execute(
                f"SELECT count(*) FROM {qualified('bootstrap_state')}"
            )
            assert (await cur.fetchone())[0] == 1

            await expect_denied(conn, f"DELETE FROM {qualified('bootstrap_state')}")
            # Ledger rows are unreachable at the grant level, with or
            # without an account context, so there is no policy to get
            # wrong here.
            await expect_denied(conn, f"SELECT count(*) FROM {qualified('ledger')}")

            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config(%s, '1', true)", (ACCOUNT_CONTEXT_SETTING,)
                )
                await expect_denied(
                    conn, f"SELECT count(*) FROM {qualified('ledger')}"
                )

        # Control cannot insert defaults for an existing account directly.
        async with control_connection(control_pool) as conn:
            await expect_denied(conn,
                f"INSERT INTO {qualified('vehicles')} (account_id, name) "
                "VALUES (1, 'control created')"
            )
            await expect_denied(conn, f"SELECT count(*) FROM {qualified('vehicles')}")
            await expect_denied(conn, f"DELETE FROM {qualified('vehicles')}")

        # The mirror image: the runtime role holds no grant at all on the
        # identity tables, so a correct account context does not open the
        # credential store.
        async with account_connection(runtime_pool, ACCOUNT_A) as conn:
            await expect_denied(conn, f"SELECT count(*) FROM {qualified('accounts')}")
            await expect_denied(
                conn, f"SELECT count(*) FROM {qualified('bootstrap_state')}"
            )
    finally:
        await runtime_pool.close()
        await control_pool.close()


def test_control_role_does_identity_work_and_cannot_read_ledger_rows():
    asyncio.run(run_scenario(_control_grants_scenario))


# ---------------------------------------------------------------------------
# The startup privilege check.
# ---------------------------------------------------------------------------


async def _privilege_check_scenario(admin_pool):
    runtime_pool = make_role_pool(RUNTIME_ROLE)
    owner_pool = make_admin_pool()
    await runtime_pool.open(wait=True)
    await owner_pool.open(wait=True)
    try:
        async with runtime_pool.connection() as conn:
            assert await runtime_privilege_problems(conn, schemas=(SCHEMA,)) == []
            await check_runtime_privileges(conn, schemas=(SCHEMA,))

        # The owner role fails on ownership, truncate rights, and the
        # ability to create objects, which is the misconfiguration of
        # pointing the application at the migration credential.
        async with owner_pool.connection() as conn:
            await conn.execute("SET LOCAL ROLE odograph_migrate")
            problems = await runtime_privilege_problems(conn, schemas=(SCHEMA,))
            assert f"owns protected table {SCHEMA}.ledger" in problems
            assert f"can truncate protected table {SCHEMA}.ledger" in problems
            assert f"can create objects in schema {SCHEMA}" in problems
            with pytest.raises(RuntimePrivilegeError):
                await check_runtime_privileges(conn, schemas=(SCHEMA,))

        # The superuser the disposable container hands out fails on every
        # attribute, including the BYPASSRLS that makes policies advisory.
        async with admin_pool.connection() as conn:
            problems = await runtime_privilege_problems(conn, schemas=(SCHEMA,))
            assert any("is a superuser" in problem for problem in problems)
            assert any("has BYPASSRLS" in problem for problem in problems)

        # Drift is caught on the runtime role itself: an attribute granted
        # by hand, and a membership in the owner role that would let it
        # SET ROLE its way past every policy.
        async with admin_pool.connection() as conn:
            await conn.execute(
                sql.SQL("ALTER ROLE {} BYPASSRLS").format(sql.Identifier(RUNTIME_ROLE))
            )
            await conn.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(OWNER_ROLE), sql.Identifier(RUNTIME_ROLE)
                )
            )
        async with runtime_pool.connection() as conn:
            problems = await runtime_privilege_problems(conn, schemas=(SCHEMA,))
            assert f"role {RUNTIME_ROLE} has BYPASSRLS" in problems
            assert f"owns protected table {SCHEMA}.ledger" in problems
            with pytest.raises(RuntimePrivilegeError):
                await check_runtime_privileges(conn, schemas=(SCHEMA,))

        # Re-provisioning restores the contract rather than needing the
        # role rebuilt by hand.
        async with admin_pool.connection() as conn:
            await provision(conn)
        async with runtime_pool.connection() as conn:
            assert await runtime_privilege_problems(conn, schemas=(SCHEMA,)) == []
    finally:
        await owner_pool.close()
        await runtime_pool.close()


def test_owner_and_privilege_misconfiguration_is_rejected_by_the_startup_check():
    asyncio.run(run_scenario(_privilege_check_scenario))


async def _runtime_cannot_create_objects_scenario(admin_pool):
    pool = make_role_pool(RUNTIME_ROLE)
    await pool.open(wait=True)
    try:
        # The privilege check's claims are backed by what the server
        # actually refuses, not only by what the catalog reports.
        async with pool.connection() as conn:
            for statement in (
                f"CREATE TABLE {SCHEMA}.smuggled (id int)",
                "CREATE SCHEMA smuggled",
                f"TRUNCATE TABLE {qualified('ledger')}",
                f"ALTER TABLE {qualified('ledger')} DISABLE ROW LEVEL SECURITY",
                f"DROP POLICY odograph_account_write ON {qualified('ledger')}",
                f"ALTER TABLE {qualified('ledger')} OWNER TO {RUNTIME_ROLE}",
                f"SET ROLE {OWNER_ROLE}",
            ):
                await expect_denied(conn, statement)
    finally:
        await pool.close()


def test_runtime_role_cannot_create_truncate_or_assume_its_way_around_policies():
    asyncio.run(run_scenario(_runtime_cannot_create_objects_scenario))


# ---------------------------------------------------------------------------
# Atomic bootstrap under the real restricted grants.
# ---------------------------------------------------------------------------


async def _bootstrap(pool, email, *, barrier=None, fail=False) -> int:
    if barrier is not None:
        await barrier.wait()
    async with control_connection(pool) as conn:
        async with conn.transaction():
            cur = await conn.execute(
                f"SELECT {SCHEMA}.bootstrap_first_account(%s)",(email,))
            account_id = (await cur.fetchone())[0]
            if fail:
                raise _ScenarioFailure("deliberate mid-transaction failure")
            return account_id


async def _bootstrap_counts(admin_pool) -> tuple[int, int, int]:
    """Row counts read as the superuser, which sees past row-level security."""
    async with admin_pool.connection() as conn:
        counts = []
        for table in ("accounts", "bootstrap_state", "vehicles"):
            cur = await conn.execute(f"SELECT count(*) FROM {qualified(table)}")
            counts.append((await cur.fetchone())[0])
    return tuple(counts)


async def _bootstrap_scenario(admin_pool):
    pool = make_role_pool(CONTROL_ROLE, max_size=3)
    await pool.open(wait=True)
    try:
        assert await _bootstrap_counts(admin_pool) == (0, 1, 0)

        # Mid-transaction failure takes all three writes with it.
        with pytest.raises(_ScenarioFailure):
            await _bootstrap(pool, "fails@example.com", fail=True)
        assert await _bootstrap_counts(admin_pool) == (0, 1, 0)

        # A clean run commits all three together.
        account_id = await _bootstrap(pool, "first@example.com")
        assert await _bootstrap_counts(admin_pool) == (1, 1, 1)
        async with admin_pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT id, first_account_id FROM {qualified('bootstrap_state')}"
            )
            assert await cur.fetchone() == (1, account_id)
            cur = await conn.execute(
                f"SELECT account_id, name FROM {qualified('vehicles')}"
            )
            assert await cur.fetchall() == [(account_id, "Default vehicle")]
    finally:
        await pool.close()


def test_first_account_bootstrap_commits_or_rolls_back_as_one_unit():
    asyncio.run(run_scenario(_bootstrap_scenario, seed=False))


async def _concurrent_bootstrap_scenario(admin_pool):
    """Two first-account attempts released together by a barrier.

    Both calls lock the same preseeded bootstrap-state row. The winner
    commits identity, defaults and completion together; the loser sees
    completion and refuses to create another account.
    """
    pool = make_role_pool(CONTROL_ROLE, min_size=2, max_size=2)
    await pool.open(wait=True)
    try:
        barrier = asyncio.Barrier(2)
        results = await asyncio.gather(
            _bootstrap(pool, "one@example.com", barrier=barrier),
            _bootstrap(pool, "two@example.com", barrier=barrier),
            return_exceptions=True,
        )
        winners = [result for result in results if isinstance(result, int)]
        losers = [result for result in results if isinstance(result, BaseException)]
        assert len(winners) == 1, results
        assert len(losers) == 1
        assert isinstance(losers[0], errors.RaiseException)

        assert await _bootstrap_counts(admin_pool) == (1, 1, 1)
        async with admin_pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT first_account_id FROM {qualified('bootstrap_state')}"
            )
            assert await cur.fetchone() == (winners[0],)
            cur = await conn.execute(
                f"SELECT account_id FROM {qualified('vehicles')}"
            )
            assert await cur.fetchall() == [(winners[0],)]
    finally:
        await pool.close()


def test_concurrent_first_account_bootstrap_has_exactly_one_winner():
    asyncio.run(run_scenario(_concurrent_bootstrap_scenario, seed=False))


# ---------------------------------------------------------------------------
# Restore and re-provisioning.
# ---------------------------------------------------------------------------


async def _role_and_grant_state(conn) -> dict:
    """Everything provisioning is responsible for, in a comparable shape."""
    state: dict = {}

    cur = await conn.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
        "       rolreplication, rolcanlogin, rolinherit "
        "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(ALL_ROLES),),
    )
    state["roles"] = await cur.fetchall()

    cur = await conn.execute(
        "SELECT grantee, table_name, privilege_type "
        "FROM information_schema.role_table_grants "
        "WHERE table_schema = %s AND grantee = ANY(%s) "
        "ORDER BY grantee, table_name, privilege_type",
        (SCHEMA, list(ALL_ROLES)),
    )
    state["grants"] = await cur.fetchall()

    cur = await conn.execute(
        "SELECT tablename, policyname, permissive, roles, cmd, qual, with_check "
        "FROM pg_policies WHERE schemaname = %s "
        "ORDER BY tablename, policyname",
        (SCHEMA,),
    )
    state["policies"] = await cur.fetchall()

    cur = await conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
        "       pg_get_userbyid(c.relowner) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind = 'r' ORDER BY c.relname",
        (SCHEMA,),
    )
    state["tables"] = await cur.fetchall()

    cur = await conn.execute(
        "SELECT has_schema_privilege(%s, %s, 'USAGE'), "
        "       has_schema_privilege(%s, %s, 'CREATE')",
        (RUNTIME_ROLE, SCHEMA, RUNTIME_ROLE, SCHEMA),
    )
    state["schema_rights"] = await cur.fetchone()
    return state


async def _dump_fixture_data(conn) -> dict[str, bytes]:
    dumped: dict[str, bytes] = {}
    for table in FIXTURE_TABLES:
        async with conn.cursor() as cur:
            async with cur.copy(
                f"COPY {qualified(table)} TO STDOUT (FORMAT binary)"
            ) as copy:
                blocks = [bytes(block) async for block in copy]
        dumped[table] = b"".join(blocks)
    return dumped


async def _restore_fixture_data(conn, dumped: dict[str, bytes]) -> None:
    for table in FIXTURE_TABLES:
        async with conn.cursor() as cur:
            async with cur.copy(
                f"COPY {qualified(table)} FROM STDIN (FORMAT binary)"
            ) as copy:
                await copy.write(dumped[table])
    # Restoring identity values verbatim does not advance their sequences,
    # exactly as tests/conftest.py's own restore has to fix up.
    for table, column in (("accounts", "id"), ("vehicles", "id"), ("ledger", "id")):
        await conn.execute(
            f"SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"  COALESCE((SELECT max({column}) FROM {qualified(table)}), 1), "
            f"  EXISTS (SELECT 1 FROM {qualified(table)}))",
            (qualified(table), column),
        )


async def _restore_scenario(admin_pool):
    """Drop the fixture the way a disaster would, put the data back, and
    re-provision.

    This remains a small COPY-based reconstruction check. The separate
    archive suite proves real PostgreSQL 16 dump/restore into a fresh cluster.
    """
    async with admin_pool.connection() as conn:
        before = await _role_and_grant_state(conn)
        dumped = await _dump_fixture_data(conn)

    assert before["policies"], "expected provisioning to have created policies"
    assert before["grants"], "expected provisioning to have made grants"

    async with admin_pool.connection() as conn:
        await drop_fixture(conn)
        cur = await conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
            (list(ALL_ROLES),),
        )
        assert (await cur.fetchone())[0] == 0

    async with admin_pool.connection() as conn:
        await conn.execute(FIXTURE_DDL)
        await conn.execute(f"DELETE FROM {SCHEMA}.bootstrap_state")
        await _restore_fixture_data(conn, dumped)
        await provision(conn)

    async with admin_pool.connection() as conn:
        after = await _role_and_grant_state(conn)
    assert after == before

    # The helper supplies the rebuilt roles' managed credentials.
    pool = make_role_pool(RUNTIME_ROLE, max_size=2)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(f"SELECT count(*) FROM {qualified('ledger')}")
            assert (await cur.fetchone())[0] == 0
        for principal, note in ((ACCOUNT_A, "a note"), (ACCOUNT_B, "b note")):
            async with account_connection(pool, principal) as conn:
                cur = await conn.execute(
                    f"SELECT account_id, note FROM {qualified('ledger')}"
                )
                assert await cur.fetchall() == [(principal.account_id, note)]
        async with pool.connection() as conn:
            assert await runtime_privilege_problems(conn, schemas=(SCHEMA,)) == []
    finally:
        await pool.close()


def test_restore_and_reprovisioning_reconstruct_role_and_grant_state():
    asyncio.run(run_scenario(_restore_scenario))


# ---------------------------------------------------------------------------
# Provisioning refusals.
# ---------------------------------------------------------------------------


async def _provision_validation_scenario(admin_pool):
    async with admin_pool.connection() as conn:
        with pytest.raises(ValueError, match="does not accept"):
            await provision(conn, schema="public")


def test_provisioning_refuses_a_configuration_that_would_leave_a_table_open():
    asyncio.run(run_scenario(_provision_validation_scenario, seed=False))
