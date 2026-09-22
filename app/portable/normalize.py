"""Import: pure bundle validation. `normalize_bundle` and everything it
calls must stay free of DB access, since rejecting a malformed upload before
any query runs is the point; every dangling `$id`, duplicate key, and
out-of-range value a hand-edited or corrupted bundle could carry is caught
here, with every issue collected rather than raising on the first one.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.expenses import EXPENSE_CATEGORIES, EXPENSE_TREATMENTS
from app.places_desc import PLACE_KINDS
from app.portable.format import (
    FORMAT,
    TAG_RULE_CATEGORIES,
    TAG_SOURCES,
    TRIP_CATEGORIES,
    TRIP_EXCLUSIONS,
    TRIP_LABEL_MAX_LENGTH,
    TRIP_SOURCES,
)
from app.validation import parse_finite_number as _parse_finite_number

# Every format_version this importer still reads. Version 1 predates the
# optional fields described in format.py, so a bundle at that version simply
# lacks them and _normalize_trips applies their absence defaults. Keep every
# supported version literal here. Deriving this from only the oldest and
# current versions would silently drop version 2 when version 3 is introduced.
SUPPORTED_FORMAT_VERSIONS = (1, 2, 3)


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
    # Same precision rule app/ui/expenses.py's _parse_expense_input enforces
    # on a hand-typed amount: reject rather than silently round a bundle
    # value that carries more than 2 decimal places.
    quantized = raw.quantize(Decimal("0.01"))
    return quantized if quantized == raw else None


def _is_optional_str(value: Any) -> bool:
    """`None` or `str` is the contract for every optional text field below
    (make, model, plate, purpose, notes, note, start_label, end_label): each
    is passed straight through to psycopg with no other transformation, so a
    dict, number, or list here would otherwise reach the database and fail
    there, either when psycopg can't adapt the value to text or when the
    column itself rejects it -- both opaque insert_failed instead of a named
    issue here.
    """
    return value is None or isinstance(value, str)


def _plain_int(value: Any) -> bool:
    """`bool` is a subclass of `int` in Python, so `True in {1}` is True and
    `hash(True) == hash(1)` -- an `isinstance(value, int)` check alone would
    accept `true`/`false` wherever this bundle format expects a real integer
    ($id, year, h2_start_month), e.g. silently resolving a `$id`
    cross-reference to whichever row has `$id` 1/0 and attaching a trip to
    the wrong vehicle instead of being refused.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _references_known_id(value: Any, known_ids: set[int]) -> bool:
    """`value in known_ids` alone is unsafe for the same reason a plain
    `isinstance(value, int)` check is -- see `_plain_int`'s docstring.
    """
    return _plain_int(value) and value in known_ids


def _each_row(field: str, raw: Any, issues: list[str]):
    """Yields (index, row) for each object in a bundle list field, recording
    an issue for a non-list field or a non-object row instead of yielding it.
    """
    if not isinstance(raw, list):
        issues.append(f"{field} must be a list")
        return
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            issues.append(f"{field}[{i}] must be an object")
            continue
        yield i, row


def _normalize_vehicles(raw: Any, issues: list[str]) -> tuple[list[dict], set[int]]:
    if isinstance(raw, list) and not raw:
        issues.append(
            "vehicles must contain at least one vehicle; import replaces the target's "
            "vehicles table entirely, so an empty list would leave it with none"
        )
        return [], set()
    out: list[dict] = []
    seen_ids: set[int] = set()
    for i, row in _each_row("vehicles", raw, issues):
        rid = row.get("$id")
        if not _plain_int(rid):
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
    out: list[dict] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for i, row in _each_row("places", raw, issues):
        rid = row.get("$id")
        if not _plain_int(rid):
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
    out: list[dict] = []
    for i, row in _each_row("tag_rules", raw, issues):
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
    out: list[dict] = []
    seen_years: set[int] = set()
    for i, row in _each_row("mileage_rates", raw, issues):
        year = row.get("year")
        if not _plain_int(year):
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
        if h2_month is not None and not (_plain_int(h2_month) and 1 <= h2_month <= 12):
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
    out: list[dict] = []
    seen_ids: set[int] = set()
    for i, row in _each_row("trips", raw, issues):
        rid = row.get("$id")
        if not _plain_int(rid):
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
        # Absent (a version-1 bundle predates this field entirely) and
        # explicit null both mean "not excluded", same as row.get("vehicle")
        # above; only a present-and-unrecognized value is an issue.
        exclusion = row.get("exclusion")
        if exclusion is not None and exclusion not in TRIP_EXCLUSIONS:
            issues.append(f"trips[{i}].exclusion must be one of {TRIP_EXCLUSIONS} or null")
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
        # A label only exists to name an endpoint that has nothing else
        # naming it, the same rule migration 025's trips_start_label_*/
        # trips_end_label_* constraints enforce in the database. A bundle
        # carries no geometry, so the place-reference half of that rule is
        # all normalize_bundle can check here; the geometry half is
        # satisfied automatically because _insert_trips never sets
        # start_geom/end_geom for an imported row.
        start_label = row.get("start_label")
        if not _is_optional_str(start_label):
            issues.append(f"trips[{i}].start_label must be a string or null")
            continue
        # Reject rather than trim: a portable bundle is machine-produced
        # data, and silently rewriting a value on import would make the
        # round trip quietly lossy. This mirrors migration 025's
        # trips_start_label_trimmed_nonblank constraint, which likewise
        # rejects a start_label that isn't already equal to its own btrim.
        if start_label is not None and not start_label.strip():
            issues.append(f"trips[{i}].start_label must be a non-empty string")
            continue
        if start_label is not None and start_label != start_label.strip():
            issues.append(
                f"trips[{i}].start_label must not have leading or trailing whitespace"
            )
            continue
        if start_label is not None and len(start_label) > TRIP_LABEL_MAX_LENGTH:
            issues.append(
                f"trips[{i}].start_label must be {TRIP_LABEL_MAX_LENGTH} characters or fewer"
            )
            continue
        if start_label is not None and source != "manual":
            issues.append(f"trips[{i}].start_label is only allowed when source is manual")
            continue
        if start_label is not None and start_place is not None:
            issues.append(f"trips[{i}] cannot set both start_label and start_place")
            continue
        end_label = row.get("end_label")
        if not _is_optional_str(end_label):
            issues.append(f"trips[{i}].end_label must be a string or null")
            continue
        # Reject rather than trim: see the matching start_label check above.
        # This mirrors migration 025's trips_end_label_trimmed_nonblank
        # constraint, which likewise rejects an end_label that isn't already
        # equal to its own btrim.
        if end_label is not None and not end_label.strip():
            issues.append(f"trips[{i}].end_label must be a non-empty string")
            continue
        if end_label is not None and end_label != end_label.strip():
            issues.append(
                f"trips[{i}].end_label must not have leading or trailing whitespace"
            )
            continue
        if end_label is not None and len(end_label) > TRIP_LABEL_MAX_LENGTH:
            issues.append(
                f"trips[{i}].end_label must be {TRIP_LABEL_MAX_LENGTH} characters or fewer"
            )
            continue
        if end_label is not None and source != "manual":
            issues.append(f"trips[{i}].end_label is only allowed when source is manual")
            continue
        if end_label is not None and end_place is not None:
            issues.append(f"trips[{i}] cannot set both end_label and end_place")
            continue
        seen_ids.add(rid)
        out.append({
            "$id": rid, "device": device, "source": source,
            "started_at": started_at, "ended_at": ended_at, "distance_m": distance_m,
            "has_gap": has_gap, "category": category, "exclusion": exclusion,
            "purpose": purpose, "notes": notes,
            "vehicle": vehicle, "start_place": start_place, "end_place": end_place,
            "tag_source": tag_source,
            "start_label": start_label, "end_label": end_label,
        })
    return out


