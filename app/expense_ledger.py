"""Expense ledger reads and writes shared by the web pages and the API: the
ledger rows and totals, creates (with an optional idempotency claim),
partial updates and deletes.

Every function takes an account connection and leaves the commit to the
caller. Validation failures raise `ExpenseValidationError` with per-field
messages, in the order the web form checks them; an expense missing from
this account is a 404.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg import errors
from psycopg.rows import dict_row

from app.account_context import account_id
from app.expense_queries import EXPENSE_SELECT_JOIN, annotate_expense
from app.expenses import CENT, EXPENSE_CATEGORIES, EXPENSE_TREATMENTS, default_treatment


class ExpenseValidationError(ValueError):
    """Per-field validation messages for an expense write. The web pages
    answer the first one; the API returns them all as `validation_failed`."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(next(iter(errors.values())))
        self.errors = errors


FIELD_REQUIRED = "This field is required."
DATE_INVALID = "Invalid expense date"
CATEGORY_INVALID = "Invalid expense category"
AMOUNT_INVALID = "Invalid expense amount"
TREATMENT_REQUIRED = "Other expenses require a tax treatment"
TREATMENT_INVALID = "Invalid expense treatment"
TRIP_INVALID = "Invalid trip"
VEHICLE_MISSING = "No such vehicle"
TRIP_MISSING = "No such trip"
REFERENCE_INVALID = "Invalid expense reference"
NOT_FOUND = "No such expense"

MAX_AMOUNT = Decimal("9999999999.99")
EXPENSE_FIELDS = ("vehicle_id", "incurred_on", "category", "amount", "treatment", "notes", "trip_id")
REQUIRED_FIELDS = ("vehicle_id", "incurred_on", "category", "amount", "treatment")


def parse_expense_amount(text: str) -> Decimal | None:
    """The amount in whole cents, or None when it is not a positive amount
    of at most 9,999,999,999.99 with no more than two decimal places."""
    try:
        raw = Decimal(text)
        parsed = raw.quantize(CENT)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != raw or parsed <= 0 or parsed > MAX_AMOUNT:
        return None
    return parsed


def resolve_treatment(category: str, treatment: str | None) -> str | None:
    """An empty treatment takes the category's default; "other" has none.
    Returns None when the category needs an explicit treatment."""
    if not treatment:
        if category == "other":
            return None
        return default_treatment(category)
    return treatment


def _text(value: str | None) -> str | None:
    return None if value is None else (value.strip() or None)


async def _exists(conn, table: str, row_id: int) -> bool:
    cur = await conn.execute(
        f"SELECT 1 FROM {table} WHERE id = %s AND account_id = %s", (row_id, account_id(conn)))
    return await cur.fetchone() is not None


def _reference_field(exc: errors.ForeignKeyViolation) -> str:
    constraint = getattr(exc.diag, "constraint_name", None) or ""
    return "trip_id" if "trip" in constraint else "vehicle_id"


