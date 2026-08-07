"""Portable data export/import: a versioned JSON snapshot of the ledger core
(vehicles, places, tag_rules, mileage_rates, trips, expenses,
odometer_readings, and the app_settings singleton) that lets an operator move
data between instances instead of the Postgres schema being the only way out.
Route/path geometry, raw points, trip_boundary_overrides, and import across a
differing schema_version are all deliberately out of scope for this bundle
format -- none of them are represented here.

`build_export_bundle` is pure -- already-fetched DB rows in, a JSON-shaped
dict out, no DB/IO -- so it's unit-testable the same way `build_export_rows`
is in `app/export.py`. `normalize_bundle` is pure too: it validates an
uploaded bundle's shape and in-bundle references without touching the
database, so a malformed upload is rejected before any query runs. Import
itself needs the database (id remapping, the clean-target precondition) and
lives in this module rather than a separate one, since it shares the bundle
format and validation with export.

Every row another row can reference (vehicles, places) carries a bundle-local
`$id` -- the source row's real database id, reused only as a cross-reference
key inside the file. Trips carry a `$id` for the same reason even though
nothing in this increment's bundle references a trip. Import never reuses a
source id: it inserts fresh rows and builds its own `$id -> new id` map per
table, rewriting every reference through that map before the dependent rows
are inserted. That's what makes import safe against a target whose sequences
are at different values than the source.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg import Rollback, errors
from psycopg.rows import dict_row
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse, Response

from app.auth import check_form_csrf, require_user
from app.detector.runner import ADVISORY_LOCK_KEY
from app.expenses import EXPENSE_CATEGORIES, EXPENSE_TREATMENTS
from app.places_desc import PLACE_KINDS

log = logging.getLogger(__name__)

FORMAT = "odograph-portable"
FORMAT_VERSION = 1

TRIP_SOURCES = ("detected", "manual")
TRIP_CATEGORIES = ("unclassified", "business", "personal")
TAG_RULE_CATEGORIES = ("business", "personal")
TAG_SOURCES = ("human", "rule")

# 008_vehicles.sql / 003_places.sql seed these exact rows on every fresh
# migration. The clean-target precondition compares against this content
# (not just table counts), so a target with e.g. an edited default tag_rule
# is refused rather than silently accepted.
SEEDED_VEHICLE = {
    "name": "My Car", "make": None, "model": None, "plate": None,
    "is_default": True, "active": True,
}
SEEDED_TAG_RULES = (
    {"a_place": None, "a_kind": "home", "b_place": None, "b_kind": "work", "category": "personal"},
    {"a_place": None, "a_kind": "work", "b_place": None, "b_kind": "work", "category": "business"},
)


class PortableImportError(Exception):
    """Carries a structured, JSON-able report to the route -- raised for
    every refused import (bad bundle, version mismatch, non-clean target, or
    an unexpected DB error after validation) so the route has one place to
    turn a failure into a response instead of reconstructing detail from a
    generic exception.
    """

    def __init__(self, error: str, detail: str, status_code: int = 409, **extra: Any):
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.status_code = status_code
        self.extra = extra

    def to_response(self) -> dict:
        return {"ok": False, "error": self.error, "detail": self.detail, **self.extra}


# ---------------------------------------------------------------------------
# Export: pure shaping
# ---------------------------------------------------------------------------

def build_export_bundle(
    *, vehicles: list[dict], places: list[dict], tag_rules: list[dict],
    mileage_rates: list[dict], trips: list[dict], expenses: list[dict],
    odometer_readings: list[dict], settings: dict, schema_version: int,
    exported_at: datetime,
) -> dict:
    """Rows are already-fetched dicts from the DB fetch helpers below (or an
    equivalent shape in tests) -- this function only reshapes them into the
    bundle format and converts non-JSON-native types (datetime/date/Decimal)
    to strings, so `json.dumps(build_export_bundle(...))` always succeeds
    without a custom encoder.
    """
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "schema_version": schema_version,
        "exported_at": exported_at.isoformat(),
        "vehicles": [_export_vehicle(v) for v in vehicles],
        "places": [_export_place(p) for p in places],
        "tag_rules": [_export_tag_rule(r) for r in tag_rules],
        "mileage_rates": [_export_mileage_rate(r) for r in mileage_rates],
        "trips": [_export_trip(t) for t in trips],
        "expenses": [_export_expense(e) for e in expenses],
        "odometer_readings": [_export_odometer_reading(r) for r in odometer_readings],
        "settings": {
            "auto_assign_default_vehicle": bool(settings["auto_assign_default_vehicle"]),
        },
    }


def _export_vehicle(row: dict) -> dict:
    return {
        "$id": row["id"], "name": row["name"], "make": row["make"],
        "model": row["model"], "plate": row["plate"],
        "is_default": row["is_default"], "active": row["active"],
    }


def _export_place(row: dict) -> dict:
    return {
        "$id": row["id"], "name": row["name"], "kind": row["kind"],
        "lat": row["lat"], "lon": row["lon"], "radius_m": row["radius_m"],
    }


def _export_tag_rule(row: dict) -> dict:
    # a_place/b_place are already the source's real vehicle/place ids, which
    # is exactly what $id is defined to be -- no remapping needed at export
    # time, only at import.
    return {
        "a_place": row["a_place"], "a_kind": row["a_kind"],
        "b_place": row["b_place"], "b_kind": row["b_kind"],
        "category": row["category"],
    }


def _export_mileage_rate(row: dict) -> dict:
    return {
        "year": row["year"], "rate_per_mi": row["rate_per_mi"],
        "rate_h2_per_mi": row["rate_h2_per_mi"], "h2_start_month": row["h2_start_month"],
    }


def _export_trip(row: dict) -> dict:
    return {
        "$id": row["id"], "device": row["device"], "source": row["source"],
        "started_at": row["started_at"].isoformat(), "ended_at": row["ended_at"].isoformat(),
        "distance_m": row["distance_m"], "has_gap": row["has_gap"],
        "category": row["category"], "purpose": row["purpose"], "notes": row["notes"],
        "vehicle": row["vehicle_id"], "start_place": row["start_place_id"],
        "end_place": row["end_place_id"], "tag_source": row["tag_source"],
    }


def _export_expense(row: dict) -> dict:
    # Money round-trips as a string, not a float: numeric(12,2) is exact and
    # a JSON float would risk a cent-level drift on the way back in (see
    # app/export.py's module docstring for the same reasoning).
    return {
        "vehicle": row["vehicle_id"], "incurred_on": row["incurred_on"].isoformat(),
        "category": row["category"], "amount": str(row["amount"]),
        "treatment": row["treatment"], "notes": row["notes"],
    }


def _export_odometer_reading(row: dict) -> dict:
    return {
        "vehicle": row["vehicle_id"], "recorded_at": row["recorded_at"].isoformat(),
        "odometer_m": row["odometer_m"], "note": row["note"],
    }


# ---------------------------------------------------------------------------
# Export: DB fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_schema_version(conn) -> int:
    cur = await conn.execute("SELECT COALESCE(max(version), 0) FROM schema_migrations")
    row = await cur.fetchone()
    return row[0]


async def _fetch_export_vehicles(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, name, make, model, plate, is_default, active FROM vehicles ORDER BY id"
    )
    return await cur.fetchall()


async def _fetch_export_places(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, name, kind::text AS kind, "
        " ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon, radius_m "
        "FROM places ORDER BY id"
    )
    return await cur.fetchall()


async def _fetch_export_tag_rules(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules ORDER BY id"
    )
    return await cur.fetchall()


async def _fetch_export_mileage_rates(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT year, rate_per_mi::float AS rate_per_mi, "
        " rate_h2_per_mi::float AS rate_h2_per_mi, h2_start_month "
        "FROM mileage_rates ORDER BY year"
    )
    return await cur.fetchall()


async def _fetch_export_trips(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, device, source::text AS source, started_at, ended_at, "
        # The distance a report actually counts -- snapped when available,
        # raw otherwise -- travels with the trip even though the path
        # geometry that produced a snapped distance does not; recomputing it
        # without that geometry isn't possible, and re-exporting the raw
        # figure instead would silently change a source instance's own
        # report totals on round trip.
        " COALESCE(distance_snapped_m, distance_m) AS distance_m, has_gap, "
        " category::text AS category, purpose, notes, vehicle_id, "
        " start_place_id, end_place_id, tag_source::text AS tag_source "
        "FROM trips ORDER BY id"
    )
    return await cur.fetchall()


async def _fetch_export_expenses(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT vehicle_id, incurred_on, category::text AS category, amount, "
        " treatment::text AS treatment, notes FROM expenses ORDER BY id"
    )
    return await cur.fetchall()


async def _fetch_export_odometer_readings(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT vehicle_id, recorded_at, odometer_m, note FROM odometer_readings ORDER BY id"
    )
    return await cur.fetchall()


async def _fetch_export_settings(conn) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT auto_assign_default_vehicle FROM app_settings WHERE id = 1")
    return await cur.fetchone()


# ---------------------------------------------------------------------------
# Import: pure bundle validation/normalization
# ---------------------------------------------------------------------------

def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive timestamp is ambiguous across instances in different local
    # timezones; every timestamptz column this bundle feeds requires one.
    return dt if dt.tzinfo is not None else None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_amount(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        raw = Decimal(value)
    except InvalidOperation:
        return None
    if not raw.is_finite() or raw <= 0 or raw > Decimal("9999999999.99"):
        return None
    # Same precision rule app/ui.py's _parse_expense_input enforces on a
    # hand-typed amount: reject rather than silently round a bundle value
    # that carries more than 2 decimal places.
    quantized = raw.quantize(Decimal("0.01"))
    return quantized if quantized == raw else None


def _parse_finite_number(
    value: Any, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    """Extends `_parse_amount`'s `is_finite()` rule to every float field
    below: `json.loads` accepts the bare `NaN`/`Infinity` tokens JSON itself
    doesn't allow, and a plain `< 0`/`<= 0` bound check lets a non-finite
    value straight through (Postgres even sorts NaN as greater than every
    real number, so a `CHECK (x > 0)` column doesn't catch it either).
    `minimum`/`maximum` are an inclusive floor/ceiling: distance_m and
    odometer_m use only `minimum=0`, while place lat/lon use both to enforce
    the `[-180 -90, 180 90]` range PostGIS's geography cast also enforces, so
    an out-of-range coordinate is a named issue here instead of an opaque
    insert_failed at the geography cast.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _is_optional_str(value: Any) -> bool:
    """`None` or `str` is the contract for every optional text field below
    (make, model, plate, purpose, notes, note): each is passed straight
    through to psycopg with no other transformation, so a dict, number, or
    list here would otherwise reach the database and fail there, either when
    psycopg can't adapt the value to text or when the column itself rejects
    it -- both opaque insert_failed instead of a named issue here.
    """
    return value is None or isinstance(value, str)


def _references_known_id(value: Any, known_ids: set[int]) -> bool:
    """`bool` is a subclass of `int` in Python, so `True in {1}` is True and
    `hash(True) == hash(1)` -- a plain `value in known_ids` membership test
    lets a `$id` cross-reference field silently resolve `true`/`false` to
    whichever row has `$id` 1/0, attaching e.g. a trip to the wrong vehicle
    instead of being refused.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value in known_ids


def _normalize_vehicles(raw: Any, issues: list[str]) -> tuple[list[dict], set[int]]:
    if not isinstance(raw, list):
        issues.append("vehicles must be a list")
        return [], set()
    if not raw:
        issues.append(
            "vehicles must contain at least one vehicle; import replaces the target's "
            "vehicles table entirely, so an empty list would leave it with none"
        )
        return [], set()
    out: list[dict] = []
    seen_ids: set[int] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"vehicles[{i}] must be an object")
            continue
        rid = row.get("$id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            issues.append(f"vehicles[{i}].$id must be an integer")
            continue
        if rid in seen_ids:
            issues.append(f"vehicles[{i}].$id {rid} is a duplicate")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(f"vehicles[{i}].name must be a non-empty string")
            continue
        make, model, plate = row.get("make"), row.get("model"), row.get("plate")
        if not _is_optional_str(make):
            issues.append(f"vehicles[{i}].make must be a string or null")
            continue
        if not _is_optional_str(model):
            issues.append(f"vehicles[{i}].model must be a string or null")
            continue
        if not _is_optional_str(plate):
            issues.append(f"vehicles[{i}].plate must be a string or null")
            continue
        is_default = row.get("is_default", False)
        if not isinstance(is_default, bool):
            issues.append(f"vehicles[{i}].is_default must be a boolean")
            continue
        active = row.get("active", True)
        if not isinstance(active, bool):
            issues.append(f"vehicles[{i}].active must be a boolean")
            continue
        seen_ids.add(rid)
        out.append({
            "$id": rid, "name": name,
            "make": make, "model": model, "plate": plate,
            "is_default": is_default, "active": active,
        })
    # vehicles_one_default_idx is a partial unique index on is_default; two
    # vehicles both true would otherwise reach the database and surface as an
    # opaque insert_failed instead of a named issue here.
    default_count = sum(1 for v in out if v["is_default"])
    if default_count > 1:
        issues.append(
            f"vehicles: at most one vehicle may have is_default true, found {default_count}"
        )
    return out, seen_ids


def _normalize_places(raw: Any, issues: list[str]) -> tuple[list[dict], set[int]]:
    if not isinstance(raw, list):
        issues.append("places must be a list")
        return [], set()
    out: list[dict] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"places[{i}] must be an object")
            continue
        rid = row.get("$id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            issues.append(f"places[{i}].$id must be an integer")
            continue
        if rid in seen_ids:
            issues.append(f"places[{i}].$id {rid} is a duplicate")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(f"places[{i}].name must be a non-empty string")
            continue
        if name in seen_names:
            issues.append(f"places[{i}].name {name!r} is a duplicate")
            continue
        kind = row.get("kind")
        if kind not in PLACE_KINDS:
            issues.append(f"places[{i}].kind must be one of {PLACE_KINDS}")
            continue
        lat_raw, lon_raw = row.get("lat"), row.get("lon")
        if _parse_finite_number(lat_raw) is None or _parse_finite_number(lon_raw) is None:
            issues.append(f"places[{i}].lat/lon must be finite numbers")
            continue
        lat = _parse_finite_number(lat_raw, minimum=-90, maximum=90)
        if lat is None:
            issues.append(f"places[{i}].lat must be between -90 and 90")
            continue
        lon = _parse_finite_number(lon_raw, minimum=-180, maximum=180)
        if lon is None:
            issues.append(f"places[{i}].lon must be between -180 and 180")
            continue
        radius_m = _parse_finite_number(row.get("radius_m", 150))
        if radius_m is None or radius_m <= 0:
            issues.append(f"places[{i}].radius_m must be a positive number")
            continue
        seen_ids.add(rid)
        seen_names.add(name)
        out.append({
            "$id": rid, "name": name, "kind": kind,
            "lat": lat, "lon": lon, "radius_m": radius_m,
        })
    return out, seen_ids


def _normalize_tag_rules(raw: Any, issues: list[str], place_ids: set[int]) -> list[dict]:
    if not isinstance(raw, list):
        issues.append("tag_rules must be a list")
        return []
    out: list[dict] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"tag_rules[{i}] must be an object")
            continue
        a_place, a_kind = row.get("a_place"), row.get("a_kind")
        b_place, b_kind = row.get("b_place"), row.get("b_kind")
        category = row.get("category")
        if a_place is not None and not _references_known_id(a_place, place_ids):
            issues.append(f"tag_rules[{i}].a_place references unknown place $id {a_place}")
            continue
        if b_place is not None and not _references_known_id(b_place, place_ids):
            issues.append(f"tag_rules[{i}].b_place references unknown place $id {b_place}")
            continue
        if a_place is not None and a_kind is not None:
            issues.append(f"tag_rules[{i}] cannot set both a_place and a_kind")
            continue
        if b_place is not None and b_kind is not None:
            issues.append(f"tag_rules[{i}] cannot set both b_place and b_kind")
            continue
        if a_kind is not None and a_kind not in PLACE_KINDS:
            issues.append(f"tag_rules[{i}].a_kind must be one of {PLACE_KINDS} or null")
            continue
        if b_kind is not None and b_kind not in PLACE_KINDS:
            issues.append(f"tag_rules[{i}].b_kind must be one of {PLACE_KINDS} or null")
            continue
        if a_place is None and a_kind is None and b_place is None and b_kind is None:
            issues.append(f"tag_rules[{i}] must constrain at least one side")
            continue
        if category not in TAG_RULE_CATEGORIES:
            issues.append(f"tag_rules[{i}].category must be one of {TAG_RULE_CATEGORIES}")
            continue
        out.append({
            "a_place": a_place, "a_kind": a_kind, "b_place": b_place, "b_kind": b_kind,
            "category": category,
        })
    return out


def _normalize_mileage_rates(raw: Any, issues: list[str]) -> list[dict]:
    if not isinstance(raw, list):
        issues.append("mileage_rates must be a list")
        return []
    out: list[dict] = []
    seen_years: set[int] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"mileage_rates[{i}] must be an object")
            continue
        year = row.get("year")
        if not isinstance(year, int) or isinstance(year, bool):
            issues.append(f"mileage_rates[{i}].year must be an integer")
            continue
        if year in seen_years:
            issues.append(f"mileage_rates[{i}].year {year} is a duplicate")
            continue
        rate = _parse_finite_number(row.get("rate_per_mi"))
        if rate is None or rate <= 0:
            issues.append(f"mileage_rates[{i}].rate_per_mi must be a positive number")
            continue
        raw_h2_rate, h2_month = row.get("rate_h2_per_mi"), row.get("h2_start_month")
        if (raw_h2_rate is None) != (h2_month is None):
            issues.append(
                f"mileage_rates[{i}]: rate_h2_per_mi and h2_start_month must be set together"
            )
            continue
        h2_rate = None
        if raw_h2_rate is not None:
            h2_rate = _parse_finite_number(raw_h2_rate)
            if h2_rate is None or h2_rate <= 0:
                issues.append(f"mileage_rates[{i}].rate_h2_per_mi must be a positive number")
                continue
        if h2_month is not None and (
            isinstance(h2_month, bool) or not isinstance(h2_month, int) or not (1 <= h2_month <= 12)
        ):
            issues.append(f"mileage_rates[{i}].h2_start_month must be between 1 and 12")
            continue
        seen_years.add(year)
        out.append({
            "year": year, "rate_per_mi": rate,
            "rate_h2_per_mi": h2_rate,
            "h2_start_month": h2_month,
        })
    return out


def _normalize_trips(
    raw: Any, issues: list[str], vehicle_ids: set[int], place_ids: set[int]
) -> list[dict]:
    if not isinstance(raw, list):
        issues.append("trips must be a list")
        return []
    out: list[dict] = []
    seen_ids: set[int] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"trips[{i}] must be an object")
            continue
        rid = row.get("$id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            issues.append(f"trips[{i}].$id must be an integer")
            continue
        if rid in seen_ids:
            issues.append(f"trips[{i}].$id {rid} is a duplicate")
            continue
        device = row.get("device")
        if not isinstance(device, str) or not device:
            issues.append(f"trips[{i}].device must be a non-empty string")
            continue
        source = row.get("source")
        if source not in TRIP_SOURCES:
            issues.append(f"trips[{i}].source must be one of {TRIP_SOURCES}")
            continue
        started_at = _parse_datetime(row.get("started_at"))
        if started_at is None:
            issues.append(f"trips[{i}].started_at must be an ISO 8601 timestamp with a UTC offset")
            continue
        ended_at = _parse_datetime(row.get("ended_at"))
        if ended_at is None:
            issues.append(f"trips[{i}].ended_at must be an ISO 8601 timestamp with a UTC offset")
            continue
        # >=, not >: a zero-duration trip (started_at == ended_at) is
        # legitimate and must still be accepted.
        if ended_at < started_at:
            issues.append(f"trips[{i}].ended_at must not be before trips[{i}].started_at")
            continue
        distance_m = _parse_finite_number(row.get("distance_m"), minimum=0)
        if distance_m is None:
            issues.append(f"trips[{i}].distance_m must be a non-negative number")
            continue
        category = row.get("category")
        if category not in TRIP_CATEGORIES:
            issues.append(f"trips[{i}].category must be one of {TRIP_CATEGORIES}")
            continue
        tag_source = row.get("tag_source")
        if tag_source is not None and tag_source not in TAG_SOURCES:
            issues.append(f"trips[{i}].tag_source must be one of {TAG_SOURCES} or null")
            continue
        has_gap = row.get("has_gap", False)
        if not isinstance(has_gap, bool):
            issues.append(f"trips[{i}].has_gap must be a boolean")
            continue
        purpose = row.get("purpose")
        if not _is_optional_str(purpose):
            issues.append(f"trips[{i}].purpose must be a string or null")
            continue
        notes = row.get("notes")
        if not _is_optional_str(notes):
            issues.append(f"trips[{i}].notes must be a string or null")
            continue
        vehicle = row.get("vehicle")
        if vehicle is not None and not _references_known_id(vehicle, vehicle_ids):
            issues.append(f"trips[{i}].vehicle references unknown vehicle $id {vehicle}")
            continue
        start_place = row.get("start_place")
        if start_place is not None and not _references_known_id(start_place, place_ids):
            issues.append(f"trips[{i}].start_place references unknown place $id {start_place}")
            continue
        end_place = row.get("end_place")
        if end_place is not None and not _references_known_id(end_place, place_ids):
            issues.append(f"trips[{i}].end_place references unknown place $id {end_place}")
            continue
        seen_ids.add(rid)
        out.append({
            "$id": rid, "device": device, "source": source,
            "started_at": started_at, "ended_at": ended_at, "distance_m": distance_m,
            "has_gap": has_gap, "category": category,
            "purpose": purpose, "notes": notes,
            "vehicle": vehicle, "start_place": start_place, "end_place": end_place,
            "tag_source": tag_source,
        })
    return out


def _normalize_expenses(raw: Any, issues: list[str], vehicle_ids: set[int]) -> list[dict]:
    if not isinstance(raw, list):
        issues.append("expenses must be a list")
        return []
    out: list[dict] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"expenses[{i}] must be an object")
            continue
        vehicle = row.get("vehicle")
        if not _references_known_id(vehicle, vehicle_ids):
            issues.append(f"expenses[{i}].vehicle references unknown vehicle $id {vehicle}")
            continue
        incurred_on = _parse_date(row.get("incurred_on"))
        if incurred_on is None:
            issues.append(f"expenses[{i}].incurred_on must be an ISO 8601 date")
            continue
        category = row.get("category")
        if category not in EXPENSE_CATEGORIES:
            issues.append(f"expenses[{i}].category must be one of {EXPENSE_CATEGORIES}")
            continue
        treatment = row.get("treatment")
        if treatment not in EXPENSE_TREATMENTS:
            issues.append(f"expenses[{i}].treatment must be one of {EXPENSE_TREATMENTS}")
            continue
        amount = _parse_amount(row.get("amount"))
        if amount is None:
            issues.append(f"expenses[{i}].amount must be a positive decimal string")
            continue
        notes = row.get("notes")
        if not _is_optional_str(notes):
            issues.append(f"expenses[{i}].notes must be a string or null")
            continue
        out.append({
            "vehicle": vehicle, "incurred_on": incurred_on, "category": category,
            "amount": amount, "treatment": treatment, "notes": notes,
        })
    return out


def _normalize_odometer_readings(raw: Any, issues: list[str], vehicle_ids: set[int]) -> list[dict]:
    if not isinstance(raw, list):
        issues.append("odometer_readings must be a list")
        return []
    out: list[dict] = []
    seen_pairs: set[tuple[int, datetime]] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"odometer_readings[{i}] must be an object")
            continue
        vehicle = row.get("vehicle")
        if not _references_known_id(vehicle, vehicle_ids):
            issues.append(f"odometer_readings[{i}].vehicle references unknown vehicle $id {vehicle}")
            continue
        recorded_at = _parse_datetime(row.get("recorded_at"))
        if recorded_at is None:
            issues.append(
                f"odometer_readings[{i}].recorded_at must be an ISO 8601 timestamp with a UTC offset"
            )
            continue
        pair = (vehicle, recorded_at)
        if pair in seen_pairs:
            issues.append(
                f"odometer_readings[{i}] duplicates an existing (vehicle, recorded_at) pair "
                f"for vehicle $id {vehicle}"
            )
            continue
        odometer_m = _parse_finite_number(row.get("odometer_m"), minimum=0)
        if odometer_m is None:
            issues.append(f"odometer_readings[{i}].odometer_m must be a non-negative number")
            continue
        note = row.get("note")
        if not _is_optional_str(note):
            issues.append(f"odometer_readings[{i}].note must be a string or null")
            continue
        seen_pairs.add(pair)
        out.append({
            "vehicle": vehicle, "recorded_at": recorded_at, "odometer_m": odometer_m,
            "note": note,
        })
    return out


def _normalize_settings(raw: Any, issues: list[str]) -> dict:
    if not isinstance(raw, dict):
        issues.append("settings must be an object")
        return {}
    value = raw.get("auto_assign_default_vehicle")
    if not isinstance(value, bool):
        issues.append("settings.auto_assign_default_vehicle must be a boolean")
        return {}
    return {"auto_assign_default_vehicle": value}


def normalize_bundle(bundle: Any) -> tuple[dict | None, list[str]]:
    """Structural and in-bundle-referential validation, with no DB access --
    every dangling `$id`, duplicate key, and out-of-range value a hand-edited
    or corrupted bundle could carry is caught here before a connection is
    even opened. Collects every issue found rather than stopping at the
    first, so a caller fixing a bundle by hand sees the whole list at once.
    Returns `(normalized_bundle, [])` on success -- with every date/datetime
    string parsed into a Python object, ready to bind straight into DB
    parameters -- or `(None, issues)` on failure.
    """
    issues: list[str] = []
    if not isinstance(bundle, dict):
        return None, ["bundle must be a JSON object"]

    if bundle.get("format") != FORMAT:
        issues.append(f"format must be {FORMAT!r}")
    if bundle.get("format_version") != FORMAT_VERSION:
        issues.append(f"format_version must be {FORMAT_VERSION}")
    schema_version = bundle.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        issues.append("schema_version must be an integer")

    vehicles, vehicle_ids = _normalize_vehicles(bundle.get("vehicles"), issues)
    places, place_ids = _normalize_places(bundle.get("places"), issues)
    tag_rules = _normalize_tag_rules(bundle.get("tag_rules"), issues, place_ids)
    mileage_rates = _normalize_mileage_rates(bundle.get("mileage_rates"), issues)
    trips = _normalize_trips(bundle.get("trips"), issues, vehicle_ids, place_ids)
    expenses = _normalize_expenses(bundle.get("expenses"), issues, vehicle_ids)
    odometer_readings = _normalize_odometer_readings(
        bundle.get("odometer_readings"), issues, vehicle_ids
    )
    settings = _normalize_settings(bundle.get("settings"), issues)

    if issues:
        return None, issues

    return {
        "schema_version": schema_version,
        "vehicles": vehicles, "places": places, "tag_rules": tag_rules,
        "mileage_rates": mileage_rates, "trips": trips, "expenses": expenses,
        "odometer_readings": odometer_readings, "settings": settings,
    }, []


# ---------------------------------------------------------------------------
# Import: DB-facing validation and mutation
# ---------------------------------------------------------------------------

def _tag_rule_sort_key(row: dict) -> tuple:
    return (row["a_place"] or 0, row["a_kind"] or "", row["b_place"] or 0, row["b_kind"] or "", row["category"])


async def _check_clean_target(conn) -> dict:
    """Empty dict means clean. Otherwise, one entry per table that isn't
    in the state a freshly migrated instance would be in -- content-compared
    for vehicles/tag_rules (not just counted), since a target could have
    zero *extra* rows but an edited seeded one. points/stays are counted
    because either can survive an operator deleting the trips they produced,
    letting the next detector pass manufacture trips from them alongside the
    imported ledger; raw_messages is excluded because it cannot itself cause
    that.
    """
    conflicts: dict[str, dict] = {}
    for table in ("trips", "expenses", "odometer_readings", "places", "points", "stays"):
        cur = await conn.execute(f"SELECT count(*) FROM {table}")
        count = (await cur.fetchone())[0]
        if count:
            conflicts[table] = {"count": count, "expected_count": 0}

    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT name, make, model, plate, is_default, active FROM vehicles")
    vehicles = await cur.fetchall()
    if vehicles != [dict(SEEDED_VEHICLE)]:
        conflicts["vehicles"] = {
            "count": len(vehicles),
            "expected": "exactly one row: the migration-seeded default vehicle",
        }

    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules"
    )
    rules = await cur.fetchall()
    expected_rules = [dict(r) for r in SEEDED_TAG_RULES]
    if sorted(rules, key=_tag_rule_sort_key) != sorted(expected_rules, key=_tag_rule_sort_key):
        conflicts["tag_rules"] = {
            "count": len(rules),
            "expected": "exactly the migration-seeded default rules",
        }

    return conflicts


async def _insert_vehicles(conn, rows: list[dict]) -> dict[int, int]:
    id_map: dict[int, int] = {}
    for row in rows:
        cur = await conn.execute(
            "INSERT INTO vehicles (name, make, model, plate, is_default, active) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (row["name"], row["make"], row["model"], row["plate"],
             row["is_default"], row["active"]),
        )
        id_map[row["$id"]] = (await cur.fetchone())[0]
    return id_map


async def _insert_places(conn, rows: list[dict]) -> dict[int, int]:
    id_map: dict[int, int] = {}
    for row in rows:
        cur = await conn.execute(
            "INSERT INTO places (name, kind, geom, radius_m) "
            "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s) RETURNING id",
            (row["name"], row["kind"], row["lon"], row["lat"], row["radius_m"]),
        )
        id_map[row["$id"]] = (await cur.fetchone())[0]
    return id_map


async def _insert_tag_rules(conn, rows: list[dict], place_id_map: dict[int, int]) -> int:
    # executemany over one round trip instead of one execute() per row: a
    # multi-year ledger's trips/expenses/odometer_readings are thousands of
    # rows each, all inside one open transaction that also holds the import
    # advisory lock, so per-row round trips directly lengthen that lock's
    # window. An empty rows list is a safe no-op here (confirmed against
    # this project's installed psycopg): executemany's per-row loop simply
    # never runs, so no query is sent.
    params = [
        (
            place_id_map[row["a_place"]] if row["a_place"] is not None else None,
            row["a_kind"],
            place_id_map[row["b_place"]] if row["b_place"] is not None else None,
            row["b_kind"],
            row["category"],
        )
        for row in rows
    ]
    cur = conn.cursor()
    await cur.executemany(
        "INSERT INTO tag_rules (a_place, a_kind, b_place, b_kind, category) "
        "VALUES (%s, %s, %s, %s, %s)",
        params,
    )
    return len(rows)


async def _upsert_mileage_rates(conn, rows: list[dict]) -> int:
    for row in rows:
        await conn.execute(
            "INSERT INTO mileage_rates (year, rate_per_mi, rate_h2_per_mi, h2_start_month) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (year) DO UPDATE SET rate_per_mi = EXCLUDED.rate_per_mi, "
            "rate_h2_per_mi = EXCLUDED.rate_h2_per_mi, h2_start_month = EXCLUDED.h2_start_month, "
            "updated_at = now()",
            (row["year"], row["rate_per_mi"], row["rate_h2_per_mi"], row["h2_start_month"]),
        )
    return len(rows)


async def _insert_trips(
    conn, rows: list[dict], vehicle_id_map: dict[int, int], place_id_map: dict[int, int]
) -> int:
    params = [
        (
            row["device"], row["source"], row["started_at"], row["ended_at"], row["distance_m"],
            row["has_gap"], row["category"], row["purpose"], row["notes"],
            vehicle_id_map[row["vehicle"]] if row["vehicle"] is not None else None,
            place_id_map[row["start_place"]] if row["start_place"] is not None else None,
            place_id_map[row["end_place"]] if row["end_place"] is not None else None,
            row["tag_source"],
        )
        for row in rows
    ]
    cur = conn.cursor()
    # imported = true unconditionally: every trip in a bundle is by
    # definition unbacked by points in this instance (see
    # migrations/019_trip_imported.sql), whether its source is
    # 'detected' or 'manual' -- the column exists to keep the detector's
    # reconcile pass from treating it as stale and deleting it.
    await cur.executemany(
        "INSERT INTO trips (device, source, started_at, ended_at, distance_m, has_gap, "
        " category, purpose, notes, vehicle_id, start_place_id, end_place_id, tag_source, "
        " imported) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
        params,
    )
    return len(rows)


async def _insert_expenses(conn, rows: list[dict], vehicle_id_map: dict[int, int]) -> int:
    params = [
        (
            vehicle_id_map[row["vehicle"]], row["incurred_on"], row["category"],
            row["amount"], row["treatment"], row["notes"],
        )
        for row in rows
    ]
    cur = conn.cursor()
    await cur.executemany(
        "INSERT INTO expenses (vehicle_id, incurred_on, category, amount, treatment, notes) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        params,
    )
    return len(rows)


async def _insert_odometer_readings(conn, rows: list[dict], vehicle_id_map: dict[int, int]) -> int:
    params = [
        (vehicle_id_map[row["vehicle"]], row["recorded_at"], row["odometer_m"], row["note"])
        for row in rows
    ]
    cur = conn.cursor()
    await cur.executemany(
        "INSERT INTO odometer_readings (vehicle_id, recorded_at, odometer_m, note) "
        "VALUES (%s, %s, %s, %s)",
        params,
    )
    return len(rows)


async def _update_settings(conn, settings: dict) -> None:
    await conn.execute(
        "UPDATE app_settings SET auto_assign_default_vehicle = %s, updated_at = now() WHERE id = 1",
        (settings["auto_assign_default_vehicle"],),
    )


async def _apply_import(conn, bundle: dict) -> dict:
    # Every other trip/place mutation path takes this lock before writing
    # (app/ui.py's trip delete and batch update, the detector itself); import
    # is no different -- without it, a scheduled detector pass can insert
    # trips after _check_clean_target's count returns 0 but before this
    # transaction commits, producing exactly the interleaved state the
    # clean-target precondition exists to prevent.
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))

    target_schema_version = await _fetch_schema_version(conn)
    if bundle["schema_version"] != target_schema_version:
        raise PortableImportError(
            "schema_version_mismatch",
            f"Bundle schema_version {bundle['schema_version']} does not match this "
            f"instance's schema_version {target_schema_version}; import across schema "
            "versions is not supported.",
        )

    conflicts = await _check_clean_target(conn)
    if conflicts:
        raise PortableImportError(
            "target_not_clean",
            "The target instance already has data beyond the freshly migrated default "
            "state; import only supports a clean target.",
            conflicts=conflicts,
        )

    try:
        # The seeded default vehicle/tag_rules are replaced, never reused --
        # import always inserts fresh rows (see the id maps below) -- so the
        # rows they'd otherwise collide with (vehicles_one_default_idx; a
        # bundle that itself carries a "home <-> work" rule) have to go
        # first. Safe only because _check_clean_target just confirmed
        # nothing else in the target references them.
        await conn.execute("DELETE FROM tag_rules")
        await conn.execute("DELETE FROM vehicles")

        vehicle_id_map = await _insert_vehicles(conn, bundle["vehicles"])
        place_id_map = await _insert_places(conn, bundle["places"])
        tag_rule_count = await _insert_tag_rules(conn, bundle["tag_rules"], place_id_map)
        mileage_rate_count = await _upsert_mileage_rates(conn, bundle["mileage_rates"])
        trip_count = await _insert_trips(conn, bundle["trips"], vehicle_id_map, place_id_map)
        expense_count = await _insert_expenses(conn, bundle["expenses"], vehicle_id_map)
        odometer_count = await _insert_odometer_readings(
            conn, bundle["odometer_readings"], vehicle_id_map
        )
        await _update_settings(conn, bundle["settings"])
    except errors.Error as exc:
        # Structural/referential validation already ran in normalize_bundle;
        # reaching here means something DB-level slipped past it (e.g. two
        # vehicles both marked is_default). Logged in full server-side, but
        # not echoed to the caller -- same caution as auth.py's OAuthError
        # handling, even though a raw psycopg error is far less likely to
        # carry a secret than that one is.
        log.exception("portable import: insert failed after validation passed")
        raise PortableImportError(
            "insert_failed",
            f"Import failed while writing data ({type(exc).__name__}); the whole "
            "import was rolled back.",
        )

    return {
        "vehicles": len(vehicle_id_map), "places": len(place_id_map),
        "tag_rules": tag_rule_count, "mileage_rates": mileage_rate_count,
        "trips": trip_count, "expenses": expense_count,
        "odometer_readings": odometer_count,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _reject_oversized_import_upload(request: Request) -> None:
    """A route dependency for POST /settings/import/data -- runs before the
    route body ever calls request.form(), which is the only way to refuse an
    oversized upload before Starlette's multipart parser spools it to disk.
    Confirmed against this project's installed Starlette (1.3.1):
    MultiPartParser.on_part_data enforces max_part_size only for a non-file
    part; a file part is appended to its SpooledTemporaryFile with no cap of
    its own. This only works because the route below takes no File()/Form()
    parameters of its own -- declaring one there would make FastAPI call
    request.form() itself while resolving the route's parameters, which (also
    confirmed empirically against this project's installed FastAPI) happens
    before any dependency, including this one, runs.

    Content-Length covers the whole multipart envelope (boundary lines and
    part headers, not just the file bytes), so this is a conservative
    approximation of the configured limit rather than an exact one -- the
    right direction to be wrong in for a guard. It's also absent entirely
    under chunked transfer-encoding; that case falls through here to let
    _read_capped_upload catch it below, on whatever Starlette already
    spooled by the time the route body runs.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared_bytes = int(content_length)
    except ValueError:
        return
    cfg = request.app.state.config
    if declared_bytes > cfg.portable_import_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Upload exceeds the {cfg.portable_import_max_bytes}-byte limit",
        )


