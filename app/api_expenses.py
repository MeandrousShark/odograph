"""API v1 expenses: the ledger, creates, edits and deletes, and the year's
actual-expense comparison.

Every read and write goes through the services the web pages use. Requests
accept only documented values: dates are `YYYY-MM-DD` in years 1900 through
9998, money is a plain decimal string, and anything else is
`validation_failed`. Responses carry money as decimal strings with two
places.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse, Response

from app.api_cursor import decode_cursor, encode_cursor, filter_digest
from app.api_common import (
    IDEMPOTENCY_KEY_HEADER, IDEMPOTENT_REPLAYED_HEADER, MAX_YEAR, MIN_YEAR, error_responses, year_in_range,
)
from app.api_common import idempotency_key as parse_idempotency_key
from app.api_errors import FIELD_INVALID, ApiError
from app.api_v1 import ApiUser, require_api_user
from app.capacity_routes import AdmissionRoute, capacity_policy, mark_committed_write
from app.expense_ledger import (
    AMOUNT_INVALID, DATE_INVALID, TRIP_MISSING, VEHICLE_MISSING, ExpenseValidationError,
    IdempotencyKeyReused, create_expense, delete_expense, expense_digest, fetch_expense, fetch_expense_page,
    update_expense, validate_expense,
)
from app.expenses import EXPENSE_CATEGORIES, ExpenseReport, expense_conflicts
from app.report_data import fetch_expense_comparison

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# Digits with an optional fraction; the shared rule then limits it to whole
# cents within the column's range.
_AMOUNT = re.compile(r"\d{1,20}(?:\.\d{1,20})?")
_MAX_ID = 2**63 - 1

ExpenseCategory = Literal[EXPENSE_CATEGORIES]
Treatment = Literal["business_use_allocated", "fully_business"]


class Expense(BaseModel):
    id: int
    vehicle_id: int
    vehicle_name: str
    incurred_on: str
    category: str
    amount: str
    treatment: str
    notes: str | None
    trip_id: int | None
    conflicts: list[str]


class ExpensePage(BaseModel):
    items: list[Expense]
    next_cursor: str | None


class _WriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExpenseCreate(_WriteBody):
    """A new expense. An omitted `treatment` takes the category's default;
    `other` has none and needs one. `trip_id` links a trip of this account."""

    vehicle_id: int
    incurred_on: str
    category: ExpenseCategory
    amount: str
    treatment: Treatment = None
    notes: str | None = None
    trip_id: int | None = None


class ExpensePatch(_WriteBody):
    """Only the fields present change. `null` clears `notes` or `trip_id`;
    the other fields cannot be null."""

    vehicle_id: int = None
    incurred_on: str = None
    category: ExpenseCategory = None
    amount: str = None
    treatment: Treatment = None
    notes: str | None = None
    trip_id: int | None = None


class CategoryAmount(BaseModel):
    category: str
    amount: str


class ComparisonLine(BaseModel):
    vehicle_id: int
    vehicle_name: str
    business_m: float
    denominator_m: float
    denominator_source: str
    provisional: bool
    invalid_odometer: bool
    business_share: float | None
    allocated_expenses: str
    fully_business_expenses: str
    standard_total: str | None
    actual_total: str | None
    difference: str | None
    larger_estimate: str | None


class ExpenseComparison(BaseModel):
    year: int
    vehicles: list[ComparisonLine]
    category_totals: list[CategoryAmount]
    allocated_total: str
    fully_business_total: str
    ledger_total: str


def money(value: Decimal | float | None) -> str | None:
    return None if value is None else format(value, ".2f")


def api_day(field: str, value: str) -> date:
    """A `YYYY-MM-DD` date in the accepted years, or `validation_failed`."""
    parsed = None
    if _DAY.fullmatch(value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            pass
    if parsed is None or not year_in_range(parsed.year):
        raise ApiError("validation_failed", fields={field: FIELD_INVALID})
    return parsed


def current_year(request: Request) -> int:
    return datetime.now(request.state.config.display_tz).year


def _write_values(values: dict) -> dict:
    """Parse dates and amounts, and refuse NUL, which no stored text can
    hold, and ids no stored row can have."""
    problems = {}
    for name, value in values.items():
        if isinstance(value, str) and "\x00" in value:
            problems[name] = FIELD_INVALID
    for name, missing in (("vehicle_id", VEHICLE_MISSING), ("trip_id", TRIP_MISSING)):
        if values.get(name) is not None and not 1 <= values[name] <= _MAX_ID:
            problems[name] = missing
    if isinstance(values.get("incurred_on"), str) and "incurred_on" not in problems:
        try:
            values["incurred_on"] = api_day("incurred_on", values["incurred_on"])
        except ApiError:
            problems["incurred_on"] = DATE_INVALID
    if isinstance(values.get("amount"), str) and "amount" not in problems:
        if not _AMOUNT.fullmatch(values["amount"]):
            problems["amount"] = AMOUNT_INVALID
        else:
            values["amount"] = Decimal(values["amount"])
    if problems:
        raise ApiError("validation_failed", fields=problems)
    return values


def _expense_id(expense_id: int) -> int:
    if not 1 <= expense_id <= _MAX_ID:
        raise ApiError("not_found")
    return expense_id


def expense_item(row: dict, tz) -> Expense:
    return Expense(
        id=row["id"], vehicle_id=row["vehicle_id"], vehicle_name=row["vehicle_name"],
        incurred_on=row["incurred_on"].isoformat(), category=row["category"], amount=money(row["amount"]),
        treatment=row["treatment"], notes=row["notes"], trip_id=row["trip_id"],
        conflicts=list(expense_conflicts(row, tz)),
    )


def _comparison(year: int, report: ExpenseReport) -> ExpenseComparison:
    return ExpenseComparison(
        year=year,
        vehicles=[
            ComparisonLine(
                vehicle_id=line.vehicle_id, vehicle_name=line.vehicle_name, business_m=line.business_m,
                denominator_m=line.denominator_m, denominator_source=line.denominator_source,
                provisional=line.provisional, invalid_odometer=line.invalid_odometer,
                business_share=None if line.business_pct is None else float(line.business_pct),
                allocated_expenses=money(line.allocated_expenses),
                fully_business_expenses=money(line.fully_business_expenses),
                standard_total=money(line.standard_total), actual_total=money(line.actual_total),
                difference=money(line.difference), larger_estimate=line.larger_estimate,
            )
            for line in report.comparisons
        ],
        category_totals=[
            CategoryAmount(category=category, amount=money(report.category_totals[category]))
            for category in EXPENSE_CATEGORIES if category in report.category_totals
        ],
        allocated_total=money(report.allocated_total),
        fully_business_total=money(report.fully_business_total),
        ledger_total=money(report.ledger_total),
    )


def _cursor_position(day: date, expense_id: int) -> tuple[datetime, int]:
    return datetime.combine(day, time(), timezone.utc), expense_id


def make_router() -> APIRouter:
    router = APIRouter(route_class=AdmissionRoute)

    @router.get("/api/v1/expenses", response_model=ExpensePage, responses=error_responses(400, 401, 422, 429, 503))
    @capacity_policy("foreground")
    async def list_expenses(
        request: Request,
        api_user: ApiUser = Depends(require_api_user),
        year: int | None = Query(None, ge=MIN_YEAR, le=MAX_YEAR),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        cursor: str | None = Query(None),
    ):
        tz = request.state.config.display_tz
        year = year or current_year(request)
        digest = filter_digest("expenses", {"year": str(year)})
        after = None
        if cursor is not None:
            position, expense_id = decode_cursor("expenses", cursor, digest)
            if position.time() != time() or position.year != year:
                raise ApiError("invalid_cursor")
            after = (position.date(), expense_id)
        async with request.state.account_pool.connection() as conn:
            rows, more = await fetch_expense_page(conn, year, after, limit)
        next_cursor = None
        if more:
            next_cursor = encode_cursor(
                "expenses", _cursor_position(rows[-1]["incurred_on"], rows[-1]["id"]), digest)
        return ExpensePage(items=[expense_item(row, tz) for row in rows], next_cursor=next_cursor)

    # Registered ahead of the {expense_id} routes, so "comparison" is never an id.
    @router.get("/api/v1/expenses/comparison", response_model=ExpenseComparison,
                responses=error_responses(401, 422, 429, 503))
    @capacity_policy("foreground")
    async def comparison(
        request: Request,
        year: int = Query(ge=MIN_YEAR, le=MAX_YEAR),
        api_user: ApiUser = Depends(require_api_user),
    ):
        tz = request.state.config.display_tz
        async with request.state.account_pool.connection() as conn:
            report = await fetch_expense_comparison(conn, year, tz)
        return _comparison(year, report)

    @router.post("/api/v1/expenses", status_code=201, response_model=Expense,
                 responses={200: {"model": Expense, "description": "Replayed"},
                            **error_responses(401, 409, 413, 422, 429, 503)})
    @capacity_policy("form")
    async def create(
        request: Request, body: ExpenseCreate,
        idempotency_key: str = Header(alias=IDEMPOTENCY_KEY_HEADER),
        api_user: ApiUser = Depends(require_api_user),
    ):
        tz = request.state.config.display_tz
        key = parse_idempotency_key(idempotency_key)
        values = _write_values(body.model_dump())
        async with request.state.account_pool.connection() as conn:
            try:
                expense = await validate_expense(conn, values)
                expense_id, created = await create_expense(conn, expense, key, expense_digest(expense))
            except ExpenseValidationError as exc:
                raise ApiError("validation_failed", fields=exc.errors) from None
            except IdempotencyKeyReused:
                raise ApiError("idempotency_key_reused") from None
            row = await fetch_expense(conn, expense_id)
        content = expense_item(row, tz).model_dump(mode="json")
        if not created:
            return JSONResponse(content, status_code=200, headers={IDEMPOTENT_REPLAYED_HEADER: "true"})
        mark_committed_write(request)
        return JSONResponse(content, status_code=201, headers={"Location": f"/api/v1/expenses/{expense_id}"})

    @router.patch("/api/v1/expenses/{expense_id}", response_model=Expense,
                  responses=error_responses(401, 404, 409, 413, 422, 429, 503))
    @capacity_policy("form")
    async def patch(request: Request, expense_id: int, body: ExpensePatch,
                    api_user: ApiUser = Depends(require_api_user)):
        tz = request.state.config.display_tz
        _expense_id(expense_id)
        changes = _write_values(body.model_dump(include=body.model_fields_set))
        async with request.state.account_pool.connection() as conn:
            try:
                written = await update_expense(conn, expense_id, changes)
            except ExpenseValidationError as exc:
                raise ApiError("validation_failed", fields=exc.errors) from None
            row = await fetch_expense(conn, expense_id)
        if written:
            mark_committed_write(request)
        return expense_item(row, tz)

    @router.delete("/api/v1/expenses/{expense_id}", status_code=204, response_class=Response,
                   responses=error_responses(401, 404, 422, 429, 503))
    # No lane, like the web delete, which posts no form.
    async def remove(request: Request, expense_id: int, api_user: ApiUser = Depends(require_api_user)):
        _expense_id(expense_id)
        async with request.state.account_pool.connection() as conn:
            await delete_expense(conn, expense_id)
        mark_committed_write(request)
        return Response(status_code=204)

    return router
