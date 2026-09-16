"""Unit coverage for app/account_context.py's shape and refusals.

Everything here runs without a database. The parts that only a real
PostgreSQL can settle (what a transaction-local setting does, what a policy
lets through) live in tests/test_account_context_db.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect

import pytest
from psycopg.pq import TransactionStatus

from app import account_context
from app.account_context import (
    ACCOUNT_CONTEXT_EXPRESSION,
    ACCOUNT_CONTEXT_SETTING,
    AccountContextError,
    AccountDisabledError,
    AccountPrincipal,
    account_connection,
    apply_account_context,
    control_connection,
)


class _FakePool:
    """Records whether a connection was ever asked for."""

    def __init__(self) -> None:
        self.borrows = 0

    def connection(self):
        self.borrows += 1
        raise AssertionError("the pool must not be touched")


class _FakeInfo:
    def __init__(self, status) -> None:
        self.transaction_status = status


class _FakeConnection:
    def __init__(self, status) -> None:
        self.info = _FakeInfo(status)
        self.executed: list[tuple] = []

    async def execute(self, *args):
        self.executed.append(args)
        raise AssertionError("no statement may be sent outside a transaction")


def test_principal_is_frozen():
    principal = AccountPrincipal(account_id=7, enabled=True, auth_version=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.account_id = 8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": True, "enabled": True, "auth_version": 1},
        {"account_id": "7", "enabled": True, "auth_version": 1},
        {"account_id": 7, "enabled": 1, "auth_version": 1},
        {"account_id": 7, "enabled": True, "auth_version": True},
        {"account_id": 7, "enabled": True, "auth_version": "1"},
    ],
)
def test_principal_rejects_wrong_types(kwargs):
    """`account_id=True` would otherwise pass as account 1: bool is an int."""
    with pytest.raises(TypeError):
        AccountPrincipal(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": 0, "enabled": True, "auth_version": 1},
        {"account_id": 2**63, "enabled": True, "auth_version": 1},
        {"account_id": 7, "enabled": True, "auth_version": 2**63},
        {"account_id": -1, "enabled": True, "auth_version": 1},
        {"account_id": 7, "enabled": True, "auth_version": 0},
    ],
)
def test_principal_rejects_out_of_range_values(kwargs):
    with pytest.raises(ValueError):
        AccountPrincipal(**kwargs)


def test_context_expression_is_null_safe_and_names_the_agreed_setting():
    """The two-argument current_setting keeps an unset value from raising,
    and NULLIF keeps a reverted one (PostgreSQL leaves the empty string)
    from blowing up the bigint cast instead of denying quietly.
    """
    assert ACCOUNT_CONTEXT_SETTING == "app.account_id"
    assert "current_setting('app.account_id', true)" in ACCOUNT_CONTEXT_EXPRESSION
    assert ACCOUNT_CONTEXT_EXPRESSION.startswith("NULLIF(")
    assert ACCOUNT_CONTEXT_EXPRESSION.endswith("::bigint")


def test_apply_account_context_refuses_a_connection_outside_a_transaction():
    """The autocommit trap. `SET LOCAL` outside a transaction block is
    discarded at the end of the implicit single-statement transaction, so
    the call has to fail rather than report success and set nothing.
    """
    conn = _FakeConnection(TransactionStatus.IDLE)
    principal = AccountPrincipal(account_id=7, enabled=True, auth_version=1)
    with pytest.raises(AccountContextError) as error:
        asyncio.run(apply_account_context(conn, principal))
    assert "IDLE" in str(error.value)
    assert conn.executed == []


def test_apply_account_context_rejects_a_non_principal():
    conn = _FakeConnection(TransactionStatus.INTRANS)
    with pytest.raises(TypeError):
        asyncio.run(apply_account_context(conn, {"account_id": 7}))


def test_account_connection_refuses_a_disabled_principal_without_borrowing():
    pool = _FakePool()
    principal = AccountPrincipal(account_id=7, enabled=False, auth_version=1)

    async def run():
        async with account_connection(pool, principal):
            raise AssertionError("unreachable")

    with pytest.raises(AccountDisabledError):
        asyncio.run(run())
    assert pool.borrows == 0


def test_account_connection_refuses_a_non_principal_without_borrowing():
    pool = _FakePool()

    async def run():
        async with account_connection(pool, 7):
            raise AssertionError("unreachable")

    with pytest.raises(TypeError):
        asyncio.run(run())
    assert pool.borrows == 0


def test_there_is_no_escape_hatch_on_the_account_entry_point():
    """No default account, no all-accounts flag, no privileged fallback:
    the only way to reach account-free access is the separately named
    control entry point.
    """
    parameters = inspect.signature(account_connection).parameters
    assert list(parameters) == ["pool", "principal"]
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in parameters.values()
    )
    assert list(inspect.signature(control_connection).parameters) == ["pool"]
    assert account_connection is not control_connection


def test_module_holds_no_mutable_process_global_state():
    """A module-level dict, list, set, or ContextVar would be exactly the
    ambient "current account" this design refuses to have. Every principal
    is passed explicitly instead, so nothing can drift out of step with the
    connection actually in hand.
    """
    mutable_containers = {
        name: type(value).__name__
        for name, value in vars(account_context).items()
        if isinstance(value, (dict, list, set, bytearray))
        and not name.startswith("__")
    }
    assert mutable_containers == {}
    assert "contextvars" not in inspect.getsource(account_context)


def test_apply_context_rejects_disabled_principal_before_sql():
    conn = _FakeConnection(TransactionStatus.INTRANS)
    with pytest.raises(AccountDisabledError):
        asyncio.run(apply_account_context(conn, AccountPrincipal(7, False, 1)))
    assert not conn.executed
