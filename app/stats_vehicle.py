"""Pure presentation model for the stats page's per-vehicle breakdown.

Grouping and per-month deduction math happen here rather than in SQL so the
same `sum_month_deductions` fold the report page uses (mid-year rate splits
included) prices each vehicle's business miles identically to the annual
report -- an operator comparing the two pages must never see the numbers
disagree over a rate change that landed mid-year.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.rates import YearRate
from app.report import sum_month_deductions

UNASSIGNED_LABEL = "Unassigned"


@dataclass(frozen=True)
class VehicleStats:
    vehicle_id: int | None  # None for unassigned trips
    vehicle_name: str  # UNASSIGNED_LABEL for vehicle_id None
    business_m: float
    personal_m: float
    unclassified_m: float
    total_m: float
    expense_total: float
    deduction: float | None  # None when no rate is on file for the year
    nondeductible_m: float = 0.0


@dataclass(frozen=True)
class VehicleBreakdown:
    vehicles: list[VehicleStats] = field(default_factory=list)
    unassigned_trip_count: int = 0
    coverage_note: str | None = None
    assigned_vehicle_count: int = 0


@dataclass
class _Accum:
    vehicle_name: str
    business_m: float = 0.0
    personal_m: float = 0.0
    unclassified_m: float = 0.0
    nondeductible_m: float = 0.0
    trip_count: int = 0
    month_business: dict[int, float] = field(default_factory=dict)


def build_vehicle_breakdown(
    mileage_rows: Iterable[tuple[int | None, str, int, str, int, float]],
    expense_rows: Iterable[tuple[int | None, str, float]],
    year: int,
    rates: dict[int, YearRate],
) -> VehicleBreakdown:
    expense_rows = list(expense_rows)

    accums: dict[int | None, _Accum] = {}
    # A vehicle with expenses (fuel, insurance) but no trips logged yet in
    # the period would otherwise never get an _Accum: expenses.vehicle_id
    # is NOT NULL, so seeding from expense_rows here is what keeps such a
    # vehicle's row (and its expense total) from being silently dropped.
    for vehicle_id, vehicle_name, _expense_total in expense_rows:
        accums.setdefault(vehicle_id, _Accum(vehicle_name))

    for vehicle_id, vehicle_name, month, category, count, meters in mileage_rows:
        name = vehicle_name if vehicle_id is not None else UNASSIGNED_LABEL
        acc = accums.setdefault(vehicle_id, _Accum(name))
        acc.trip_count += int(count)
        meters = float(meters)
        if category == "business":
            acc.business_m += meters
            acc.month_business[month] = acc.month_business.get(month, 0.0) + meters
        elif category == "personal":
            acc.personal_m += meters
        elif category == "unclassified":
            acc.unclassified_m += meters
        elif category == "nondeductible":
            acc.nondeductible_m += meters

    expense_totals: dict[int | None, float] = {}
    for vehicle_id, _vehicle_name, expense_total in expense_rows:
        expense_totals[vehicle_id] = expense_totals.get(vehicle_id, 0.0) + float(expense_total)

    unassigned_trip_count = accums[None].trip_count if None in accums else 0

    vehicles: list[VehicleStats] = []
    for vehicle_id, acc in accums.items():
        month_meters = list(acc.month_business.items())
        deduction = sum_month_deductions(month_meters, year, rates)
        vehicles.append(
            VehicleStats(
                vehicle_id=vehicle_id,
                vehicle_name=acc.vehicle_name,
                business_m=acc.business_m,
                personal_m=acc.personal_m,
                unclassified_m=acc.unclassified_m,
                total_m=(
                    acc.business_m + acc.personal_m + acc.unclassified_m
                    + acc.nondeductible_m
                ),
                expense_total=expense_totals.get(vehicle_id, 0.0),
                deduction=deduction,
                nondeductible_m=acc.nondeductible_m,
            )
        )

    # Unassigned always sorts last regardless of its mileage total, since it
    # isn't a real vehicle -- assigned vehicles are ranked by total_m.
    vehicles.sort(key=lambda v: (v.vehicle_id is None, -v.total_m))

    coverage_note = (
        f"{unassigned_trip_count} trip(s) without a vehicle assignment are grouped under Unassigned."
        if unassigned_trip_count > 0
        else None
    )

    assigned_vehicle_count = sum(1 for v in vehicles if v.vehicle_id is not None)

    return VehicleBreakdown(
        vehicles=vehicles,
        unassigned_trip_count=unassigned_trip_count,
        coverage_note=coverage_note,
        assigned_vehicle_count=assigned_vehicle_count,
    )
