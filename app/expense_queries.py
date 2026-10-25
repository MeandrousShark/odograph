"""Shared expense row SQL and the conflict labels every expense surface shows."""
from __future__ import annotations

from app.expenses import EXPENSE_CONFLICT_LABELS, expense_conflicts

EXPENSE_SELECT_JOIN = (
    "SELECT expenses.id, expenses.vehicle_id, vehicles.name AS vehicle_name, "
    "expenses.incurred_on, expenses.category::text AS category, expenses.amount, "
    "expenses.treatment::text AS treatment, expenses.notes, expenses.trip_id, "
    "trips.vehicle_id AS trip_vehicle_id, trips.started_at AS trip_started_at, "
    "trips.exclusion::text AS trip_exclusion "
    "FROM expenses JOIN vehicles ON vehicles.id = expenses.vehicle_id AND vehicles.account_id = expenses.account_id "
    "LEFT JOIN trips ON trips.id = expenses.trip_id AND trips.account_id = expenses.account_id "
)


def annotate_expense(row: dict, tz) -> dict:
    annotated = dict(row)
    annotated["conflicts"] = [
        EXPENSE_CONFLICT_LABELS[code] for code in expense_conflicts(annotated, tz)
    ]
    return annotated
