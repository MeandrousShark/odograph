"""Shared trip SQL: the row projections and the filter every trip list uses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.geocode import GEOCODE_PRECISION

DISPLAY_DISTANCE_SQL = "COALESCE(distance_snapped_m, distance_m)"

# Place names/addresses are correlated subselects (not JOINs) so every query
# built on TRIP_COLUMNS picks them up without touching its FROM clause.
# Factored into module-level constants (rather than left inline) so
# `_trip_filter_sql`'s search predicate can reuse the exact same subselects
# instead of duplicating them in the WHERE clause.
_START_PLACE_NAME_SQL = "(SELECT name FROM places WHERE account_id = trips.account_id AND id = trips.start_place_id)"
_END_PLACE_NAME_SQL = "(SELECT name FROM places WHERE account_id = trips.account_id AND id = trips.end_place_id)"
_START_ADDRESS_SQL = (
    "(SELECT address FROM geocode_cache\n"
    f"     WHERE account_id = trips.account_id AND lat = ROUND(ST_Y(trips.start_geom::geometry)::numeric, {GEOCODE_PRECISION})\n"
    f"       AND lon = ROUND(ST_X(trips.start_geom::geometry)::numeric, {GEOCODE_PRECISION}))"
)
_END_ADDRESS_SQL = (
    "(SELECT address FROM geocode_cache\n"
    f"     WHERE account_id = trips.account_id AND lat = ROUND(ST_Y(trips.end_geom::geometry)::numeric, {GEOCODE_PRECISION})\n"
    f"       AND lon = ROUND(ST_X(trips.end_geom::geometry)::numeric, {GEOCODE_PRECISION}))"
)

# display_distance_m is the canonical "distance to show": snapped when
# available, raw as fallback (raw distance_m stays selected as the pre-snap
# baseline). distance_snapped_m comes along raw because NULL on a trip that
# does have a snapped path carries its own meaning: the match didn't cover the
# trip and its distance was declined (app/snap.py's min_coverage). The trip
# page labels and draws that case differently, and COALESCE alone can't
# distinguish it from a trip that was never snapped.
REPORT_TRIP_COLUMNS = f"""
    id, device, tracking_device_id, source::text AS source, started_at, ended_at, distance_m,
    distance_snapped_m,
    {DISPLAY_DISTANCE_SQL} AS display_distance_m,
    snap_status::text AS snap_status,
    point_count, has_gap, imported, category::text AS category,
    exclusion::text AS exclusion, purpose, notes,
    start_label, end_label,
    -- Raw place ids, not just start_place_name/end_place_name below: the
    -- trip-edit card's per-endpoint label eligibility has to mirror
    -- migrations/025_manual_trip_labels.sql's own constraint columns
    -- exactly, and a resolved place *name* is not a sound proxy for
    -- whether a place id is actually set.
    start_place_id, end_place_id,
    ST_Y(start_geom::geometry) AS start_lat, ST_X(start_geom::geometry) AS start_lon,
    ST_Y(end_geom::geometry) AS end_lat, ST_X(end_geom::geometry) AS end_lon,
    -- A custom label and a saved place can never both name the same
    -- endpoint: migrations/025_manual_trip_labels.sql requires a label's
    -- place id to be null. That makes "label, else saved-place name" an
    -- unambiguous precedence to resolve right here, so every display
    -- consumer keeps calling describe_endpoint/describe_compact_endpoint on
    -- start_place_name/end_place_name exactly as before and never has to
    -- learn that labels exist.
    COALESCE(start_label, {_START_PLACE_NAME_SQL}) AS start_place_name,
    COALESCE(end_label, {_END_PLACE_NAME_SQL}) AS end_place_name,
    vehicle_id,
    -- Subselect (not JOIN) so a deactivated vehicle still shows its name on
    -- trips that point at it, despite being absent from the default picker.
    (SELECT name FROM vehicles WHERE account_id = trips.account_id AND id = trips.vehicle_id) AS vehicle_name,
    (SELECT count(*) FROM expenses WHERE expenses.account_id = trips.account_id AND expenses.trip_id = trips.id) AS expense_count,
    (path IS NOT NULL OR path_snapped IS NOT NULL) AS has_route_geometry,
    {_START_ADDRESS_SQL} AS start_address,
    {_END_ADDRESS_SQL} AS end_address
