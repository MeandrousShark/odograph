"""CSV/XLSX export and the annual/range report workbooks.
`build_export_rows` is pure -- no I/O, no DB/openpyxl imports -- so it's
unit-testable without a workbook or a filesystem. `to_csv`/`to_xlsx` are thin,
in-memory writers around it; `to_report_xlsx` adds a Summary sheet ahead
of the same Trips sheet, so the report carries its own audit-appendix detail.
`to_range_report_xlsx` reuses the same sheet writers for a range report,
without the annual-only Expenses/odometer sections.
"""
from __future__ import annotations

import csv
import io
import os
import sys
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.expenses import (
    CATEGORY_LABELS,
    TREATMENT_LABELS,
    ExpenseReport,
    comparison_caveat_lines,
    comparison_status,
)
from app.formatting import format_duration
from app.odometer import VehicleCoverage, coverage_line
from app.places_desc import describe_endpoint
from app.rates import METERS_PER_MILE, YearRate, deduction
from app.report import MONTH_ABBR, AnnualReport, RangeReport, caveat_lines, format_rate_periods, range_label

HEADERS = (
    "Date", "Start", "End", "Duration", "Start location", "End location",
    "Distance (mi)", "Distance (km)", "Category", "Exclusion", "Vehicle", "Purpose", "Notes",
    "Gap", "Source", "Deduction ($)",
)


def _write_only_cell(ws, value, *, font=None, number_format=None):
    from openpyxl.cell import WriteOnlyCell

    cell = WriteOnlyCell(ws, value=value)
    if font is not None:
        cell.font = font
    if number_format is not None:
        cell.number_format = number_format
    return cell


def _cleanup_write_only_workbook(wb) -> None:
    """Remove only this workbook's unfinished openpyxl worksheet writers."""
    has_primary_error = sys.exc_info()[0] is not None
    cleanup_error = None
    for ws in wb.worksheets:
        writer = getattr(ws, "_writer", None)
        path = getattr(writer, "out", None)
        if writer is None or not path or not os.path.exists(path):
            continue
        try:
            if not ws.closed:
                ws.close()
        except Exception as error:
            cleanup_error = cleanup_error or error
        finally:
            try:
                writer.close()
            except Exception as error:
                cleanup_error = cleanup_error or error
            try:
                if os.path.exists(path):
                    writer.cleanup()
            except Exception as error:
                cleanup_error = cleanup_error or error
    if cleanup_error is not None and not has_primary_error:
        raise cleanup_error


def _trip_distance_and_deduction(t: dict, rates: dict[int, YearRate], tz: ZoneInfo):
    """The unrounded `(distance_m, deduction_or_None)` a trip contributes,
    shared by `build_export_rows` (which rounds each for per-row display)
    and `_populate_trips_sheet`'s total row (which sums these unrounded
    values before rounding once), so the Trips-sheet total can't drift
    from the Summary sheet's own unrounded-then-rounded total by instead
    summing already-rounded per-row cells. `distance_m` is
    `display_distance_m` (snapped-or-raw), and `ded` is business-only,
    matching every other deduction figure in the app.
    """
    local_start = t["started_at"].astimezone(tz)
    distance_m = t["display_distance_m"]
    is_business = t["category"] == "business" and not t.get("exclusion")
    ded = (
        deduction(distance_m, local_start.year, rates, local_start.month)
        if is_business else None
    )
    return distance_m, ded


def _iter_export_rows(trips, rates: dict[int, YearRate], tz: ZoneInfo):
    """Yield rows in `HEADERS` order without retaining the export in memory."""
    for t in trips:
        yield _export_row(t, rates, tz)


