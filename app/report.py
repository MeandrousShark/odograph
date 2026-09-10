"""Annual tax report aggregation, generalized to arbitrary single-year date
ranges. `build_range_report` is the one fold -- pure, no I/O, no DB/openpyxl
imports -- so it's unit-testable the same way `app.export.build_export_rows`
is; `build_annual_report` is a thin Jan 1-Dec 31 wrapper around it, kept as
its own function so its existing signature/output type are untouched.
`app/ui/reports.py` fetches trips with `TRIP_COLUMNS` and hands them here;
`app/export.py`'s `to_report_xlsx` renders the result.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.rates import METERS_PER_MILE, YearRate, deduction, rate_for

MONTH_ABBR = calendar.month_abbr  # ['', 'Jan', ..., 'Dec'] -- same table app/main.py hands templates


@dataclass(frozen=True)
class MonthLine:
    month: int  # 1-12
    trip_count: int
    business_m: float
    rate_per_mi: float | None
    deduction: float | None


NO_VEHICLE_LABEL = "(no vehicle)"


@dataclass(frozen=True)
class VehicleLine:
    vehicle_id: int | None
    vehicle_name: str  # NO_VEHICLE_LABEL for trips with no vehicle assigned
    business_m: float
    personal_m: float
    nondeductible_m: float
    total_m: float  # business + personal + nondeductible, excludes unclassified
    deduction: float | None


@dataclass(frozen=True)
class ReportCaveats:
    gap_trips: int = 0
    low_conf_trips: int = 0
    manual_trips: int = 0
    unclassified_trips: int = 0
    business_missing_purpose: int = 0
    missing_rate: bool = False

    @property
    def any(self) -> bool:
        return (
            self.gap_trips > 0 or self.low_conf_trips > 0 or self.manual_trips > 0
            or self.unclassified_trips > 0 or self.business_missing_purpose > 0
            or self.missing_rate
        )


@dataclass(frozen=True)
class AnnualReport:
    year: int
    months: list[MonthLine] = field(default_factory=list)  # ascending, business trips only
    business_m: float = 0.0
    personal_m: float = 0.0
    nondeductible_m: float = 0.0
    business_pct: float | None = None
    total_deduction: float | None = None  # None only when no rate is on file at all
    rate_periods: list[tuple[float, int, int]] = field(default_factory=list)  # (rate, first_month, last_month)
    trip_count: int = 0
    caveats: ReportCaveats = field(default_factory=ReportCaveats)
    by_vehicle: list[VehicleLine] = field(default_factory=list)  # sorted by vehicle_name

    @property
    def total_m(self) -> float:
        return self.business_m + self.personal_m + self.nondeductible_m


@dataclass(frozen=True)
class RangeReport(AnnualReport):
    """`AnnualReport` plus the exact `start`/`end` dates a range report was
    built for (both within `year`). A subclass, not a duplicate field list,
    so the two can't drift on shape -- `start`/`end` are `kw_only` since a
    dataclass subclass can't add positional fields after a parent field that
    already has a default (`AnnualReport.months` onward all do).
    """
    start: date = field(kw_only=True)
    end: date = field(kw_only=True)


def default_report_year(now: datetime) -> int:
    """Jan-Apr defaults to the prior (complete) year, since that's the year
    still being filed; May-Dec defaults to the current year, once filing
    season for the prior year has passed.
    """
    return now.year - 1 if now.month <= 4 else now.year


def next_year_disabled(report_year: int, now: datetime) -> bool:
    """True once `report_year` is the operator's current year or later, so
    the report page's "next year" control can't offer a year that hasn't
    started. `>=`, not `==`, since `/report/{year}` accepts any year 1-9998
    by URL -- a year already reached that way must not offer the one past
    it either. `now` must already be localized to the display timezone
    (a caller passing a UTC `now` would disable the operator's still-current
    year right at a US evening's year boundary, when the UTC date has
    already rolled over but the local one hasn't).
    """
    return report_year >= now.year


def sum_month_deductions(
    month_meters: list[tuple[int, float]], year: int, rates: dict[int, YearRate]
) -> float | None:
    """Sum per-month business deductions for one year. Summing by month (not
    one flat total) is what lets a mid-year rate change price each half
    correctly -- IRS splits fall on a month boundary, so a month is always
    within one rate period. None if no rate is on file for the year at all.
    Shared by `app/ui/trips.py`'s YTD stat and `build_annual_report` below
    so the two can't independently drift on this rule.
    """
    total = 0.0
    any_rate = False
    for month, meters in month_meters:
        d = deduction(meters, year, rates, month)
        if d is not None:
            total += d
            any_rate = True
    return total if any_rate else None


def format_rate_periods(rate_periods: list[tuple[float, int, int]]) -> str:
    """Render `AnnualReport.rate_periods` as "$0.6850/mi Jan-Jun, $0.7000/mi
    Jul-Dec" (or "no rate on file" when empty). The one place this text is
    built, called from both the XLSX summary sheet and the HTML report page,
    so they can't render the same data with different punctuation.
    """
    if not rate_periods:
        return "no rate on file"
    spans = []
    for rate, first, last in rate_periods:
        span = (
            MONTH_ABBR[first] if first == last
            else f"{MONTH_ABBR[first]}-{MONTH_ABBR[last]}"
        )
        spans.append(f"${rate:.4f}/mi {span}")
    return ", ".join(spans)


def caveat_lines(caveats: ReportCaveats, year: int) -> list[str]:
    """Plain-text caveat sentences, ordered unclassified/missing-purpose/gap/
    low-confidence/manual/missing-rate. The one place this wording is built, called from
    both the XLSX summary sheet and the HTML report page, so the two can't
    drift on what each caveat says (they already had on the unclassified
    line before this was unified). `missing_rate`, when present, is always
    last -- callers needing to append something rate-specific (e.g. the HTML
    page's "Add one" settings link) can rely on that ordering.
    """
    lines = []
    if caveats.unclassified_trips:
        lines.append(
            f"{caveats.unclassified_trips} trip(s) still unclassified, not "
            "counted in the business/personal split above."
        )
    if caveats.business_missing_purpose:
        lines.append(
            f"{caveats.business_missing_purpose} business trip(s) have no purpose recorded."
        )
    if caveats.gap_trips:
        lines.append(
            f"{caveats.gap_trips} trip(s) have a recording gap; distance may under-read."
        )
    if caveats.low_conf_trips:
        lines.append(
            f"{caveats.low_conf_trips} trip(s) have a low-confidence road-snapped distance."
        )
    if caveats.manual_trips:
        lines.append(f"{caveats.manual_trips} trip(s) were entered manually.")
    if caveats.missing_rate:
        lines.append(f"No IRS mileage rate on file for {year}; deduction is unavailable.")
    return lines


def quarter_bounds(year: int, q: int) -> tuple[date, date]:
    """First/last calendar date of tax quarter `q` (1-4) in `year` -- the Q1-Q4
    preset links on the report page, and the exact-quarter check
    `range_label` uses to prefer "2026 Q2" over a raw date span.
    """
    if q not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {q}")
    first_month = (q - 1) * 3 + 1
    last_month = first_month + 2
    return date(year, first_month, 1), date(year, last_month, calendar.monthrange(year, last_month)[1])


def range_label(start: date, end: date) -> str:
    """Human-readable range name -- "2026 Q2" when `start`/`end` exactly match
    `quarter_bounds`, "2026 annual" when they exactly span the calendar year,
    else an ISO date span (e.g. "2026-05-15 - 2026-08-15"). The one place
    this text is built, so the HTML report page, the XLSX summary sheet, and
    the export filename can't independently drift on wording (same rationale
    as `format_rate_periods`/`caveat_lines`).
    """
    if start.year == end.year:
        for q in (1, 2, 3, 4):
            if (start, end) == quarter_bounds(start.year, q):
                return f"{start.year} Q{q}"
        if (start, end) == (date(start.year, 1, 1), date(start.year, 12, 31)):
            return f"{start.year} annual"
    return f"{start.isoformat()} - {end.isoformat()}"


def range_filename_slug(start: date, end: date) -> str:
    """`range_label`, made filesystem/URL-safe for a `Content-Disposition`
    filename -- "2026 Q2" -> "2026-Q2", "2026-05-15 - 2026-08-15" ->
    "2026-05-15_2026-08-15". Derives from `range_label` rather than
    reimplementing the exact-quarter/exact-year checks, so a filename can
    never disagree with the page/sheet title it names.
    """
    return range_label(start, end).replace(" - ", "_").replace(" ", "-")


def _rate_periods(month_rates: dict[int, float | None]) -> list[tuple[float, int, int]]:
    """Collapse a month->rate mapping (contiguous months, e.g. the annual
    report's Jan..Dec or a range report's intersecting months; some entries
    may be None) into contiguous (rate, first_month, last_month) spans, for a
    human-readable summary like "$0.67/mi Jan-Jun, $0.70/mi Jul-Dec" instead
    of one near-identical row per month.
    """
    periods: list[tuple[float, int, int]] = []
    for month in sorted(month_rates):
        rate = month_rates[month]
        if rate is None:
            continue
        if periods and periods[-1][0] == rate and periods[-1][2] == month - 1:
            prev_rate, first, _ = periods[-1]
            periods[-1] = (prev_rate, first, month)
        else:
            periods.append((rate, month, month))
    return periods


def build_range_report(
    trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo, start: date, end: date
) -> RangeReport:
    """Fold already-fetched trips (one row per trip, the same shape
    `TRIP_COLUMNS` selects -- `started_at` still UTC) into the report shape,
    for any `start`..`end` span within a single calendar year (quarterly
    estimate, mid-year check, or -- via `build_annual_report` -- the full
    year). Date attribution uses each trip's *local* start, converting here
    rather than trusting the caller to pre-filter, since a trip stored near a
    UTC midnight can fall on a different local date (same rule
    `sum_month_deductions`/`build_export_rows` already follow) -- a trip
    outside `[start, end]` once localized is silently excluded rather than
    mis-costed into the wrong period's report. `start > end` or a span
    crossing a year boundary raises `ValueError` -- (year, month) bucketing
    and year-aware rate-period labels aren't worth the surface area for a
    need neither motivating use case (quarterly estimates, mid-year checks)
    has ever hit.
    """
    if start > end:
        raise ValueError(f"start date {start} is after end date {end}")
    if start.year != end.year:
        raise ValueError(f"date range {start}..{end} crosses a calendar year boundary")
    year = start.year

    business_by_month: dict[int, float] = {}
    count_by_month: dict[int, int] = {}
    business_m = 0.0
    personal_m = 0.0
    nondeductible_m = 0.0
    trip_count = 0
    gap_trips = 0
    low_conf_trips = 0
    manual_trips = 0
    unclassified_trips = 0
    business_missing_purpose = 0

    # Per-vehicle business miles, bucketed by month (not just summed) for the
    # same reason business_by_month is: sum_month_deductions needs each
    # vehicle's own per-month buckets so a mid-year rate change still prices
    # each vehicle's miles at the rate in force that month, not one blended
    # rate for the year. Personal and non-deductible vehicle mileage are not
    # month-bucketed since they only feed VehicleLine.total_m, never a
    # deduction calculation.
    vehicle_business_by_month: dict[object, dict[int, float]] = {}
    personal_by_vehicle: dict[object, float] = {}
    nondeductible_by_vehicle: dict[object, float] = {}
    vehicle_names: dict[object, str] = {}

    for trip in trips:
        local_start = trip["started_at"].astimezone(tz)
        if not (start <= local_start.date() <= end):
            continue
        exclusion = trip.get("exclusion")
        category = trip["category"]
        if category == "unclassified":
            unclassified_trips += 1
        if exclusion == "not_my_vehicle":
            continue
        trip_count += 1
        month = local_start.month
        distance_m = trip["display_distance_m"]
        vehicle_name = trip.get("vehicle_name") or NO_VEHICLE_LABEL
        # Production rows always include vehicle_id. The legacy-name fallback
        # keeps the pure function friendly to older callers/tests while never
        # allowing two real DB vehicles with the same display name to merge.
        vehicle_key = trip.get("vehicle_id") if "vehicle_id" in trip else ("legacy", vehicle_name)
        vehicle_names[vehicle_key] = vehicle_name

        if exclusion == "not_deductible":
            nondeductible_m += distance_m
            nondeductible_by_vehicle[vehicle_key] = (
                nondeductible_by_vehicle.get(vehicle_key, 0.0) + distance_m
            )
        elif category == "business":
            business_by_month[month] = business_by_month.get(month, 0.0) + distance_m
            count_by_month[month] = count_by_month.get(month, 0) + 1
            business_m += distance_m
            by_month = vehicle_business_by_month.setdefault(vehicle_key, {})
            by_month[month] = by_month.get(month, 0.0) + distance_m
            if not (trip.get("purpose") or "").strip():
                business_missing_purpose += 1
        elif category == "personal":
            personal_m += distance_m
            personal_by_vehicle[vehicle_key] = personal_by_vehicle.get(vehicle_key, 0.0) + distance_m
        if trip.get("has_gap"):
            gap_trips += 1
        if trip.get("snap_status") == "low_confidence":
            low_conf_trips += 1
        if trip.get("source") == "manual":
            manual_trips += 1

    # Computed over every month the range touches (not just months with
    # business trips) so rate_periods describes the range's full rate
    # structure: a single July trip in a split-year range should still show
    # "Jan-Jun / Jul-Dec" context for where the split falls, not a lone
    # "Jul-Jul" span. For the annual report this is all 12 months; for a
    # range it's start.month..end.month (guaranteed non-decreasing since
    # start <= end within one year).
    context_month_rates: dict[int, float | None] = {
        month: rate_for(rates, year, month) for month in range(start.month, end.month + 1)
    }
    # rate_for is uniform across a year's months (a YearRate always resolves
    # to *some* rate once its year is found; it's never partially missing),
    # so checking one month tells us whether the whole range is priced.
    missing_rate = context_month_rates[start.month] is None

    months = [
        MonthLine(
            month=month,
            trip_count=count_by_month[month],
            business_m=business_by_month[month],
            rate_per_mi=context_month_rates[month],
            deduction=deduction(business_by_month[month], year, rates, month),
        )
        for month in sorted(business_by_month)
    ]

    total_deduction = sum_month_deductions(list(business_by_month.items()), year, rates)
    total_m = business_m + personal_m + nondeductible_m
    business_pct = (business_m / total_m * 100.0) if total_m > 0 else None

    vehicle_keys = sorted(
        set(vehicle_business_by_month) | set(personal_by_vehicle) | set(nondeductible_by_vehicle),
        key=lambda key: (vehicle_names[key].casefold(), str(key)),
    )
    by_vehicle = [
        VehicleLine(
            vehicle_id=vehicle_key if isinstance(vehicle_key, int) else None,
            vehicle_name=vehicle_names[vehicle_key],
            business_m=sum(vehicle_business_by_month.get(vehicle_key, {}).values()),
            personal_m=personal_by_vehicle.get(vehicle_key, 0.0),
            nondeductible_m=nondeductible_by_vehicle.get(vehicle_key, 0.0),
            total_m=(
                sum(vehicle_business_by_month.get(vehicle_key, {}).values())
                + personal_by_vehicle.get(vehicle_key, 0.0)
                + nondeductible_by_vehicle.get(vehicle_key, 0.0)
            ),
            deduction=sum_month_deductions(
                list(vehicle_business_by_month.get(vehicle_key, {}).items()), year, rates
            ),
        )
        for vehicle_key in vehicle_keys
    ]

    return RangeReport(
        year=year,
        months=months,
        business_m=business_m,
        personal_m=personal_m,
        nondeductible_m=nondeductible_m,
        business_pct=business_pct,
        total_deduction=total_deduction,
        rate_periods=_rate_periods(context_month_rates),
        trip_count=trip_count,
        caveats=ReportCaveats(
            gap_trips=gap_trips,
            low_conf_trips=low_conf_trips,
            manual_trips=manual_trips,
            unclassified_trips=unclassified_trips,
            business_missing_purpose=business_missing_purpose,
            missing_rate=missing_rate,
        ),
        by_vehicle=by_vehicle,
        start=start,
        end=end,
    )


def build_annual_report(
    trips: list[dict], rates: dict[int, YearRate], tz: ZoneInfo, year: int
) -> AnnualReport:
    """Jan 1-Dec 31 of `year`, delegated to `build_range_report` so the two
    can't drift. Returns a plain `AnnualReport` (not the `RangeReport`
    subclass) so this function's signature and output type stay exactly what
    existing callers expect.
    """
    range_report = build_range_report(trips, rates, tz, date(year, 1, 1), date(year, 12, 31))
    return AnnualReport(
        **{f.name: getattr(range_report, f.name) for f in fields(AnnualReport)}
    )