async def _normalize(conn, changes: Mapping[str, object], problems: dict[str, str]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for name, value in changes.items():
        if name not in EXPENSE_FIELDS:
            raise ValueError(f"unknown expense field {name!r}")
        if value is None and name in REQUIRED_FIELDS:
            problems[name] = FIELD_REQUIRED
            continue
        if name == "incurred_on":
            if not isinstance(value, date):
                problems[name] = DATE_INVALID
                continue
        elif name == "category":
            if value not in EXPENSE_CATEGORIES:
                problems[name] = CATEGORY_INVALID
                continue
        elif name == "amount":
            parsed = parse_expense_amount(str(value)) if isinstance(value, (str, Decimal)) else None
            if parsed is None:
                problems[name] = AMOUNT_INVALID
                continue
            value = parsed
        elif name == "treatment":
            if value not in EXPENSE_TREATMENTS:
                problems[name] = TREATMENT_INVALID
                continue
        elif name == "notes":
            value = _text(value)
        normalized[name] = value
    # Existence checks last, vehicle before trip, as the web form runs them.
    for name, table, missing in (("vehicle_id", "vehicles", VEHICLE_MISSING), ("trip_id", "trips", TRIP_MISSING)):
        value = normalized.get(name)
        if value is not None and not await _exists(conn, table, value):
            problems[name] = missing
            del normalized[name]
    return normalized


@dataclass(frozen=True)
class Expense:
    """A validated, normalized expense."""

    vehicle_id: int
    incurred_on: date
    category: str
    amount: Decimal
    treatment: str
    notes: str | None = None
    trip_id: int | None = None


async def validate_expense(conn, values: Mapping[str, object]) -> Expense:
    """Normalize a new expense, or raise with every invalid field. A missing
    `treatment` takes the category's default, which "other" does not have."""
    problems: dict[str, str] = {}
    present = {name: values.get(name) for name in EXPENSE_FIELDS}
    if present["treatment"] is None:
        category = present["category"]
        # Without a category there is no default to take; that field says so.
        present["treatment"] = resolve_treatment(category, None) if isinstance(category, str) else None
        if present["treatment"] is None:
            if category == "other":
                problems["treatment"] = TREATMENT_REQUIRED
            del present["treatment"]
    normalized = await _normalize(conn, present, problems)
    if problems:
        raise ExpenseValidationError(problems)
    return Expense(**normalized)


def expense_digest(expense: Expense) -> bytes:
    """SHA-256 of the canonical validated request: sorted field names and
    normalized values, so requests that mean the same expense match."""
    canonical = {
        "amount": str(expense.amount), "category": expense.category,
        "incurred_on": expense.incurred_on.isoformat(), "notes": expense.notes,
        "treatment": expense.treatment, "trip_id": expense.trip_id, "vehicle_id": expense.vehicle_id,
    }
    text = json.dumps({"resource": "expenses", "request": canonical}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).digest()


_INSERT = (
    "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, notes, trip_id{keys}) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s{values})"
)


def _insert_params(conn, expense: Expense) -> tuple:
    return (account_id(conn), expense.vehicle_id, expense.incurred_on, expense.category, expense.amount,
            expense.treatment, expense.notes, expense.trip_id)


async def insert_expense(conn, expense: Expense) -> None:
    """Insert an expense without an idempotency key, as the web form does."""
    try:
        async with conn.transaction():
            await conn.execute(_INSERT.format(keys="", values=""), _insert_params(conn, expense))
    except errors.ForeignKeyViolation as exc:
        raise ExpenseValidationError({_reference_field(exc): REFERENCE_INVALID}) from None


class IdempotencyKeyReused(Exception):
    """The key already created an expense from a different request."""


async def create_expense(conn, expense: Expense, key: uuid.UUID, digest: bytes) -> tuple[int, bool]:
    """Create an expense once per idempotency key.

    Returns `(expense_id, created)`. A key this account already used for the
    same request returns that expense with `created` False and writes
    nothing; one used for a different request raises `IdempotencyKeyReused`.
    A concurrent create with the same key waits on the unique index until the
    first transaction ends, then replays its expense, or inserts if it rolled
    back. Deleting the expense frees its key.
    """
    statement = _INSERT.format(keys=", idempotency_key, idempotency_digest", values=", %s, %s") + (
        " ON CONFLICT (account_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING id")
    for _ in range(3):
        try:
            async with conn.transaction():
                cur = await conn.execute(statement, (*_insert_params(conn, expense), key, digest))
                row = await cur.fetchone()
        except errors.ForeignKeyViolation as exc:
            raise ExpenseValidationError({_reference_field(exc): REFERENCE_INVALID}) from None
        if row is not None:
            return row[0], True
        cur = await conn.execute(
            "SELECT id, idempotency_digest FROM expenses WHERE account_id = %s AND idempotency_key = %s",
            (account_id(conn), key),
        )
        existing = await cur.fetchone()
        if existing is None:
            # Deleted after the conflict was seen; the key is free again.
            continue
        if bytes(existing[1]) != digest:
            raise IdempotencyKeyReused()
        return existing[0], False
    raise RuntimeError("idempotent create did not settle")


async def update_expense(
    conn, expense_id: int, changes: Mapping[str, object], *, write_unchanged: bool = False,
) -> bool:
    """Apply the present fields of `changes` to an expense.

    Omitted fields are left alone; `None` clears `notes` or `trip_id` and is
    refused for a required field. Notes are trimmed, and empty means `None`.
    The fields are validated before the expense is looked up, as the web form
    does, so an invalid change is refused the same way whether or not the
    expense exists. Without `write_unchanged`, a request that changes nothing
    issues no UPDATE, so `updated_at` stays as it is; the web edit form passes
    it because every save there refreshes `updated_at`. Returns whether a row
    was written.
    """
    problems: dict[str, str] = {}
    normalized = await _normalize(conn, changes, problems)
    if problems:
        raise ExpenseValidationError(problems)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT vehicle_id, incurred_on, category::text AS category, amount, treatment::text AS treatment, "
        "notes, trip_id FROM expenses WHERE id = %s AND account_id = %s FOR UPDATE",
        (expense_id, account_id(conn)),
    )
    stored = await cur.fetchone()
    if stored is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    written = normalized if write_unchanged else {
        name: value for name, value in normalized.items() if value != stored[name]}
    if not written and not write_unchanged:
        return False
    clauses = [f"{name} = %s" for name in written] + ["updated_at = now()"]
    try:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE expenses SET {', '.join(clauses)} WHERE id = %s AND account_id = %s",
                (*written.values(), expense_id, account_id(conn)),
            )
    except errors.ForeignKeyViolation as exc:
        raise ExpenseValidationError({_reference_field(exc): REFERENCE_INVALID}) from None
    return True