def _export_row(t: dict, rates: dict[int, YearRate], tz: ZoneInfo) -> list:
    local_start = t["started_at"].astimezone(tz)
    local_end = t["ended_at"].astimezone(tz)
    distance_m, ded = _trip_distance_and_deduction(t, rates, tz)
    return [
        local_start.strftime("%Y-%m-%d"),
        local_start.strftime("%H:%M"),
        local_end.strftime("%H:%M"),
        format_duration(t["started_at"], t["ended_at"]),
        describe_endpoint(
            t.get("start_place_name"), t.get("start_lat"), t.get("start_lon"),
            t.get("start_address"),
        ),
        describe_endpoint(
            t.get("end_place_name"), t.get("end_lat"), t.get("end_lon"),
            t.get("end_address"),
        ),
        round(distance_m / METERS_PER_MILE, 1),
        round(distance_m / 1000.0, 1),
        t["category"],
        t.get("exclusion") or "",
        t.get("vehicle_name") or "",
        t.get("purpose") or "",
        t.get("notes") or "",
        "yes" if t.get("has_gap") else "",
        t["source"],
        round(ded, 2) if ded is not None else "",
    ]


def build_export_rows(trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo) -> list[list]:
    """One row per trip, in `HEADERS` order. `trips` rows are expected to
    carry the same keys `TRIP_COLUMNS` selects (including start/end place
    names). Uses `display_distance_m` -- snapped distance when available,
    raw `distance_m` as fallback -- so exports match what's shown on the
    trip list.
    """
    return list(_iter_export_rows(trips, rates, tz))


def to_csv(trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo) -> bytes:
    rows = build_export_rows(trips, rates, tz)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _populate_trips_sheet(ws, trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo) -> None:
    from openpyxl.styles import Font

    header_font = Font(bold=True)
    ws.append([_write_only_cell(ws, value, font=header_font) for value in HEADERS])

    # The total row is derived from each trip's unrounded distance/deduction
    # (via `_trip_distance_and_deduction`, the same helper `build_export_rows`
    # rounds per-row), not by summing the already-rounded row cells above.
    # Summing pre-rounded per-trip cells drifted from the Summary sheet's own
    # unrounded-then-rounded `total_deduction`, so one workbook could show two
    # different deduction totals for the same trips. Mi/km totals are rounded
    # the same way, once, for internal consistency, even though they cover
    # every category (not just business) and so aren't expected to match the
    # Summary's classified "Total miles". Ledger rows remain visible for
    # both exclusion states, but not_my_vehicle is omitted from the totals
    # because those miles belong to no tracked vehicle.
    mi_col, km_col, ded_col = 7, 8, 16
    total_m = 0.0
    total_ded = 0.0
    for t in trips:
        row = _export_row(t, rates, tz)
        distance_m, ded = _trip_distance_and_deduction(t, rates, tz)
        if t.get("exclusion") != "not_my_vehicle":
            total_m += distance_m
        if ded is not None:
            total_ded += ded
        deduction_value = row[-1]
        if isinstance(deduction_value, (int, float)):
            row[-1] = _write_only_cell(
                ws, deduction_value, number_format='"$"#,##0.00'
            )
        ws.append(row)
    totals = [""] * len(HEADERS)
    totals[0] = "Total"
    totals[mi_col - 1] = round(total_m / METERS_PER_MILE, 1)
    totals[km_col - 1] = round(total_m / 1000.0, 1)
    totals[ded_col - 1] = round(total_ded, 2)
    total_cells = []
    for index, value in enumerate(totals):
        total_cells.append(_write_only_cell(
            ws, value, font=header_font,
            number_format='"$"#,##0.00' if index == ded_col - 1 else None,
        ))
    ws.append(total_cells)


def to_xlsx(trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo) -> bytes:
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    try:
        ws = wb.create_sheet("Trips")
        _populate_trips_sheet(ws, trips, rates, tz)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    finally:
        _cleanup_write_only_workbook(wb)
        wb.close()