def _normalize_expenses(
    raw: Any, issues: list[str], vehicle_ids: set[int], trip_ids: set[int]
) -> list[dict]:
    out: list[dict] = []
    for i, row in _each_row("expenses", raw, issues):
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
        trip = row.get("trip")
        if trip is not None and not _references_known_id(trip, trip_ids):
            issues.append(f"expenses[{i}].trip references unknown trip $id {trip}")
            continue
        out.append({
            "vehicle": vehicle, "incurred_on": incurred_on, "category": category,
            "amount": amount, "treatment": treatment, "notes": notes, "trip": trip,
        })
    return out


def _normalize_odometer_readings(raw: Any, issues: list[str], vehicle_ids: set[int]) -> list[dict]:
    out: list[dict] = []
    seen_pairs: set[tuple[int, datetime]] = set()
    for i, row in _each_row("odometer_readings", raw, issues):
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


def _normalize_settings(raw: Any, issues: list[str], format_version: int) -> dict:
    if not isinstance(raw, dict):
        issues.append("settings must be an object")
        return {}
    value = raw.get("auto_assign_default_vehicle")
    if not isinstance(value, bool):
        issues.append("settings.auto_assign_default_vehicle must be a boolean")
        return {}
    display_tz = raw.get("display_tz") if format_version == 3 else None
    if format_version == 3:
        try:
            if not isinstance(display_tz, str) or len(display_tz) > 128:
                raise ValueError
            ZoneInfo(display_tz)
        except (ValueError, ZoneInfoNotFoundError):
            issues.append("settings.display_tz must be an IANA timezone")
    return {"auto_assign_default_vehicle": value, "display_tz": display_tz}


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
    format_version = bundle.get("format_version")
    if (
        not isinstance(format_version, int)
        or isinstance(format_version, bool)
        or format_version not in SUPPORTED_FORMAT_VERSIONS
    ):
        issues.append(f"format_version must be one of {SUPPORTED_FORMAT_VERSIONS}")
    schema_version = bundle.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        issues.append("schema_version must be an integer")

    vehicles, vehicle_ids = _normalize_vehicles(bundle.get("vehicles"), issues)
    places, place_ids = _normalize_places(bundle.get("places"), issues)
    tag_rules = _normalize_tag_rules(bundle.get("tag_rules"), issues, place_ids)
    mileage_rates = _normalize_mileage_rates(bundle.get("mileage_rates"), issues)
    trips = _normalize_trips(bundle.get("trips"), issues, vehicle_ids, place_ids)
    trip_ids = {trip["$id"] for trip in trips}
    expenses = _normalize_expenses(bundle.get("expenses"), issues, vehicle_ids, trip_ids)
    odometer_readings = _normalize_odometer_readings(
        bundle.get("odometer_readings"), issues, vehicle_ids
    )
    settings = _normalize_settings(bundle.get("settings"), issues, format_version)

    if issues:
        return None, issues

    return {
        "format_version": format_version, "schema_version": schema_version,
        "vehicles": vehicles, "places": places, "tag_rules": tag_rules,
        "mileage_rates": mileage_rates, "trips": trips, "expenses": expenses,
        "odometer_readings": odometer_readings, "settings": settings,
    }, []
