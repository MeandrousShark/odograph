"""Trip writes shared by the web pages and the API: partial updates, the
human-tag primitive, manual creates (with an optional idempotency claim) and
source-aware deletion.

Every function takes an account connection and leaves the commit to the
caller. Validation failures raise `ManualTripValidationError` with
per-field messages; a trip that is missing from this account (another
account's id, or one the detector replaced) is a 404.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from fastapi import HTTPException
from psycopg import errors
from psycopg.rows import dict_row

from app.account_context import account_id
from app.detector.lock import lock_detector
from app.trip_queries import CATEGORIES, EXCLUSIONS

class ManualTripValidationError(ValueError):
    """Per-field validation messages for a trip write. The web pages show
    them beside their inputs; the API returns them as `validation_failed`."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(next(iter(errors.values())))
        self.errors = errors


class IdempotencyKeyReused(Exception):
    """The key already created a record from a different request."""


# Matches the database's own char_length cap on trips.start_label/end_label
# (migrations/025_manual_trip_labels.sql) so a value this function accepts
# can never be rejected by the constraint, and vice versa.
TRIP_LABEL_MAX_LENGTH = 100

FIELD_REQUIRED = "This field is required."
CATEGORY_INVALID = "Choose a valid category."
EXCLUSION_INVALID = "Choose a valid exclusion."
VEHICLE_MISSING = "Choose a vehicle that still exists."
LABEL_HAS_ROUTE = "Location names are only available for a trip with no route."
MANUAL_ONLY = "This can only be changed on a manual trip."
DISTANCE_INVALID = "Distance must be a positive finite number."
END_BEFORE_START = "The end must be after the start."
TIME_INVALID = "Enter a time with its offset."

TEXT_FIELDS = ("purpose", "notes")
LABEL_FIELDS = ("start_label", "end_label")
MANUAL_FIELDS = ("started_at", "ended_at", "distance_m", *LABEL_FIELDS)
UPDATE_FIELDS = ("category", "exclusion", "purpose", "notes", "vehicle_id", *MANUAL_FIELDS)
REQUIRED_FIELDS = ("category", "started_at", "ended_at", "distance_m")

IN_PROGRESS_DETAIL = "This trip is still in progress. Try again once it ends."


def normalize_trip_label(value: str, field: str) -> str | None:
    """Shared trim/blank/length handling for a manual trip's optional start
    or end display label, used identically by manual create and trip edit so
    a direct request to either sees the same behavior. Length is counted in
    characters, not bytes, so a name written in a multi-byte script isn't
    penalized for its encoded size -- the same reasoning the database
    constraint's `char_length` (rather than `octet_length`) follows.
    """
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > TRIP_LABEL_MAX_LENGTH:
        raise ManualTripValidationError(
            {field: f"Keep it to {TRIP_LABEL_MAX_LENGTH} characters or fewer."}
        )
    return trimmed


def _text(value: str | None) -> str | None:
    return None if value is None else (value.strip() or None)


async def vehicle_exists(conn, vehicle_id: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM vehicles WHERE id = %s AND account_id = %s", (vehicle_id, account_id(conn))
    )
    return await cur.fetchone() is not None


def _instant_problem(value) -> str | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return TIME_INVALID
    return None


