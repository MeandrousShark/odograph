"""Odometer readings + GPS-vs-odometer reconciliation. Pure core, no I/O —
unit-testable the same way `app/report.py`/`app/stats.py` are; `app/ui.py`
fetches readings/trips with plain SQL and hands them here, and
`app/report.py`'s `build_annual_report` is deliberately untouched
(reconciliation is computed separately and passed to the report
template/export as its own object).

Deliberately not named near `app/detector/reconcile.py` — that module
reconciles detected trip *segments* against each other (a different,
unrelated meaning of "reconcile").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.rates import METERS_PER_MILE


@dataclass(frozen=True)
class OdometerReading:
    recorded_at: datetime
    odometer_m: float


@dataclass(frozen=True)
class ReconInterval:
    start: datetime
    end: datetime
    odometer_delta_m: float
    detected_m: float
    coverage: float | None   # detected/odometer_delta; None if delta <= 0
    gap_m: float              # odometer_delta - detected (may be negative if GPS over-reads)
    data_error: bool          # True when odometer_delta <= 0 (reading decreased/duplicate)


@dataclass(frozen=True)
class ReconResult:
    intervals: list[ReconInterval]
    total_odometer_delta_m: float   # summed over intervals with delta > 0 only
    total_detected_m: float          # summed over the same intervals, so total_coverage stays consistent
    total_coverage: float | None


def reconcile(
    readings: list[OdometerReading], trip_starts_dists: list[tuple[datetime, float]]
) -> ReconResult:
    """Diff consecutive odometer readings against the GPS-detected distance
    driven in between. `trip_starts_dists` is `(started_at, display_distance_m)`
    for every trip to consider — the caller is responsible for restricting
    that list to one vehicle (this function has no vehicle concept at all,
    so vehicle isolation is entirely a caller-side filter) and for using the
    display distance (snapped-or-raw, `TRIP_COLUMNS`' `display_distance_m`)
    so this matches every other distance shown in the app.

    A trip is attributed to the interval its `started_at` falls in
    (`[start, end)`), consistent with how the rest of the app buckets trips
    by their start; a trip straddling a reading lands wholly in the earlier
    interval. Fewer than two readings produces no intervals at all (nothing
    to diff) rather than raising — the settings page shows a hint instead of
    a table in that case.

    `odometer_delta <= 0` (the reading went backwards, or a duplicate
    timestamp collapsed to the same value) is flagged `data_error=True` with
    `coverage=None` rather than computing a division-by-zero or a nonsense
    negative percentage — an odometer should never decrease, so this is
    treated as bad data to surface, not driving to explain.

    `coverage > 1.0` (GPS distance exceeding the odometer delta — road-
    snapping error, or a trip whose start-time attribution pulled it into
    the wrong interval) is deliberately **not** clamped to 100%: an
    over-read is a real signal about data quality that clamping would hide.
    """
    ordered = sorted(readings, key=lambda r: r.recorded_at)
    intervals: list[ReconInterval] = []
    total_delta = 0.0
    total_detected = 0.0
    for start_r, end_r in zip(ordered, ordered[1:]):
        delta = end_r.odometer_m - start_r.odometer_m
        detected = sum(
            dist for started_at, dist in trip_starts_dists
            if start_r.recorded_at <= started_at < end_r.recorded_at
        )
        data_error = delta <= 0
        coverage = None if data_error else (detected / delta)
        gap = delta - detected
        intervals.append(ReconInterval(
            start=start_r.recorded_at, end=end_r.recorded_at,
            odometer_delta_m=delta, detected_m=detected,
            coverage=coverage, gap_m=gap, data_error=data_error,
        ))
        if not data_error:
            total_delta += delta
            total_detected += detected

    total_coverage = (total_detected / total_delta) if total_delta > 0 else None
    return ReconResult(
        intervals=intervals,
        total_odometer_delta_m=total_delta,
        total_detected_m=total_detected,
        total_coverage=total_coverage,
    )


@dataclass(frozen=True)
class VehicleCoverage:
    """One report-year coverage line, spanning whichever of the year's
    readings exist (not necessarily the full year — `fully_bracketed`
    says whether they do).
    """
    vehicle_name: str
    span_start: datetime
    span_end: datetime
    coverage: float | None
    gap_m: float
    fully_bracketed: bool


def vehicle_coverage_for_report(
    readings_by_vehicle: dict[object, list[OdometerReading]],
    trips_by_vehicle: dict[object, list[tuple[datetime, float]]],
    year_start: datetime, next_year_start: datetime,
) -> list[VehicleCoverage]:
    """Per-vehicle coverage summary for the annual report, computed entirely
    outside `build_annual_report` so that function's signature/behavior —
    and the report/export paths that lean on it — never
    change here. Only vehicles with >=2 readings *in the report year*
    produce a line; a vehicle with 0 or 1 is silently omitted (no crash, no
    empty/misleading row) rather than shown with nothing to say.

    `fully_bracketed` is true only when the earliest reading lands exactly
    at `year_start` and the latest at/after `next_year_start` — since
    `readings_by_vehicle` is expected to already be filtered to readings
    *within* the report year, in practice this is almost always false
    (a reading exactly at midnight Jan 1 both years is rare), which is the
    honest answer: an annual coverage % is normally over a partial span, and
    the caller surfaces that rather than implying full-year coverage it
    can't back up.
    """
    lines: list[VehicleCoverage] = []
    for vehicle_key in sorted(readings_by_vehicle, key=str):
        readings = readings_by_vehicle[vehicle_key]
        if len(readings) < 2:
            continue
        result = reconcile(readings, trips_by_vehicle.get(vehicle_key, []))
        if result.total_odometer_delta_m <= 0:
            continue  # every interval was a data error; nothing meaningful to show
        ordered = sorted(readings, key=lambda r: r.recorded_at)
        span_start, span_end = ordered[0].recorded_at, ordered[-1].recorded_at
        vehicle_name = vehicle_key[1] if isinstance(vehicle_key, tuple) else str(vehicle_key)
        lines.append(VehicleCoverage(
            vehicle_name=vehicle_name,
            span_start=span_start,
            span_end=span_end,
            coverage=result.total_coverage,
            gap_m=result.total_odometer_delta_m - result.total_detected_m,
            fully_bracketed=(span_start <= year_start and span_end >= next_year_start),
        ))
    return lines


def coverage_line(line: VehicleCoverage) -> str:
    """"GPS captured X% of odometer miles (Y mi unaccounted)" wording,
    shared by the HTML report and its XLSX export so the two can't drift on
    phrasing — the same convention `app.report.format_rate_periods`/
    `caveat_lines` already follow for their own shared sentences.
    """
    pct = f"{line.coverage * 100:.1f}%" if line.coverage is not None else "—"
    gap_mi = line.gap_m / METERS_PER_MILE
    text = f"{line.vehicle_name}: GPS captured {pct} of odometer miles ({gap_mi:.1f} mi unaccounted)"
    if not line.fully_bracketed:
        text += " — based on a partial-year reading span, not the full year"
    return text


def latest_quarter_start(now: datetime, hour: int) -> datetime:
    """Return the latest calendar-quarter start (Jan/Apr/Jul/Oct 1st at
    `hour`, in `now`'s timezone) that is <= `now` — the same "latest local
    boundary <= now" shape as `app.nudge.latest_window_end`, factored out
    here so the odometer reminder worker can stay a thin DB/ntfy wrapper
    around a pure decision.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    quarter_month = ((now.month - 1) // 3) * 3 + 1  # 1, 4, 7, or 10
    candidate = datetime(now.year, quarter_month, 1, hour, tzinfo=now.tzinfo)
    if now >= candidate:
        return candidate
    prev_month = quarter_month - 3
    prev_year = now.year
    if prev_month <= 0:
        prev_month += 12
        prev_year -= 1
    return datetime(prev_year, prev_month, 1, hour, tzinfo=now.tzinfo)


def vehicles_due_for_reminder(
    active_vehicles: list[tuple[int, str]], vehicle_ids_with_reading: set[int]
) -> list[str]:
    """Active vehicles with no odometer reading dated on/after the current
    quarter start, sorted by name. Pure so the worker's two small DB reads
    (active vehicle id/name pairs, and which of those ids already logged a
    reading this quarter) can be exercised without a database — only the
    reads themselves, and the ntfy POST, are I/O.
    """
    return sorted(name for vid, name in active_vehicles if vid not in vehicle_ids_with_reading)
