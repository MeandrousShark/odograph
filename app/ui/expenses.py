from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from psycopg import errors
from psycopg.rows import dict_row
from starlette.responses import Response

from app.auth import require_csrf, require_user
from app.page import render_page
from app.expenses import (
    CATEGORY_LABELS,
    EXPENSE_CONFLICT_LABELS,
    EXPENSE_CATEGORIES,
    EXPENSE_TREATMENTS,
    TREATMENT_LABELS,
    default_treatment,
    expense_conflicts,
)
from app.vehicles import list_vehicles

from app.ui._common import VEHICLE_FILTER_UNASSIGNED, _parse_vehicle_id

_EXPENSE_SELECT_JOIN = (
    "SELECT expenses.id, expenses.vehicle_id, vehicles.name AS vehicle_name, "
    "expenses.incurred_on, expenses.category::text AS category, expenses.amount, "
    "expenses.treatment::text AS treatment, expenses.notes, expenses.trip_id, "
    "trips.vehicle_id AS trip_vehicle_id, trips.started_at AS trip_started_at, "
    "trips.exclusion::text AS trip_exclusion "
    "FROM expenses JOIN vehicles ON vehicles.id = expenses.vehicle_id "
    "LEFT JOIN trips ON trips.id = expenses.trip_id "
)


