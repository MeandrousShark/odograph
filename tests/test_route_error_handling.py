"""Operator-facing 400s, not 500s, for junk input on two routes that
previously let an uncaught ValueError/InvalidTextRepresentation escape to a
generic 500: create_rule (non-numeric place id) and expense_ledger
(the "none"/unassigned vehicle sentinel, which is meaningless for expenses
since expenses.vehicle_id is NOT NULL). No database is touched by either
scenario below -- both fail before the first query.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.ui import make_router

USER = {"sub": "test"}


def _endpoint(path: str):
    for route in make_router().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} missing")


def _request():
    # A pool that would raise loudly if a scenario ever reached it -- both
    # tests below must fail validation before any query is attempted.
    def _unexpected_pool_use(*_args, **_kwargs):
        raise AssertionError("scenario reached the database; expected to fail before any query")

    pool = SimpleNamespace(connection=_unexpected_pool_use)
    # expense_ledger reads request.app.state.config.display_tz before the
    # vehicle_id check this test exercises -- a real (if minimal) config is
    # needed even though the scenario never reaches the database.
    config = SimpleNamespace(display_tz=ZoneInfo("UTC"))
    state = SimpleNamespace(pool=pool, config=config)
    return SimpleNamespace(state=SimpleNamespace(account_pool=pool, config=config), app=SimpleNamespace(state=state), headers={})


def test_create_rule_rejects_a_non_numeric_place():
    create_rule = _endpoint("/rules")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(
            _request(), a_mode="place", a_kind="", a_place="abc",
            b_mode="kind", b_kind="other", b_place="",
            category="business", user=USER,
        ))
    assert exc.value.status_code == 400


def test_expense_ledger_rejects_the_unassigned_vehicle_sentinel():
    expense_ledger = _endpoint("/expenses")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(expense_ledger(_request(), year=2026, vehicle="none", user=USER))
    assert exc.value.status_code == 400
