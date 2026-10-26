from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from starlette.responses import Response

from app import expense_ledger as ledger_service
from app.auth import require_csrf, require_user
from app.capacity_routes import mark_committed_write
from app.page import render_page
from app.expenses import (
    CATEGORY_LABELS,
    EXPENSE_CONFLICT_LABELS,
    EXPENSE_CATEGORIES,
    EXPENSE_TREATMENTS,
    TREATMENT_LABELS,
)
from app.expense_ledger import (
    AMOUNT_INVALID, CATEGORY_INVALID, DATE_INVALID, TREATMENT_INVALID, TREATMENT_REQUIRED, TRIP_INVALID,
    ExpenseValidationError, parse_expense_amount, resolve_treatment,
)
from app.expense_queries import EXPENSE_SELECT_JOIN as _EXPENSE_SELECT_JOIN  # noqa: F401
from app.vehicles import list_vehicles

from app.ui._common import VEHICLE_FILTER_UNASSIGNED, _parse_vehicle_id


def _parse_expense_input(
    incurred_on: str, category: str, amount: str, treatment: str,
    trip_id: str | None = None,
) -> tuple[date, str, Decimal, str]:
    try:
        parsed_date = date.fromisoformat(incurred_on)
    except ValueError:
        raise HTTPException(status_code=400, detail=DATE_INVALID)
    if category not in EXPENSE_CATEGORIES:
        raise HTTPException(status_code=400, detail=CATEGORY_INVALID)
    parsed_amount = parse_expense_amount(amount)
    if parsed_amount is None:
        raise HTTPException(status_code=400, detail=AMOUNT_INVALID)
    resolved = resolve_treatment(category, treatment)
    if resolved is None:
        raise HTTPException(status_code=400, detail=TREATMENT_REQUIRED)
    treatment = resolved
    if treatment not in EXPENSE_TREATMENTS:
        raise HTTPException(status_code=400, detail=TREATMENT_INVALID)
    if isinstance(trip_id, str) and trip_id:
        try:
            parsed_trip_id = int(trip_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=TRIP_INVALID)
        if parsed_trip_id < 1:
            raise HTTPException(status_code=400, detail=TRIP_INVALID)
    return parsed_date, category, parsed_amount, treatment


def _parse_optional_trip_id(trip_id: str | None) -> int | None:
    if not isinstance(trip_id, str) or not trip_id:
        return None
    try:
        parsed = int(trip_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=TRIP_INVALID)
    if parsed < 1:
        raise HTTPException(status_code=400, detail=TRIP_INVALID)
    return parsed


def _form_expense(vehicle_id: int, incurred_on: date, category: str, amount: Decimal, treatment: str,
                  notes: str, trip_id: int | None) -> dict:
    return {"vehicle_id": vehicle_id, "incurred_on": incurred_on, "category": category, "amount": amount,
            "treatment": treatment, "notes": notes, "trip_id": trip_id}


def register(router: APIRouter) -> None:
        @router.get("/expenses")
        async def expense_ledger(
            request: Request,
            year: int | None = Query(None, ge=1, le=9998),
            vehicle: str = Query(""),
            user: dict = Depends(require_user),
        ):
            tz = request.state.config.display_tz
            selected_year = year or datetime.now(tz).year
            vehicle_id = _parse_vehicle_id(vehicle)
            if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
                # expenses.vehicle_id is NOT NULL (migration 011), so there is no
                # unassigned bucket to filter to -- unlike trips.
                raise HTTPException(status_code=400, detail="Invalid vehicle")
            async with request.state.account_pool.connection() as conn:
                vehicles = await list_vehicles(conn, include_inactive=True)
                ledger = await ledger_service.fetch_ledger(conn, selected_year, vehicle_id, tz)
            return await render_page(
                request, "expenses.html",
                {
                    "expenses": ledger.expenses,
                    "trips": ledger.trips,
                    "vehicles": vehicles,
                    "selected_year": selected_year,
                    "selected_vehicle": vehicle,
                    "category_totals": ledger.category_totals,
                    "ledger_total": ledger.total,
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
            values = _form_expense(vehicle_id, parsed_date, category, parsed_amount, treatment, notes,
                                   parsed_trip_id)
            async with request.state.account_pool.connection() as conn:
                try:
                    expense = await ledger_service.validate_expense(conn, values)
                    await ledger_service.insert_expense(conn, expense)
                except ExpenseValidationError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from None
            mark_committed_write(request)
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
            values = _form_expense(vehicle_id, parsed_date, category, parsed_amount, treatment, notes,
                                   parsed_trip_id)
            async with request.state.account_pool.connection() as conn:
                try:
                    # Every save refreshes updated_at, as the form always has.
                    await ledger_service.update_expense(conn, expense_id, values, write_unchanged=True)
                except ExpenseValidationError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from None
            mark_committed_write(request)
            return Response(status_code=204, headers={"HX-Redirect": f"/expenses?year={parsed_date.year}"})

        @router.post("/expenses/{expense_id}/delete", dependencies=[Depends(require_csrf)])
        async def delete_expense(
            request: Request, expense_id: int, user: dict = Depends(require_user)
        ):
            async with request.state.account_pool.connection() as conn:
                await ledger_service.delete_expense(conn, expense_id)
            mark_committed_write(request)
            return Response(status_code=204, headers={"HX-Redirect": "/expenses"})