def _parse_expense_input(
    incurred_on: str, category: str, amount: str, treatment: str,
    trip_id: str | None = None,
) -> tuple[date, str, Decimal, str]:
    try:
        parsed_date = date.fromisoformat(incurred_on)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid expense date")
    if category not in EXPENSE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid expense category")
    try:
        raw_amount = Decimal(amount)
        parsed_amount = raw_amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Invalid expense amount")
    if (
        not parsed_amount.is_finite() or parsed_amount != raw_amount
        or parsed_amount <= 0 or parsed_amount > Decimal("9999999999.99")
    ):
        raise HTTPException(status_code=400, detail="Invalid expense amount")
    if not treatment:
        if category == "other":
            raise HTTPException(status_code=400, detail="Other expenses require a tax treatment")
        treatment = default_treatment(category)
    if treatment not in EXPENSE_TREATMENTS:
        raise HTTPException(status_code=400, detail="Invalid expense treatment")
    if isinstance(trip_id, str) and trip_id:
        try:
            parsed_trip_id = int(trip_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid trip")
        if parsed_trip_id < 1:
            raise HTTPException(status_code=400, detail="Invalid trip")
    return parsed_date, category, parsed_amount, treatment


def _parse_optional_trip_id(trip_id: str | None) -> int | None:
    if not isinstance(trip_id, str) or not trip_id:
        return None
    try:
        parsed = int(trip_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid trip")
    if parsed < 1:
        raise HTTPException(status_code=400, detail="Invalid trip")
    return parsed


def _annotate_expense(row: dict, tz) -> dict:
    annotated = dict(row)
    annotated["conflicts"] = [
        EXPENSE_CONFLICT_LABELS[code] for code in expense_conflicts(annotated, tz)
    ]
    return annotated


def register(router: APIRouter) -> None:
        @router.get("/expenses")
        async def expense_ledger(
            request: Request,
            year: int | None = Query(None, ge=1, le=9998),
            vehicle: str = Query(""),
            user: dict = Depends(require_user),
        ):
            tz = request.app.state.config.display_tz
            selected_year = year or datetime.now(tz).year
            vehicle_id = _parse_vehicle_id(vehicle)
            if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
                # expenses.vehicle_id is NOT NULL (migration 011), so there is no
                # unassigned bucket to filter to -- unlike trips.
                raise HTTPException(status_code=400, detail="Invalid vehicle")
            async with request.app.state.pool.connection() as conn:
                vehicles = await list_vehicles(conn, include_inactive=True)
                cur = conn.cursor(row_factory=dict_row)
                clauses = ["expenses.incurred_on >= %s", "expenses.incurred_on < %s"]
                params: list = [date(selected_year, 1, 1), date(selected_year + 1, 1, 1)]
                if vehicle_id is not None:
                    clauses.append("expenses.vehicle_id = %s")
                    params.append(vehicle_id)
                await cur.execute(
                    _EXPENSE_SELECT_JOIN
                    + "WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY expenses.incurred_on DESC, expenses.id DESC",
                    params,
                )
                expenses = [
                    _annotate_expense(row, tz) for row in await cur.fetchall()
                ]
                trip_cur = conn.cursor(row_factory=dict_row)
                await trip_cur.execute(
                    "SELECT trips.id, trips.started_at, trips.ended_at, trips.vehicle_id, "
                    "trips.exclusion::text AS exclusion, vehicles.name AS vehicle_name FROM trips "
                    "LEFT JOIN vehicles ON vehicles.id = trips.vehicle_id "
                    "ORDER BY trips.started_at DESC, trips.id DESC"
                )
                trips = await trip_cur.fetchall()
            category_totals: dict[str, Decimal] = {}
            for expense in expenses:
                category_totals[expense["category"]] = (
                    category_totals.get(expense["category"], Decimal("0")) + expense["amount"]
                )
            return await render_page(
                request, "expenses.html",
                {
                    "expenses": expenses,
                    "trips": trips,
                    "vehicles": vehicles,
                    "selected_year": selected_year,
                    "selected_vehicle": vehicle,
                    "category_totals": category_totals,
                    "ledger_total": sum(category_totals.values(), Decimal("0")),
                    "category_labels": CATEGORY_LABELS,
                    "treatment_labels": TREATMENT_LABELS,
                    "expense_conflict_labels": EXPENSE_CONFLICT_LABELS,
                    "expense_categories": EXPENSE_CATEGORIES,
                    "expense_treatments": EXPENSE_TREATMENTS,
                    "user": user,
                    "csrf": request.session.get("csrf", ""),
                },
            )

        @router.post("/expenses", dependencies=[Depends(require_csrf)])
        async def add_expense(
            request: Request,
            vehicle_id: int = Form(...),
            incurred_on: str = Form(...),
            category: str = Form(...),
            amount: str = Form(...),
            treatment: str = Form(""),
            notes: str = Form(""),
            trip_id: Annotated[str, Form()] = "",
            user: dict = Depends(require_user),
        ):
            parsed_trip_id = _parse_optional_trip_id(trip_id)
            parsed_date, category, parsed_amount, treatment = _parse_expense_input(
                incurred_on, category, amount, treatment, trip_id
            )
            async with request.app.state.pool.connection() as conn:
                vehicle_cur = await conn.execute(
                    "SELECT 1 FROM vehicles WHERE id = %s", (vehicle_id,)
                )
                if await vehicle_cur.fetchone() is None:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                if parsed_trip_id is not None:
                    trip_cur = await conn.execute(
                        "SELECT 1 FROM trips WHERE id = %s", (parsed_trip_id,)
                    )
                    if await trip_cur.fetchone() is None:
                        raise HTTPException(status_code=400, detail="No such trip")
                try:
                    await conn.execute(
                        "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment, notes, trip_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            vehicle_id, parsed_date, category, parsed_amount, treatment,
                            notes.strip() or None, parsed_trip_id,
                        ),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="Invalid expense reference")
            return Response(status_code=204, headers={"HX-Redirect": f"/expenses?year={parsed_date.year}"})

        @router.post("/expenses/{expense_id}/update", dependencies=[Depends(require_csrf)])
        async def update_expense(
            request: Request,
            expense_id: int,
            vehicle_id: int = Form(...),
            incurred_on: str = Form(...),
            category: str = Form(...),
            amount: str = Form(...),
            treatment: str = Form(""),
            notes: str = Form(""),
            trip_id: Annotated[str, Form()] = "",
            user: dict = Depends(require_user),
        ):
            parsed_trip_id = _parse_optional_trip_id(trip_id)
            parsed_date, category, parsed_amount, treatment = _parse_expense_input(
                incurred_on, category, amount, treatment, trip_id
            )
            async with request.app.state.pool.connection() as conn:
                vehicle_cur = await conn.execute(
                    "SELECT 1 FROM vehicles WHERE id = %s", (vehicle_id,)
                )
                if await vehicle_cur.fetchone() is None:
                    raise HTTPException(status_code=400, detail="No such vehicle")
                if parsed_trip_id is not None:
                    trip_cur = await conn.execute(
                        "SELECT 1 FROM trips WHERE id = %s", (parsed_trip_id,)
                    )
                    if await trip_cur.fetchone() is None:
                        raise HTTPException(status_code=400, detail="No such trip")
                try:
                    cur = await conn.execute(
                        "UPDATE expenses SET vehicle_id = %s, incurred_on = %s, category = %s, "
                        "amount = %s, treatment = %s, notes = %s, trip_id = %s, "
                        "updated_at = now() WHERE id = %s",
                        (
                            vehicle_id, parsed_date, category, parsed_amount, treatment,
                            notes.strip() or None, parsed_trip_id, expense_id,
                        ),
                    )
                except errors.ForeignKeyViolation:
                    raise HTTPException(status_code=400, detail="Invalid expense reference")
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such expense")
            return Response(status_code=204, headers={"HX-Redirect": f"/expenses?year={parsed_date.year}"})

        @router.post("/expenses/{expense_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_expense(
            request: Request, expense_id: int, user: dict = Depends(require_user)
        ):
            async with request.app.state.pool.connection() as conn:
                cur = await conn.execute("DELETE FROM expenses WHERE id = %s", (expense_id,))
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="No such expense")
            return Response(status_code=204, headers={"HX-Redirect": "/expenses"})