"""

# Report consumers don't render continuity badges. Keep their source rows
# free of the predecessor/overlap lookups while trip and review queries
# retain the complete projection.
TRIP_COLUMNS = f"""{REPORT_TRIP_COLUMNS},
    snap_capacity_needed_bytes,
    -- Missing-trip detection: four near-identical subselects for the
    -- previous included trip, because one SELECT item can't reference
    -- another's alias (and a LATERAL join would mean touching every FROM
    -- clause that embeds TRIP_COLUMNS; deferred until this scales past
    -- "acceptable"). A not_my_vehicle trip remains outside missing-trip
    -- continuity, so it cannot create or suppress a warning indirectly.
    -- No `end_geom IS NOT NULL` filter: skipping an included predecessor that
    -- lacks end_geom would silently pick an even older trip, while ST_Distance
    -- against NULL is NULL, exactly "no badge".
    (SELECT ST_Distance(p.end_geom, trips.start_geom) FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_end_gap_m,
    (SELECT p.ended_at FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_ended_at,
    (SELECT ST_Y(p.end_geom::geometry) FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_end_lat,
    (SELECT ST_X(p.end_geom::geometry) FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_end_lon,
    (SELECT name FROM places WHERE account_id = trips.account_id AND id = (
       SELECT p.end_place_id FROM trips p
       WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
         AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
         AND p.started_at < trips.started_at
       ORDER BY p.started_at DESC LIMIT 1
     )) AS prev_trip_end_place_name,
    -- Suppression: a manual trip overlapping the window between the
    -- predecessor's end and this trip's start clears the badge. Re-derives
    -- the predecessor's ended_at once more purely to test the overlap.
    EXISTS (
      SELECT 1 FROM trips m
      WHERE m.account_id = trips.account_id AND m.source = 'manual' AND m.started_at < trips.started_at
        AND m.exclusion IS DISTINCT FROM 'not_my_vehicle'
        AND m.ended_at > (
          SELECT p.ended_at FROM trips p
          WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
            AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
            AND p.started_at < trips.started_at
          ORDER BY p.started_at DESC LIMIT 1
        )
    ) AS missing_trip_covered,
    -- The predecessor's ID and the stored acknowledgement fingerprints let
    -- app/trip_warnings.py decide which list warnings stay acknowledged.
    (SELECT p.id FROM trips p
     WHERE p.account_id = trips.account_id AND p.tracking_device_id = trips.tracking_device_id AND p.source = 'detected'
       AND p.exclusion IS DISTINCT FROM 'not_my_vehicle'
       AND p.started_at < trips.started_at
     ORDER BY p.started_at DESC LIMIT 1) AS prev_trip_id,
    (SELECT a.trip_started_at FROM trip_warning_acks a
     WHERE a.account_id = trips.account_id AND a.trip_id = trips.id AND a.kind = 'recording_gap') AS gap_ack_trip_started_at,
    (SELECT a.trip_ended_at FROM trip_warning_acks a
     WHERE a.account_id = trips.account_id AND a.trip_id = trips.id AND a.kind = 'recording_gap') AS gap_ack_trip_ended_at,
    (SELECT a.trip_started_at FROM trip_warning_acks a
     WHERE a.account_id = trips.account_id AND a.trip_id = trips.id AND a.kind = 'missing_trip') AS missing_ack_trip_started_at,
    (SELECT a.prev_trip_id FROM trip_warning_acks a
     WHERE a.account_id = trips.account_id AND a.trip_id = trips.id AND a.kind = 'missing_trip') AS missing_ack_prev_trip_id,
    (SELECT a.prev_ended_at FROM trip_warning_acks a
     WHERE a.account_id = trips.account_id AND a.trip_id = trips.id AND a.kind = 'missing_trip') AS missing_ack_prev_ended_at
"""

CATEGORIES = ("business", "personal", "unclassified")
EXCLUSIONS = ("not_my_vehicle", "not_deductible")

# The vehicle filter's `<select>` has three states, not two: no filter, one
# specific vehicle, or "only trips with no vehicle at all". The third can't
# be an int, so it needs its own value distinct from every real vehicle id;
# reusing the query string's own "none" spelling keeps _parse_vehicle_id and
# _trip_filter_sql agreeing on one literal instead of a second constant.
VEHICLE_FILTER_UNASSIGNED = "none"

# Same shape as VEHICLE_FILTER_UNASSIGNED above: the exclusion filter also
# has a state -- "normal trips only" -- that isn't one of EXCLUSIONS' enum
# values, so it needs a sentinel distinct from every real value rather than
# being folded into the enum check in _trip_filter_sql.
EXCLUSION_FILTER_NONE = "none"


def _escape_ilike_term(term: str) -> str:
    """Escape `%`, `_`, and backslash so a raw search term matches those
    characters literally instead of acting as ILIKE wildcards/escape
    introducer. Backslash must be escaped first, or escaping % and _ would
    double-escape the backslashes this step just inserted.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _trip_filter_sql(
    category: str, from_dt: datetime | None, to_dt: datetime | None,
    vehicle_id: int | None | Literal["none"] = None,
    q: str = "",
    exclusion: str = "",
    *, owner_id: int,
) -> tuple[str, list]:
    """Build a `WHERE` clause + params list for filtering trips by category,
    vehicle, date range, exclusion state, and/or a free-text search term.
    Shared by the trip list, `/export`, the month pager, and `/review` so
    none of them can drift apart.

    The search term matches notes, purpose, either place name, either custom
    endpoint label, or either cached address, case-insensitively and on
    substrings -- the same four place/address subselects `TRIP_COLUMNS`
    selects, reused here rather than duplicated, plus the raw
    `start_label`/`end_label` columns `TRIP_COLUMNS` also selects directly,
    so the predicate can never see different text than what the archive
    displays. An empty or whitespace-only term is no search at all, so the
    generated SQL (and every URL built from it) is unchanged from before
    this filter existed.

    `exclusion` follows the same forgiving-on-junk posture as `category` and
    `vehicle_id`: an empty string or anything outside `EXCLUSIONS` is no
    filter at all, so a page with no exclusion filter selected still builds
    the exact SQL and params this function produced before the parameter
    existed. `EXCLUSION_FILTER_NONE` is the one non-enum value that isn't
    junk, the same role `VEHICLE_FILTER_UNASSIGNED` plays for `vehicle_id`:
    it means "normal trips only", i.e. `exclusion IS NULL`.
    """
    if type(owner_id) is not int or not 1 <= owner_id <= 2**63 - 1:
        raise ValueError("owner_id must be a positive bigint")
    clauses = ["trips.account_id = %s"]
    params: list = [owner_id]
    if category in CATEGORIES:
        clauses.append("category = %s")
        params.append(category)
    if vehicle_id == VEHICLE_FILTER_UNASSIGNED:
        clauses.append("vehicle_id IS NULL")
    elif vehicle_id is not None:
        clauses.append("vehicle_id = %s")
        params.append(vehicle_id)
    if exclusion == EXCLUSION_FILTER_NONE:
        clauses.append("exclusion IS NULL")
    elif exclusion in EXCLUSIONS:
        clauses.append("exclusion = %s")
        params.append(exclusion)
    if from_dt is not None:
        clauses.append("started_at >= %s")
        params.append(from_dt)
    if to_dt is not None:
        clauses.append("started_at < %s")
        params.append(to_dt)
    term = q.strip()
    if term:
        pattern = f"%{_escape_ilike_term(term)}%"
        clauses.append(
            "(notes ILIKE %s ESCAPE '\\' OR purpose ILIKE %s ESCAPE '\\' OR "
            f"{_START_PLACE_NAME_SQL} ILIKE %s ESCAPE '\\' OR "
            f"{_END_PLACE_NAME_SQL} ILIKE %s ESCAPE '\\' OR "
            "start_label ILIKE %s ESCAPE '\\' OR end_label ILIKE %s ESCAPE '\\' OR "
            f"{_START_ADDRESS_SQL} ILIKE %s ESCAPE '\\' OR "
            f"{_END_ADDRESS_SQL} ILIKE %s ESCAPE '\\')"
        )
        params.extend([pattern] * 8)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


@dataclass(frozen=True)
class TripFilter:
    """One trip-list filter set, applied through `_trip_filter_sql`.

    Values keep that function's forgiving meaning: an empty or unknown
    category or exclusion is no filter. Callers that must reject unknown
    values validate before building this.
    """
    category: str = ""
    from_dt: datetime | None = None
    to_dt: datetime | None = None
    vehicle_id: int | None | Literal["none"] = None
    q: str = ""
    exclusion: str = ""

    def where(self, owner_id: int) -> tuple[str, list]:
        return _trip_filter_sql(
            self.category, self.from_dt, self.to_dt, self.vehicle_id,
            q=self.q, exclusion=self.exclusion, owner_id=owner_id,
        )