def _write_summary_sheet(
    ws, report: AnnualReport, odometer_coverage: list[VehicleCoverage] | None = None,
    expense_report: ExpenseReport | None = None, title: str | None = None,
) -> None:
    """The report's headline numbers, laid out for a quick read rather than
    as a data table -- a distinct shape from the Trips sheet's per-row detail.
    `title` defaults to the annual report's own heading; `to_range_report_xlsx`
    passes a `range_label`-derived one instead, so the two exports share this
    writer without the range report's Summary sheet ever calling itself annual.
    """
    from openpyxl.styles import Font

    for col, width in zip("ABCDEFGHIJK", (32, 18, 14, 16, 18, 19, 18, 20, 18, 28, 18)):
        ws.column_dimensions[col].width = width

    def append(values, *, bold=False, title_row=False):
        if not values:
            ws.append([])
            return
        font = Font(bold=True, size=14) if title_row else Font(bold=True) if bold else None
        cells = [_write_only_cell(ws, value, font=font) for value in values]
        ws.append(cells)

    append([title or f"Annual Mileage Report: {report.year}"], bold=True, title_row=True)
    append([])
    append(["Business miles", round(report.business_m / METERS_PER_MILE, 1)])
    append(["Personal miles", round(report.personal_m / METERS_PER_MILE, 1)])
    if report.nondeductible_m:
        append(["Non-deductible miles", round(report.nondeductible_m / METERS_PER_MILE, 1)])
    append(["Total miles", round(report.total_m / METERS_PER_MILE, 1)])
    append([
        "Business share",
        f"{report.business_pct:.1f}%" if report.business_pct is not None else "--",
    ])
    append(["Rate(s) applied", format_rate_periods(report.rate_periods)])
    append([
        "Total deduction",
        f"${report.total_deduction:,.2f}" if report.total_deduction is not None else "--",
    ])
    append(["Trip count", report.trip_count])
    append([])

    append(["Month", "Trips", "Business (mi)", "Rate ($/mi)", "Deduction ($)"], bold=True)
    for month in report.months:
        append([
            MONTH_ABBR[month.month],
            month.trip_count,
            round(month.business_m / METERS_PER_MILE, 1),
            round(month.rate_per_mi, 4) if month.rate_per_mi is not None else "--",
            round(month.deduction, 2) if month.deduction is not None else "--",
        ])

    if report.caveats.any:
        append([])
        append(["Caveats: review before filing"], bold=True)
        for line in caveat_lines(report.caveats, report.year):
            append([line])

    # Appended after the caveats block (rather than, say, right after the
    # month table) so the summary sheet's fixed row positions (title/headline
    # rows, month-table header/rows) stay put regardless of how many vehicles
    # a year has.
    if report.by_vehicle:
        append([])
        append([
            "By vehicle", "Business (mi)", "Non-deductible (mi)", "Total (mi)",
            "Deduction ($)",
        ], bold=True)
        for v in report.by_vehicle:
            append([
                v.vehicle_name,
                round(v.business_m / METERS_PER_MILE, 1),
                round(v.nondeductible_m / METERS_PER_MILE, 1),
                round(v.total_m / METERS_PER_MILE, 1),
                round(v.deduction, 2) if v.deduction is not None else "--",
            ])

    # Appended after "By vehicle" for the same fixed-row-position reason;
    # one text line per vehicle via `coverage_line` (not a data table) so
    # this can't drift in wording from the HTML report's own use of it.
    if odometer_coverage:
        append([])
        append(["Odometer reconciliation"], bold=True)
        for line in odometer_coverage:
            append([coverage_line(line)])

    if expense_report and expense_report.comparisons:
        append([])
        append(["Standard vs. actual expense estimate"], bold=True)
        append([
            "Vehicle", "Business (mi)", "Total (mi)", "Business use", "Total-mile source",
            "Shared expenses ($)", "Fully business ($)", "Standard estimate ($)",
            "Actual estimate ($)", "Larger estimate", "Status",
        ], bold=True)
        for line in expense_report.comparisons:
            if line.larger_estimate == "tie":
                larger = "Tie"
            elif line.larger_estimate:
                larger = f"{line.larger_estimate.title()} by ${line.difference:,.2f}"
                if line.provisional:
                    larger += " (provisional)"
            else:
                larger = "--"
            append([
                line.vehicle_name,
                round(line.business_m / METERS_PER_MILE, 1),
                round(line.denominator_m / METERS_PER_MILE, 1),
                f"{line.business_pct * 100:.1f}%" if line.business_pct is not None else "--",
                "Odometer" if line.denominator_source == "odometer" else "GPS detected",
                float(line.allocated_expenses),
                float(line.fully_business_expenses),
                float(line.standard_total) if line.standard_total is not None else "--",
                float(line.actual_total) if line.actual_total is not None else "--",
                larger,
                comparison_status(line),
            ])
        append([])
        append(["Actual-expense caveats: review before filing"], bold=True)
        for line in comparison_caveat_lines(expense_report.comparisons):
            append([line])
        append(["IRS guidance: Publication 463 and Publication 946."])