async def _read_capped_upload(file: UploadFile, max_bytes: int) -> bytes | None:
    """Bounds the size of the body handed to json.loads below -- same intent
    as app/ingest.py's _read_capped_body, adapted for an UploadFile (already
    received by Starlette's multipart parser, which spools past a small
    threshold to disk rather than holding an arbitrarily large upload in
    memory) rather than a raw request stream. Kept as defense in depth
    alongside _reject_oversized_import_upload above, which only catches a
    declared Content-Length -- this still bounds what reaches json.loads when
    that header is missing (chunked transfer-encoding) or understates the
    true size.
    """
    data = await file.read(max_bytes + 1)
    return None if len(data) > max_bytes else data


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/settings/export/data")
    async def export_data(request: Request, user: dict = Depends(require_user)):
        pool = request.app.state.pool
        async with pool.connection() as conn:
            bundle = build_export_bundle(
                vehicles=await _fetch_export_vehicles(conn),
                places=await _fetch_export_places(conn),
                tag_rules=await _fetch_export_tag_rules(conn),
                mileage_rates=await _fetch_export_mileage_rates(conn),
                trips=await _fetch_export_trips(conn),
                expenses=await _fetch_export_expenses(conn),
                odometer_readings=await _fetch_export_odometer_readings(conn),
                settings=await _fetch_export_settings(conn),
                schema_version=await _fetch_schema_version(conn),
                exported_at=datetime.now(timezone.utc),
            )
        try:
            # allow_nan=False: json.dumps otherwise writes a bare NaN/Infinity
            # token for any non-finite value already in the ledger, which
            # Python's own json.loads accepts back but isn't valid JSON per
            # RFC 8259 -- JS JSON.parse, jq, and Go's encoding/json all
            # reject it, making the file unreadable by anything but this app.
            content = json.dumps(bundle, indent=2, allow_nan=False).encode("utf-8")
        except ValueError:
            log.exception("portable export: bundle contains a non-finite value")
            return JSONResponse(
                {
                    "ok": False, "error": "non_finite_value",
                    "detail": "Export failed because the ledger contains a non-finite "
                    "number (NaN or Infinity); the export was not produced.",
                },
                status_code=500,
            )
        filename = f"odograph-export-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        return Response(
            content=content, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post(
        "/settings/import/data",
        dependencies=[Depends(_reject_oversized_import_upload)],
    )
    async def import_data(request: Request, user: dict = Depends(require_user)):
        # file/dry_run/csrf_token are read from the parsed form by hand, not
        # declared as File()/Form() parameters on this function -- see
        # _reject_oversized_import_upload's docstring for why that's load-
        # bearing rather than a style choice.
        form = await request.form()
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=422, detail="file is required")
        raw_dry_run = form.get("dry_run", "")
        raw_csrf_token = form.get("csrf_token", "")
        dry_run = raw_dry_run if isinstance(raw_dry_run, str) else ""
        csrf_token = raw_csrf_token if isinstance(raw_csrf_token, str) else ""

        check_form_csrf(request, csrf_token)

        cfg = request.app.state.config
        raw = await _read_capped_upload(file, cfg.portable_import_max_bytes)
        if raw is None:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {cfg.portable_import_max_bytes}-byte limit",
            )

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_json", "detail": "Uploaded file is not valid JSON"},
                status_code=400,
            )

        normalized, issues = normalize_bundle(payload)
        if issues:
            return JSONResponse(
                {"ok": False, "error": "malformed_bundle", "issues": issues}, status_code=400
            )

        is_dry_run = dry_run == "1"
        pool = request.app.state.pool
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    summary = await _apply_import(conn, normalized)
                    if is_dry_run:
                        # Runs the identical validate-then-mutate path so a
                        # dry run genuinely exercises conflict detection,
                        # then discards the mutation instead of committing it.
                        raise Rollback()
        except PortableImportError as exc:
            return JSONResponse(exc.to_response(), status_code=exc.status_code)

        return JSONResponse({"ok": True, "dry_run": is_dry_run, "counts": summary})

    return router
