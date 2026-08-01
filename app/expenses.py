"""Pure actual-expense comparison arithmetic.

Tax-method eligibility and depreciation schedules depend on facts this app
does not possess. This module therefore compares only the entered annual
figures and keeps every uncertainty explicit; it never recommends or selects
a filing method.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from app.rates import YearRate, deduction

CENT = Decimal("0.01")

EXPENSE_CATEGORIES = (
    "fuel", "maintenance_repairs", "tires", "insurance",
    "registration_taxes", "lease_payments", "depreciation",
    "parking", "tolls", "other",
)
EXPENSE_TREATMENTS = ("business_use_allocated", "fully_business")
CATEGORY_LABELS = {
    "fuel": "Fuel",
    "maintenance_repairs": "Maintenance / repairs",
    "tires": "Tires",
    "insurance": "Insurance",
    "registration_taxes": "Registration / taxes",
    "lease_payments": "Lease payments",
    "depreciation": "Depreciation",
    "parking": "Parking",
    "tolls": "Tolls",
    "other": "Other",
}
TREATMENT_LABELS = {
    "business_use_allocated": "Business-use allocated",
    "fully_business": "Fully business",
}


@dataclass(frozen=True)
class ExpenseComparison:
    vehicle_id: int
    vehicle_name: str
    business_m: float
    denominator_m: float
    denominator_source: str
    provisional: bool
    invalid_odometer: bool
    business_pct: Decimal | None
    allocated_expenses: Decimal
    fully_business_expenses: Decimal
    standard_total: Decimal | None
    actual_total: Decimal | None
    difference: Decimal | None
    larger_estimate: str | None


@dataclass(frozen=True)
class ExpenseReport:
    comparisons: list[ExpenseComparison] = field(default_factory=list)
    category_totals: dict[str, Decimal] = field(default_factory=dict)
    allocated_total: Decimal = Decimal("0.00")
    fully_business_total: Decimal = Decimal("0.00")
    ledger_total: Decimal = Decimal("0.00")


def default_treatment(category: str) -> str:
    return "fully_business" if category in ("parking", "tolls") else "business_use_allocated"


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def comparison_status(line: ExpenseComparison) -> str:
    if line.business_pct is None:
        return "Unavailable"
    if line.invalid_odometer:
        return "Provisional (odometer ignored)"
    if line.provisional:
        return "Provisional"
    return "Odometer-backed"


def comparison_caveat_lines(lines: list[ExpenseComparison]) -> list[str]:
    """Shared HTML/XLSX wording so uncertainty cannot drift by format."""
    caveats = [
        "Odograph compares entered amounts only; it does not determine method eligibility, depreciation, basis adjustments, or recapture.",
        "The larger estimate is informational, not a filing recommendation; method choices can have multi-year consequences.",
    ]
    if any(line.provisional for line in lines):
        caveats.append(
            "Provisional comparisons use GPS-detected total miles; missed driving can make the business-use percentage and actual-method estimate too high."
        )
    if any(line.invalid_odometer for line in lines):
        caveats.append(
            "An inconsistent full-year odometer span (one or more readings did not increase, or the span was smaller than recorded business miles) was ignored and the comparison fell back to GPS-detected miles."
        )
    if any(line.business_pct is None for line in lines):
        caveats.append(
            "A comparison is unavailable when total vehicle miles are zero; fully-business expenses remain listed separately."
        )
    return caveats


def build_expense_report(
    year: int,
    trips: list[dict],
    expenses: list[dict],
    odometer_readings: list[dict],
    rates: dict[int, YearRate],
    tz: ZoneInfo,
) -> ExpenseReport:
    """Build per-vehicle method estimates using stable vehicle IDs.

    The caller may pass a coarse DB-side year window. Local-year checks stay
    here because report attribution, rate selection, and the odometer boundary
    must all agree at UTC/local-midnight edges.
    """
    year_start = datetime(year, 1, 1, tzinfo=tz)
    next_year_start = datetime(year + 1, 1, 1, tzinfo=tz)

    names: dict[int, str] = {}
    business_m: dict[int, float] = {}
    gps_total_m: dict[int, float] = {}
    business_by_month: dict[int, dict[int, float]] = {}
    for trip in trips:
        vehicle_id = trip.get("vehicle_id")
        if vehicle_id is None:
            continue
        local_start = trip["started_at"].astimezone(tz)
        if local_start.year != year:
            continue
        names[vehicle_id] = trip.get("vehicle_name") or f"Vehicle {vehicle_id}"
        distance_m = float(trip["display_distance_m"])
        gps_total_m[vehicle_id] = gps_total_m.get(vehicle_id, 0.0) + distance_m
        if trip["category"] == "business":
            business_m[vehicle_id] = business_m.get(vehicle_id, 0.0) + distance_m
            months = business_by_month.setdefault(vehicle_id, {})
            months[local_start.month] = months.get(local_start.month, 0.0) + distance_m

    category_totals: dict[str, Decimal] = {}
    allocated_by_vehicle: dict[int, Decimal] = {}
    fully_by_vehicle: dict[int, Decimal] = {}
    for expense in expenses:
        if expense["incurred_on"].year != year:
            continue
        vehicle_id = expense["vehicle_id"]
        names[vehicle_id] = expense.get("vehicle_name") or f"Vehicle {vehicle_id}"
        amount = _decimal(expense["amount"])
        category = expense["category"]
        category_totals[category] = category_totals.get(category, Decimal("0")) + amount
        target = fully_by_vehicle if expense["treatment"] == "fully_business" else allocated_by_vehicle
        target[vehicle_id] = target.get(vehicle_id, Decimal("0")) + amount

    readings_by_vehicle: dict[int, list[dict]] = {}
    for reading in odometer_readings:
        vehicle_id = reading["vehicle_id"]
        names[vehicle_id] = reading.get("vehicle_name") or names.get(vehicle_id, f"Vehicle {vehicle_id}")
        readings_by_vehicle.setdefault(vehicle_id, []).append(reading)

    comparisons = []
    vehicle_ids = set(gps_total_m) | set(allocated_by_vehicle) | set(fully_by_vehicle)
    for vehicle_id in vehicle_ids:
        biz_m = business_m.get(vehicle_id, 0.0)
        readings = sorted(readings_by_vehicle.get(vehicle_id, []), key=lambda row: row["recorded_at"])
        starts = [row for row in readings if row["recorded_at"] <= year_start]
        ends = [row for row in readings if row["recorded_at"] >= next_year_start]
        invalid_odometer = False
        denominator_source = "gps"
        denominator_m = gps_total_m.get(vehicle_id, 0.0)
        denominator_decimal = _decimal(denominator_m)
        provisional = True
        if starts and ends:
            start_anchor = starts[-1]
            end_anchor = ends[0]
            bracket = [
                row for row in readings
                if start_anchor["recorded_at"] <= row["recorded_at"] <= end_anchor["recorded_at"]
            ]
            has_non_increasing_interval = any(
                _decimal(current["odometer_m"]) <= _decimal(previous["odometer_m"])
                for previous, current in zip(bracket, bracket[1:])
            )
            delta = _decimal(end_anchor["odometer_m"]) - _decimal(start_anchor["odometer_m"])
            # A positive delta can still be impossible: business mileage is a
            # subset of total mileage, so it cannot exceed the odometer span.
            # An endpoint-only check also hides an internal rollback, which
            # makes the overall delta untrustworthy even when it stays positive.
            # Falling back preserves the approved all-category GPS denominator.
            if not has_non_increasing_interval and delta >= _decimal(biz_m) and delta > 0:
                denominator_decimal = delta
                denominator_m = float(delta)
                denominator_source = "odometer"
                provisional = False
            else:
                invalid_odometer = True

        pct = _decimal(biz_m) / denominator_decimal if denominator_decimal > 0 else None
        allocated = _money(allocated_by_vehicle.get(vehicle_id, Decimal("0")))
        fully = _money(fully_by_vehicle.get(vehicle_id, Decimal("0")))
        standard_base = Decimal("0")
        any_rate = False
        for month, meters in business_by_month.get(vehicle_id, {}).items():
            value = deduction(meters, year, rates, month)
            if value is not None:
                standard_base += _decimal(value)
                any_rate = True
        standard_total = _money(standard_base + fully) if any_rate or biz_m == 0 else None
        actual_total = _money(allocated * pct + fully) if pct is not None else None
        difference = None
        leader = None
        if standard_total is not None and actual_total is not None:
            difference = _money(abs(actual_total - standard_total))
            if actual_total > standard_total:
                leader = "actual"
            elif standard_total > actual_total:
                leader = "standard"
            else:
                leader = "tie"
        comparisons.append(ExpenseComparison(
            vehicle_id=vehicle_id,
            vehicle_name=names[vehicle_id],
            business_m=biz_m,
            denominator_m=denominator_m,
            denominator_source=denominator_source,
            provisional=provisional,
            invalid_odometer=invalid_odometer,
            business_pct=pct,
            allocated_expenses=allocated,
            fully_business_expenses=fully,
            standard_total=standard_total,
            actual_total=actual_total,
            difference=difference,
            larger_estimate=leader,
        ))

    comparisons.sort(key=lambda line: (line.vehicle_name.casefold(), line.vehicle_id))
    normalized_categories = {key: _money(value) for key, value in category_totals.items()}
    allocated_total = _money(sum(allocated_by_vehicle.values(), Decimal("0")))
    fully_total = _money(sum(fully_by_vehicle.values(), Decimal("0")))
    return ExpenseReport(
        comparisons=comparisons,
        category_totals=normalized_categories,
        allocated_total=allocated_total,
        fully_business_total=fully_total,
        ledger_total=_money(allocated_total + fully_total),
    )