def _populate_expenses_sheet(ws, expenses: list[dict]) -> None:
    from openpyxl.styles import Font

    headers = ("Date", "Vehicle", "Category", "Amount ($)", "Tax treatment", "Notes")
    for col, width in zip("ABCDEF", (14, 22, 24, 14, 24, 36)):
        ws.column_dimensions[col].width = width
    header_font = Font(bold=True)
    ws.append([_write_only_cell(ws, value, font=header_font) for value in headers])
    total = Decimal("0")
    for expense in expenses:
        amount = Decimal(str(expense["amount"]))
        total += amount
        row = [
            expense["incurred_on"], expense["vehicle_name"],
            CATEGORY_LABELS[expense["category"]], float(amount),
            TREATMENT_LABELS[expense["treatment"]], expense.get("notes") or "",
        ]
        row[3] = _write_only_cell(ws, row[3], number_format='"$"#,##0.00')
        ws.append(row)
    total_row = ["Total", "", "", float(total.quantize(Decimal("0.01"))), "", ""]
    total_cells = []
    for index, value in enumerate(total_row):
        total_cells.append(_write_only_cell(
            ws, value, font=Font(bold=True),
            number_format='"$"#,##0.00' if index == 3 else None,
        ))
    ws.append(total_cells)


def to_report_xlsx(
    report: AnnualReport, trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo,
    odometer_coverage: list[VehicleCoverage] | None = None,
    expense_report: ExpenseReport | None = None,
    expenses: list[dict] | None = None,
) -> bytes:
    """Summary sheet with the headline numbers/month table/caveats, plus a
    Trips sheet (the same detail `to_xlsx` writes, filtered to the report's
    year) as an audit appendix backing the summary. When an expense report is
    supplied, an Expenses sheet carries the ledger behind the comparison.
    `odometer_coverage` is computed by the caller (app.ui's report routes),
    same as `report` itself -- kept a plain parameter here rather than folded
    into `AnnualReport` so `build_annual_report`'s signature stays untouched.
    """
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    try:
        summary_ws = wb.create_sheet("Summary")
        _write_summary_sheet(summary_ws, report, odometer_coverage, expense_report)

        trips_ws = wb.create_sheet("Trips")
        _populate_trips_sheet(trips_ws, trips, rates, tz)

        if expense_report is not None:
            expenses_ws = wb.create_sheet("Expenses")
            _populate_expenses_sheet(expenses_ws, expenses or [])

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    finally:
        _cleanup_write_only_workbook(wb)
        wb.close()


def to_range_report_xlsx(
    report: RangeReport, trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo
) -> bytes:
    """Range-report analog of `to_report_xlsx` -- reuses the same
    `_write_summary_sheet`/`_populate_trips_sheet` writers so the two exports
    can't drift on layout, with no `odometer_coverage`/`expense_report`
    arguments to pass through (the range report deliberately carries no
    odometer-coverage or standard-vs-actual section, so there's nothing
    app/ui/reports.py's range routes need to fetch for those sheets).
    `trips` is filtered here to those whose local start date falls in
    `[report.start, report.end]` -- the same rule `build_range_report` used to
    fold the summary numbers above -- so every Trips-sheet row backs a row
    already counted in the summary, even though the caller's DB query only
    coarsely pre-filters (see `app/ui/reports.py`'s `_fetch_range_trips`).
    """
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    try:
        summary_ws = wb.create_sheet("Summary")
        _write_summary_sheet(
            summary_ws, report, title=f"Mileage Report: {range_label(report.start, report.end)}"
        )

        trips_ws = wb.create_sheet("Trips")
        in_range_trips = (
            t for t in trips if report.start <= t["started_at"].astimezone(tz).date() <= report.end
        )
        _populate_trips_sheet(trips_ws, in_range_trips, rates, tz)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    finally:
        _cleanup_write_only_workbook(wb)
        wb.close()
