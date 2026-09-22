"""Account context primitives for account-scoped database access.

The application binds personal work to an immutable principal and the
transaction-local database setting every row-level security policy reads,
with a separate control entry point for identity work and a privilege check
that refuses roles capable of defeating those policies. Personal queries
also scope rows explicitly while live policy activation remains staged.

Two properties are load-bearing and easy to get wrong.

First, the context is applied with ``set_config(..., is_local => true)``,
the function form of ``SET LOCAL``. PostgreSQL discards a transaction-local
setting when the surrounding transaction ends, which is what keeps a
pooled connection from carrying one account's context into the next
borrower's work. A plain session ``SET`` would survive the return to the
pool: psycopg_pool only rolls back an open transaction when a connection
comes back, it does not issue ``DISCARD ALL``.

Second, ``SET LOCAL`` outside a transaction block is a silent no-op. In
autocommit each statement is its own implicit transaction, so the setting
is reverted before the next statement can read it, while ``set_config``
still cheerfully returns the value it just set. That failure mode looks
like success at the call site and like a total loss of access at the
policy, so `apply_account_context` checks the connection is actually
inside a transaction block before it sets anything.

The policy expression pairs with all of that: `ACCOUNT_CONTEXT_EXPRESSION`
maps both an unset setting and a reverted one (PostgreSQL leaves the empty
string behind, not NULL, once a setting has been set and rolled back) onto
SQL NULL, so the comparison is NULL rather than true and the policy denies.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Any

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

# Text-valued custom setting carrying the authenticated account. Read with
# the two-argument current_setting(..., missing_ok => true) form so an
# unset value is NULL instead of an error, then cast to bigint.
ACCOUNT_CONTEXT_SETTING = "app.account_id"

# The exact expression row-level security policies compare against. NULLIF
# is not decoration: once a transaction-local setting has been set and
# reverted, current_setting returns the empty string, and ''::bigint raises
# instead of yielding NULL.
ACCOUNT_CONTEXT_EXPRESSION = (
    f"NULLIF(current_setting('{ACCOUNT_CONTEXT_SETTING}', true), '')::bigint"
)

# Database role contract. The application process connects as one of the
# two restricted roles and never as the owner; see
# scripts/sql/provision_roles.sql and docs/configuration.md.
MIGRATE_ROLE = "odograph_migrate"
CONTROL_ROLE = "odograph_control"
RUNTIME_ROLE = "odograph_runtime"


class AccountContextError(RuntimeError):
    """The account context could not be applied, or is not trustworthy."""


class AccountDisabledError(AccountContextError):
    """A disabled principal gets no connection at all."""


class RuntimePrivilegeError(RuntimeError):
    """The connected role holds privileges that defeat row-level security."""


@dataclass(frozen=True, slots=True)
class AccountPrincipal:
    """The authenticated account, established once and never mutated.

    Frozen and slotted on purpose. A principal is passed explicitly to
    every helper below; there is no process-global "current account" to
    fall out of step with the connection actually in hand, and no way to
    retarget a principal after an authorization decision was made from it.
    """

    account_id: int
    enabled: bool
    auth_version: int

    def __post_init__(self) -> None:
        # bool is a subclass of int, so a bare isinstance check would let
        # AccountPrincipal(True, ...) through as account 1.
        if isinstance(self.account_id, bool) or not isinstance(self.account_id, int):
            raise TypeError("account_id must be an int")
        if not 1 <= self.account_id <= 2**63 - 1:
            raise ValueError("account_id must fit a positive bigint")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        if isinstance(self.auth_version, bool) or not isinstance(self.auth_version, int):
            raise TypeError("auth_version must be an int")
        if not 1 <= self.auth_version <= 2**63 - 1:
            raise ValueError("auth_version must fit a positive bigint")


@dataclass(frozen=True, slots=True)
class AccountConnection:
    """A transaction and its immutable authenticated owner.

    SQL must still filter by this owner. The wrapper makes accidentally
    passing a control connection to a personal helper a visible error.
    """

    _connection: AsyncConnection
    principal: AccountPrincipal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    async def commit(self) -> None:
        raise AccountContextError("the account connection owns the transaction")

    async def rollback(self) -> None:
        raise AccountContextError("the account connection owns the transaction")


def account_id(conn: AccountConnection) -> int:
    if not isinstance(conn, AccountConnection):
        raise AccountContextError("personal data requires an account connection")
    return conn.principal.account_id


@dataclass(frozen=True, slots=True)
class AccountPool:
    """Bind a restricted runtime pool to one validated principal."""

    runtime_pool: AsyncConnectionPool
    principal: AccountPrincipal

    @asynccontextmanager
    async def connection(self, *, timeout=None) -> AsyncIterator[AccountConnection]:
        async with _account_connection(self.runtime_pool, self.principal, timeout=timeout) as conn:
            # A shared row lock blocks disablement/password version changes
            # until this unit of work commits. Runtime cannot mutate accounts.
            await conn.execute(
                "SELECT public.assert_account_active(%s, %s)",
                (self.principal.account_id, self.principal.auth_version),
            )
            yield AccountConnection(conn, self.principal)

    def get_stats(self):
        return self.runtime_pool.get_stats()


async def apply_account_context(
    conn: AsyncConnection, principal: AccountPrincipal
) -> None:
    """Set the account context transaction-locally on an open transaction.

    Raises `AccountContextError` unless the connection is already inside a
    transaction block. Without that check the call would succeed loudly and
    do nothing: PostgreSQL discards a transaction-local setting at the end
    of the implicit single-statement transaction an autocommit connection
    wraps it in.
    """
    if not isinstance(principal, AccountPrincipal):
        raise TypeError("principal must be an AccountPrincipal")
    if not principal.enabled:
        raise AccountDisabledError("account is not enabled")
    status = conn.info.transaction_status
    if status != TransactionStatus.INTRANS:
        raise AccountContextError(
            f"cannot apply {ACCOUNT_CONTEXT_SETTING} with transaction status "
            f"{TransactionStatus(status).name}: a transaction-local setting "
            "applied outside a transaction block is silently discarded"
        )
    expected = str(principal.account_id)
    cur = await conn.execute(
        "SELECT set_config(%s, %s, true)", (ACCOUNT_CONTEXT_SETTING, expected)
    )
    applied = (await cur.fetchone())[0]
    if applied != expected:
        raise AccountContextError(
            f"{ACCOUNT_CONTEXT_SETTING} did not take the requested value"
        )


async def current_account_context(conn: AsyncConnection) -> int | None:
    """Return the account id currently in context, or None if there is none.

    Never raises on an unset or reverted setting, so a caller can use this
    to assert the absence of a context. A setting holding something that is
    not a plain decimal integer is a corrupted context, not an absent one,
    and raises.
    """
    cur = await conn.execute(
        "SELECT current_setting(%s, true)", (ACCOUNT_CONTEXT_SETTING,)
    )
    raw = (await cur.fetchone())[0]
    if raw is None or raw == "":
        return None
    # str.isdigit alone accepts non-ASCII digits, and int() accepts
    # underscore separators PostgreSQL's bigint cast would reject. The
    # value itself is deliberately left out of the message.
    if not (raw.isascii() and raw.isdigit()):
        raise AccountContextError(
            f"{ACCOUNT_CONTEXT_SETTING} holds a value that is not an account id"
        )
    if len(raw) > 19 or not 1 <= int(raw) <= 2**63 - 1:
        raise AccountContextError(f"{ACCOUNT_CONTEXT_SETTING} is outside the account id range")
    return int(raw)


@asynccontextmanager
async def account_connection(
    pool: AsyncConnectionPool, principal: AccountPrincipal,
) -> AsyncIterator[AsyncConnection]:
    """Borrow an account transaction with a required explicit principal."""
    async with _account_connection(pool, principal) as conn:
        yield conn


@asynccontextmanager
async def _account_connection(
    pool: AsyncConnectionPool, principal: AccountPrincipal, *, timeout=None,
) -> AsyncIterator[AsyncConnection]:
    """Borrow a connection, open a transaction, and scope it to one account.

    The yielded connection is inside a transaction that commits on a clean
    exit and rolls back on an exception. Either way the account context
    goes away with the transaction, so the next borrower of the same pooled
    connection starts with no context.

    There is no default account, no "every account" mode, and no fallback
    to a privileged connection. A caller that needs to work outside a
    single account's rows uses `control_connection` explicitly.
    """
    if not isinstance(principal, AccountPrincipal):
        raise TypeError("principal must be an AccountPrincipal")
    if not principal.enabled:
        raise AccountDisabledError(
            f"account {principal.account_id} is not enabled"
        )
    async with pool.connection(**({"timeout": timeout} if timeout is not None else {})) as conn:
        async with conn.transaction():
            await apply_account_context(conn, principal)
            yield conn


@asynccontextmanager
async def control_connection(
    pool: AsyncConnectionPool,
) -> AsyncIterator[AsyncConnection]:
    """Borrow a connection for identity and bootstrap work, with no context.

    Deliberately a separate entry point rather than a flag on
    `account_connection`: the two have different database roles, different
    grants, and different reviewers' expectations, and a boolean argument
    would let a caller reach account-free access by accident.

    Opens no transaction of its own. Callers that need atomicity, such as
    first-account bootstrap, open one explicitly around their own work.
    """
    async with pool.connection() as conn:
        leaked = await current_account_context(conn)
        if leaked is not None:
            raise AccountContextError(
                f"{ACCOUNT_CONTEXT_SETTING} is set on a connection borrowed "
                "for control work; the context was not applied transaction-locally"
            )
        yield conn


async def runtime_privilege_problems(
    conn: AsyncConnection, *, schemas: tuple[str, ...] = ("public",)
) -> list[str]:
    """Return every reason the connected role is unsafe for account runtime use.

    Effective privileges, not declared ones: role attributes are collected
    across every role the current user is a member of, whether inherited or
    reachable with SET ROLE, and object rights are read from the catalog's
    own has_*_privilege functions rather than from a grant list.
    """
    problems: list[str] = []
    schema_list = list(schemas)

    cur = await conn.execute(
        "SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreatedb, "
        "       r.rolcreaterole, r.rolreplication "
        "FROM pg_roles r "
        "WHERE pg_has_role(current_user, r.oid, 'MEMBER') "
        "ORDER BY r.rolname"
    )
    for name, superuser, bypassrls, createdb, createrole, replication in (
        await cur.fetchall()
    ):
        if superuser:
            problems.append(f"role {name} is a superuser")
        if bypassrls:
            problems.append(f"role {name} has BYPASSRLS")
        if createdb:
            problems.append(f"role {name} has CREATEDB")
        if createrole:
            problems.append(f"role {name} has CREATEROLE")
        if replication:
            problems.append(f"role {name} has REPLICATION")

    cur = await conn.execute(
        "SELECT r.rolname FROM pg_roles r WHERE r.rolname <> current_user "
        "AND pg_has_role(current_user,r.oid,'MEMBER')"
    )
    for (name,) in await cur.fetchall():
        problems.append(f"can assume another role {name}")

    cur = await conn.execute(
        "SELECT n.nspname, c.relname "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s) "
        "  AND pg_has_role(current_user, c.relowner, 'MEMBER') "
        "ORDER BY n.nspname, c.relname",
        (schema_list,),
    )
    for schema, table in await cur.fetchall():
        problems.append(f"owns protected table {schema}.{table}")

    cur = await conn.execute(
        "SELECT n.nspname, c.relname "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s) "
        "  AND has_table_privilege(current_user, c.oid, 'TRUNCATE') "
        "ORDER BY n.nspname, c.relname",
        (schema_list,),
    )
    for schema, table in await cur.fetchall():
        problems.append(f"can truncate protected table {schema}.{table}")

    cur = await conn.execute(
        "SELECT n.nspname FROM pg_namespace n "
        "WHERE n.nspname = ANY(%s) "
        "  AND has_schema_privilege(current_user, n.oid, 'CREATE') "
        "ORDER BY n.nspname",
        (schema_list,),
    )
    for (schema,) in await cur.fetchall():
        problems.append(f"can create objects in schema {schema}")

    cur = await conn.execute(
        "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
    )
    if (await cur.fetchone())[0]:
        problems.append("can create schemas in the current database")

    return problems


async def check_runtime_privileges(
    conn: AsyncConnection, *, schemas: tuple[str, ...] = ("public",)
) -> None:
    """Raise `RuntimePrivilegeError` if the connected role is unsafe.

    Not wired into application startup by this change. It exists so the
    wiring, when it lands, has a checked primitive to call rather than an
    inline query written under deadline.
    """
    problems = await runtime_privilege_problems(conn, schemas=schemas)
    if problems:
        raise RuntimePrivilegeError(
            "the connected database role can defeat account row-level "
            "security: " + "; ".join(problems)
        )