def _distance_problem(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DISTANCE_INVALID
    if not math.isfinite(value) or value <= 0:
        return DISTANCE_INVALID
    return None


# ---------------------------------------------------------------------------
# Human tags


async def apply_human_tag(
    conn, trip_id: int, category: str | None, purpose: str | None = None,
    *, update_purpose: bool = False,
) -> None:
    """Set tag_source='human' unconditionally, even on a clear back to
    'unclassified'. This is the load-bearing line for human-tag supremacy: the
    auto-tagger (app.autotag) only touches rows with tag_source NULL or
    'rule', so a deliberate human choice can never be overwritten by a rule.
    Shared by the list-view tag and purpose buttons and /review's undo so the
    guarantee lives in one place; category validation stays with each caller
    (the list view allows clearing to 'unclassified', review does not). A
    `category` of None leaves the category alone and sets only the purpose.

    Checks rowcount and 404s if the row is gone: the detector's reconcile
    deletes and reinserts trips it replaces, and without this guard a human
    tag racing that delete would silently match zero rows and report success.
    """
    if category is None:
        cur = await conn.execute(
            "UPDATE trips SET purpose = %s, tag_source = 'human', updated_at = now() "
            "WHERE id = %s AND account_id = %s",
            ((purpose or "").strip() or None, trip_id, account_id(conn)),
        )
    elif update_purpose:
        cur = await conn.execute(
            "UPDATE trips SET category = %s, purpose = %s, tag_source = 'human', "
            "updated_at = now() WHERE id = %s AND account_id = %s",
            (category, (purpose or "").strip() or None, trip_id, account_id(conn)),
        )
    else:
        cur = await conn.execute(
            "UPDATE trips SET category = %s, tag_source = 'human', updated_at = now() "
            "WHERE id = %s AND account_id = %s",
            (category, trip_id, account_id(conn)),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such trip")


# ---------------------------------------------------------------------------
# Partial updates

_STORED_COLUMNS = (
    "source::text AS source, category::text AS category, exclusion::text AS exclusion, "
    "purpose, notes, vehicle_id, started_at, ended_at, distance_m, start_label, end_label, "
    "tag_source::text AS tag_source, start_place_id, end_place_id, "
    "start_geom IS NOT NULL AS has_start_geom, end_geom IS NOT NULL AS has_end_geom"
)


async def lock_trip(conn, trip_id: int) -> dict:
    """The stored trip, locked for this transaction's update, or a 404."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {_STORED_COLUMNS} FROM trips WHERE id = %s AND account_id = %s FOR UPDATE",
        (trip_id, account_id(conn)),
    )
    stored = await cur.fetchone()
    if stored is None:
        raise HTTPException(status_code=404, detail="No such trip")
    return stored


def label_eligible(stored: dict, field: str) -> bool:
    """A label only names an endpoint that has nothing else naming it (see
    migrations/025_manual_trip_labels.sql): a detected trip or a routed
    manual trip's endpoint already has a saved place or geometry describing
    it. Checked per endpoint, matching the database constraint's own
    per-column shape."""
    side = field.split("_", 1)[0]
    return (stored["source"] == "manual" and stored[f"{side}_place_id"] is None
            and not stored[f"has_{side}_geom"])


async def _normalize_changes(
    conn, stored: dict, changes: Mapping[str, object], problems: dict[str, str],
) -> dict[str, object]:
    manual = stored["source"] == "manual"
    normalized: dict[str, object] = {}
    for name, value in changes.items():
        if name not in UPDATE_FIELDS:
            raise ValueError(f"unknown trip field {name!r}")
        if value is None and name in REQUIRED_FIELDS:
            problems[name] = FIELD_REQUIRED
            continue
        if name == "category":
            if value not in CATEGORIES:
                problems[name] = CATEGORY_INVALID
                continue
        elif name == "exclusion":
            value = value or None
            if value is not None and value not in EXCLUSIONS:
                problems[name] = EXCLUSION_INVALID
                continue
        elif name in TEXT_FIELDS:
            value = _text(value)
        elif name == "vehicle_id":
            if value is not None and not await vehicle_exists(conn, value):
                problems[name] = VEHICLE_MISSING
                continue
        elif name in LABEL_FIELDS:
            if value is not None:
                try:
                    value = normalize_trip_label(value, name)
                except ManualTripValidationError as exc:
                    problems.update(exc.errors)
                    continue
            if value is not None and not label_eligible(stored, name):
                problems[name] = LABEL_HAS_ROUTE
                continue
            if not manual:
                problems[name] = MANUAL_ONLY
                continue
        elif name in ("started_at", "ended_at"):
            if not manual:
                problems[name] = MANUAL_ONLY
                continue
            problem = _instant_problem(value)
            if problem:
                problems[name] = problem
                continue
        elif name == "distance_m":
            if not manual:
                problems[name] = MANUAL_ONLY
                continue
            problem = _distance_problem(value)
            if problem:
                problems[name] = problem
                continue
            value = float(value)
        normalized[name] = value
    if ("started_at" in normalized or "ended_at" in normalized) and not (
            {"started_at", "ended_at"} & problems.keys()):
        started_at = normalized.get("started_at", stored["started_at"])
        ended_at = normalized.get("ended_at", stored["ended_at"])
        if ended_at <= started_at:
            problems["ended_at"] = END_BEFORE_START
    return normalized


async def update_locked_trip(
    conn, trip_id: int, stored: dict, changes: Mapping[str, object], *,
    write_unchanged: bool = False, force_human: bool = False,
    prior_errors: dict[str, str] | None = None,
) -> bool:
    """Apply the present fields of `changes` to a trip `lock_trip` returned.

    Omitted fields are left alone; `None` clears a nullable field and is
    refused for a required one. Text is trimmed, and empty means `None`.
    Times, distance and labels belong to manual trips only, and a label only
    to an endpoint with no place or route. Validation runs on the stored
    values overlaid with the changes. `prior_errors` carries the caller's own
    field problems, so one response can name every invalid field; nothing is
    written while any remain.

    `tag_source` becomes 'human' when the normalized category or purpose
    differs from the stored value, or always with `force_human`. Without
    `write_unchanged`, a request that changes nothing issues no UPDATE, so
    `updated_at` and `tag_source` stay as they are. The web edit form passes
    `write_unchanged` because every save there refreshes `updated_at`, which
    orders recent purposes and is the snap worker's concurrency token.
    Returns whether a row was written.
    """
    problems = dict(prior_errors or {})
    normalized = await _normalize_changes(conn, stored, changes, problems)
    if problems:
        raise ManualTripValidationError(problems)
    changed = {name: value for name, value in normalized.items() if value != stored[name]}
    human = force_human or "category" in changed or "purpose" in changed
    written = normalized if write_unchanged else changed
    if not written and not human and not write_unchanged:
        return False
    clauses = [f"{name} = %s" for name in written]
    params = list(written.values())
    if human:
        clauses.append("tag_source = 'human'")
    clauses.append("updated_at = now()")
    try:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE trips SET {', '.join(clauses)} WHERE id = %s AND account_id = %s",
                (*params, trip_id, account_id(conn)),
            )
    except errors.ForeignKeyViolation:
        raise ManualTripValidationError({"vehicle_id": VEHICLE_MISSING}) from None
    return True


async def update_trip(conn, trip_id: int, changes: Mapping[str, object], **options) -> bool:
    """Lock the trip and apply `changes` (see `update_locked_trip`)."""
    stored = await lock_trip(conn, trip_id)
    return await update_locked_trip(conn, trip_id, stored, changes, **options)


# ---------------------------------------------------------------------------
# Manual creates


@dataclass(frozen=True)
class ManualTrip:
    """A validated, normalized manual trip. The route fields are set only
    when the web form routed the trip; the API creates plain trips."""

    started_at: datetime
    ended_at: datetime
    distance_m: float
    category: str = "unclassified"
    exclusion: str | None = None
    purpose: str | None = None
    notes: str | None = None
    vehicle_id: int | None = None
    start_label: str | None = None
    end_label: str | None = None
    path_geojson: dict | None = field(default=None, compare=False)
    start_point: tuple[float, float] | None = None
    end_point: tuple[float, float] | None = None
    start_place_id: int | None = None
    end_place_id: int | None = None


async def validate_manual_trip(conn, values: Mapping[str, object]) -> ManualTrip:
    """Normalize a plain manual trip from client fields, or raise with every
    invalid field. Omitted optional fields take their defaults."""
    problems: dict[str, str] = {}
    for name in ("started_at", "ended_at"):
        if values.get(name) is None:
            problems[name] = FIELD_REQUIRED
        elif _instant_problem(values[name]):
            problems[name] = TIME_INVALID
    if values.get("distance_m") is None:
        problems["distance_m"] = FIELD_REQUIRED
    elif _distance_problem(values["distance_m"]):
        problems["distance_m"] = DISTANCE_INVALID
    if not problems.keys() & {"started_at", "ended_at"} and values["ended_at"] <= values["started_at"]:
        problems["ended_at"] = END_BEFORE_START
    category = values.get("category")
    if category is None:
        category = "unclassified"
    elif category not in CATEGORIES:
        problems["category"] = CATEGORY_INVALID
    exclusion = values.get("exclusion") or None
    if exclusion is not None and exclusion not in EXCLUSIONS:
        problems["exclusion"] = EXCLUSION_INVALID
    labels: dict[str, str | None] = {}
    for name in LABEL_FIELDS:
        raw = values.get(name)
        try:
            labels[name] = None if raw is None else normalize_trip_label(raw, name)
        except ManualTripValidationError as exc:
            problems.update(exc.errors)
    vehicle_id = values.get("vehicle_id")
    if vehicle_id is not None and not await vehicle_exists(conn, vehicle_id):
        problems["vehicle_id"] = VEHICLE_MISSING
    if problems:
        raise ManualTripValidationError(problems)
    return ManualTrip(
        started_at=values["started_at"], ended_at=values["ended_at"],
        distance_m=float(values["distance_m"]), category=category, exclusion=exclusion,
        purpose=_text(values.get("purpose")), notes=_text(values.get("notes")),
        vehicle_id=vehicle_id, start_label=labels["start_label"], end_label=labels["end_label"],
    )


def _instant_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def manual_trip_digest(trip: ManualTrip) -> bytes:
    """SHA-256 of the canonical validated request: sorted field names and
    normalized values, so requests that mean the same trip match."""
    if trip.path_geojson is not None:
        raise ValueError("routed trips are not created with an idempotency key")
    canonical = {
        "category": trip.category, "distance_m": repr(trip.distance_m),
        "end_label": trip.end_label, "ended_at": _instant_text(trip.ended_at),
        "exclusion": trip.exclusion, "notes": trip.notes, "purpose": trip.purpose,
        "start_label": trip.start_label, "started_at": _instant_text(trip.started_at),
        "vehicle_id": trip.vehicle_id,
    }
    text = json.dumps({"resource": "trips", "request": canonical}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).digest()


def _insert_statement(trip: ManualTrip, keyed: bool) -> tuple[str, list]:
    columns = ["account_id", "device", "source", "started_at", "ended_at", "distance_m",
               "category", "exclusion", "purpose", "notes", "vehicle_id"]
    values = ["%s", "'manual'", "'manual'", "%s", "%s", "%s", "%s", "%s", "%s", "%s", "%s"]
    params: list = [trip.started_at, trip.ended_at, trip.distance_m, trip.category, trip.exclusion,
                    trip.purpose, trip.notes, trip.vehicle_id]
    if trip.path_geojson is not None:
        columns += ["path", "start_geom", "end_geom", "start_place_id", "end_place_id", "snap_status"]
        values += ["ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)",
                   "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography",
                   "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography", "%s", "%s", "NULL"]
        params += [json.dumps(trip.path_geojson), *trip.start_point, *trip.end_point,
                   trip.start_place_id, trip.end_place_id]
    columns += ["start_label", "end_label"]
    values += ["%s", "%s"]
    params += [trip.start_label, trip.end_label]
    if keyed:
        columns += ["idempotency_key", "idempotency_digest"]
        values += ["%s", "%s"]
    return (f"INSERT INTO trips ({', '.join(columns)}) VALUES ({', '.join(values)})", params)


async def insert_manual_trip(conn, trip: ManualTrip) -> int:
    """Insert a manual trip without an idempotency key, as the web form does.
    A vanished vehicle raises the database's ForeignKeyViolation."""
    statement, params = _insert_statement(trip, keyed=False)
    cur = await conn.execute(statement + " RETURNING id", (account_id(conn), *params))
    return (await cur.fetchone())[0]


async def create_manual_trip(
    conn, trip: ManualTrip, key: uuid.UUID, digest: bytes,
) -> tuple[int, bool]:
    """Create a manual trip once per idempotency key.

    Returns `(trip_id, created)`. A key this account already used for the
    same request returns that trip with `created` False and writes nothing;
    one used for a different request raises `IdempotencyKeyReused`. A
    concurrent create with the same key waits on the unique index until the
    first transaction ends, then replays its trip, or inserts if it rolled
    back. Deleting the trip frees its key.
    """
    statement, params = _insert_statement(trip, keyed=True)
    statement += (" ON CONFLICT (account_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
                  " DO NOTHING RETURNING id")
    for _ in range(3):
        try:
            async with conn.transaction():
                cur = await conn.execute(statement, (account_id(conn), *params, key, digest))
                row = await cur.fetchone()
        except errors.ForeignKeyViolation:
            raise ManualTripValidationError({"vehicle_id": VEHICLE_MISSING}) from None
        if row is not None:
            return row[0], True
        cur = await conn.execute(
            "SELECT id, idempotency_digest FROM trips WHERE account_id = %s AND idempotency_key = %s",
            (account_id(conn), key),
        )
        existing = await cur.fetchone()
        if existing is None:
            # Deleted after the conflict was seen; the key is free again.
            continue
        if bytes(existing[1]) != digest:
            raise IdempotencyKeyReused()
        return existing[0], False
    raise RuntimeError("idempotent create did not settle")


# ---------------------------------------------------------------------------
# Deletion


async def refuse_in_progress_trips(conn, trip_ids: list[int]) -> None:
    """Refuse an edit to a trip whose end the detector has not settled.

    A trip ending at an arrival the detector has only provisionally accepted,
    or kept while the drive goes on, can still grow: a discard override over
    it could later hide the longer real trip, and a merge or split of it has
    no settled trip to produce. Such a trip is its device's latest detected
    trip and has no persisted stay at its end. A device that has sent nothing
    for a day is not waiting on a next fix, so its trip is editable again.
    Callers hold the detector lock.
    """
    cur = await conn.execute(
        "SELECT 1 FROM trips t WHERE t.account_id = %s AND t.id = ANY(%s) "
        "AND t.source = 'detected' AND NOT t.imported "
        "AND NOT EXISTS (SELECT 1 FROM stays s WHERE s.account_id = t.account_id "
        " AND s.tracking_device_id = t.tracking_device_id AND s.started_at = t.ended_at) "
        "AND EXISTS (SELECT 1 FROM points p WHERE p.account_id = t.account_id "
        " AND p.tracking_device_id = t.tracking_device_id "
        " AND p.received_at > now() - interval '24 hours') "
        "AND NOT EXISTS (SELECT 1 FROM trips l WHERE l.account_id = t.account_id "
        " AND l.tracking_device_id = t.tracking_device_id AND l.source = 'detected' "
        " AND NOT l.imported AND l.started_at > t.started_at) "
        "LIMIT 1",
        (account_id(conn), trip_ids),
    )
    if await cur.fetchone():
        raise HTTPException(status_code=409, detail=IN_PROGRESS_DETAIL)


async def delete_trip(conn, trip_id: int) -> tuple[datetime, int]:
    """Apply source-appropriate deletion and return the trip's old cursor.

    Every delete entry point shares this helper so review, list/detail and
    the API cannot drift. The detector advisory lock is intentionally
    acquired before reading the boundary timestamps: otherwise a background
    pass could rewrite them between capture and the discard insert. For
    detected trips, the durable discard and targeted row delete share the
    caller's transaction; later detector passes honor the override without
    rewriting unrelated trips. Manual and imported trips remain a direct row
    DELETE.
    """
    await lock_detector(conn)
    cur = await conn.execute(
        "SELECT device, tracking_device_id, source::text, imported, started_at, ended_at "
        "FROM trips WHERE id = %s AND account_id = %s FOR UPDATE",
        (trip_id, account_id(conn)),
    )
    trip = await cur.fetchone()
    if not trip:
        raise HTTPException(status_code=404, detail="No such trip")
    device, tracking_device_id, source, imported, started_at, ended_at = trip
    if source == "manual" or imported:
        await conn.execute("DELETE FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn)))
    else:
        await refuse_in_progress_trips(conn, [trip_id])
        await conn.execute(
            "INSERT INTO trip_boundary_overrides "
            "(account_id, tracking_device_id, device, kind, range_start, range_end) "
            "VALUES (%s, %s, %s, 'discard', %s, %s) ON CONFLICT DO NOTHING",
            (account_id(conn), tracking_device_id, device, started_at, ended_at),
        )
        await conn.execute("DELETE FROM trips WHERE id = %s AND account_id = %s", (trip_id, account_id(conn)))
    return started_at, trip_id