async def delete_expense(conn, expense_id: int) -> None:
    cur = await conn.execute(
        "DELETE FROM expenses WHERE id = %s AND account_id = %s", (expense_id, account_id(conn)))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=NOT_FOUND)


# ---------------------------------------------------------------------------
# Reads


async def fetch_expense(conn, expense_id: int) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        EXPENSE_SELECT_JOIN + "WHERE expenses.id = %s AND expenses.account_id = %s",
        (expense_id, account_id(conn)),
    )
    return await cur.fetchone()


async def fetch_expense_rows(
    conn, start: date, end: date, *, vehicle_id: int | None = None, descending: bool = True,
    after: tuple[date, int] | None = None, limit: int | None = None,
) -> list[dict]:
    """Expense rows with `start <= incurred_on < end`, ordered by
    `(incurred_on, id)`, newest first unless `descending` is False. `after`
    continues a newest-first keyset page past that row."""
    clauses = ["expenses.account_id = %s", "expenses.incurred_on >= %s", "expenses.incurred_on < %s"]
    params: list = [account_id(conn), start, end]
    if vehicle_id is not None:
        clauses.append("expenses.vehicle_id = %s")
        params.append(vehicle_id)
    if after is not None:
        clauses.append("(expenses.incurred_on, expenses.id) < (%s, %s)")
        params.extend(after)
    direction = " DESC" if descending else ""
    statement = (EXPENSE_SELECT_JOIN + "WHERE " + " AND ".join(clauses)
                 + f" ORDER BY expenses.incurred_on{direction}, expenses.id{direction}")
    if limit is not None:
        statement += " LIMIT %s"
        params.append(limit)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(statement, params)
    return await cur.fetchall()


async def fetch_expense_page(
    conn, year: int, after: tuple[date, int] | None, limit: int,
) -> tuple[list[dict], bool]:
    """One newest-first keyset page of a year's expenses, and whether more follow."""
    rows = await fetch_expense_rows(
        conn, date(year, 1, 1), date(year + 1, 1, 1), after=after, limit=limit + 1)
    return rows[:limit], len(rows) > limit


async def fetch_trip_choices(conn) -> list[dict]:
    """Every trip, newest first, for linking an expense on the web form."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT trips.id, trips.started_at, trips.ended_at, trips.vehicle_id, "
        "trips.exclusion::text AS exclusion, vehicles.name AS vehicle_name FROM trips "
        "LEFT JOIN vehicles ON vehicles.id = trips.vehicle_id AND vehicles.account_id = trips.account_id "
        "WHERE trips.account_id = %s ORDER BY trips.started_at DESC, trips.id DESC",
        (account_id(conn),),
    )
    return await cur.fetchall()


@dataclass(frozen=True)
class ExpenseLedger:
    expenses: list[dict]
    trips: list[dict]
    category_totals: dict[str, Decimal]
    total: Decimal


def ledger_totals(expenses: list[dict]) -> tuple[dict[str, Decimal], Decimal]:
    category_totals: dict[str, Decimal] = {}
    for expense in expenses:
        category_totals[expense["category"]] = (
            category_totals.get(expense["category"], Decimal("0")) + expense["amount"]
        )
    return category_totals, sum(category_totals.values(), Decimal("0"))


async def fetch_ledger(conn, year: int, vehicle_id: int | None, tz: ZoneInfo) -> ExpenseLedger:
    """A year's ledger as the web page shows it: rows newest first with
    their conflict labels, the trips they can link to, and the totals."""
    rows = await fetch_expense_rows(conn, date(year, 1, 1), date(year + 1, 1, 1), vehicle_id=vehicle_id)
    expenses = [annotate_expense(row, tz) for row in rows]
    trips = await fetch_trip_choices(conn)
    category_totals, total = ledger_totals(expenses)
    return ExpenseLedger(expenses, trips, category_totals, total)
