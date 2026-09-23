"""Import: DB-facing validation and mutation. `PortableImportError` carries
a structured, JSON-able report back to the route; everything else here needs
an open connection, either to check the clean-target precondition or to
insert a bundle that `normalize_bundle` has already validated.
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from app.account_context import account_id

from app.db import DETECTOR_ADVISORY_LOCK_KEY, _fetch_schema_version
from app.portable.format import SEEDED_TAG_RULES, SEEDED_VEHICLE

log = logging.getLogger(__name__)

# Schemas 22 and 23 only add nullable fields. A format-1 schema-21 bundle has
# no value for either field; format-2 schema-22 predates expense trip links,
# while schema-23 carries them. Schema 24 adds avatar_bytes/avatar_mime/
# avatar_updated_at to accounts, a table the portable bundle format never
# carries at all, so each listed transition is lossless. Schema 25 adds
# nullable trips.start_label and trips.end_label; a bundle from any older
# schema simply has no value for either, so both default to null on import.
# Keep the tuples explicit so a future migration never becomes cross-schema
# compatible merely because its version is adjacent.
_COMPATIBLE_SCHEMA_TRANSITIONS = {
    (1, 21, 27), (2, 22, 27), (2, 23, 27), (2, 24, 27), (2, 25, 27), (3, 26, 27),
    (3, 27, 27),
}


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
# Import: DB-facing validation and mutation
# ---------------------------------------------------------------------------

def _tag_rule_sort_key(row: dict) -> tuple:
    return (row["a_place"] or 0, row["a_kind"] or "", row["b_place"] or 0, row["b_kind"] or "", row["category"])


async def _check_clean_target(conn) -> dict:
    """Empty dict means clean. Otherwise, one entry per table that isn't
    in the state a freshly migrated instance would be in -- content-compared
    for vehicles/tag_rules/mileage_rates (not just counted), since a target
    could have zero *extra* rows but an edited seeded one.

    Rates are the one *one-directional* comparison: `_apply_import` deletes
    them before reinserting the bundle's, so what must not be destroyed is a
    row the account holds that the canonical set does not -- an edited year,
    or one created by the one-time `MILEAGE_RATE_<YEAR>` upgrade import.
    Canonical rows the account happens to be *missing* are not a conflict:
    the delete destroys nothing, and requiring exact equality would refuse an
    import on any account provisioned outside `bootstrap_first_account`, or
    after a future reference year existing accounts have not been given.
    points/stays are counted
    because either can survive an operator deleting the trips they produced,
    letting the next detector pass manufacture trips from them alongside the
    imported ledger; raw_messages is excluded because it cannot itself cause
    that.
    """
    conflicts: dict[str, dict] = {}
    for table in ("trips", "expenses", "odometer_readings", "places", "points", "stays", "trip_boundary_overrides"):
        cur = await conn.execute(f"SELECT count(*) FROM {table} WHERE account_id = %s", (account_id(conn),))
        count = (await cur.fetchone())[0]
        if count:
            conflicts[table] = {"count": count, "expected_count": 0}

    cur = await conn.execute(
        "SELECT count(*) FROM detector_state WHERE account_id = %s "
        "AND (last_run_at IS NOT NULL OR detector_version <> 0)", (account_id(conn),),
    )
    progressed = (await cur.fetchone())[0]
    if progressed:
        conflicts["detector_state"] = {"count": progressed, "expected_count": 0}

    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT name, make, model, plate, is_default, active FROM vehicles WHERE account_id = %s", (account_id(conn),))
    vehicles = await cur.fetchall()
    if vehicles != [dict(SEEDED_VEHICLE)]:
        conflicts["vehicles"] = {
            "count": len(vehicles),
            "expected": "exactly one row: the migration-seeded default vehicle",
        }

    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules WHERE account_id = %s", (account_id(conn),)
    )
    rules = await cur.fetchall()
    expected_rules = [dict(r) for r in SEEDED_TAG_RULES]
    if sorted(rules, key=_tag_rule_sort_key) != sorted(expected_rules, key=_tag_rule_sort_key):
        conflicts["tag_rules"] = {
            "count": len(rules),
            "expected": "exactly the migration-seeded default rules",
        }

    # EXCEPT, so the comparison uses PostgreSQL numeric equality: a
    # hand-entered 0.70 and a seeded 0.7000 are the same rate.
    rate_columns = "year, rate_per_mi, rate_h2_per_mi, h2_start_month"
    cur = await conn.execute(
        "SELECT (SELECT count(*) FROM mileage_rates WHERE account_id = %s), "
        f"(SELECT count(*) FROM (SELECT {rate_columns} FROM mileage_rates "
        f"WHERE account_id = %s EXCEPT SELECT {rate_columns} "
        "FROM reference_mileage_rates) beyond_canonical)",
        (account_id(conn), account_id(conn)),
    )
    total, beyond_canonical = await cur.fetchone()
    if beyond_canonical:
        conflicts["mileage_rates"] = {
            "count": total,
            "expected": "no rate beyond this instance's canonical reference rates",
        }

    return conflicts


async def _insert_vehicles(conn, rows: list[dict]) -> dict[int, int]:
    id_map: dict[int, int] = {}
    for row in rows:
        cur = await conn.execute(
            "INSERT INTO vehicles (account_id, name, make, model, plate, is_default, active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (account_id(conn), row["name"], row["make"], row["model"], row["plate"],
             row["is_default"], row["active"]),
        )
        id_map[row["$id"]] = (await cur.fetchone())[0]
    return id_map


async def _insert_places(conn, rows: list[dict]) -> dict[int, int]:
    id_map: dict[int, int] = {}
    for row in rows:
        cur = await conn.execute(
            "INSERT INTO places (account_id, name, kind, geom, radius_m) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s) RETURNING id",
            (account_id(conn), row["name"], row["kind"], row["lon"], row["lat"], row["radius_m"]),
        )
        id_map[row["$id"]] = (await cur.fetchone())[0]
    return id_map


async def _insert_many(conn, sql: str, params: list[tuple]) -> int:
    # executemany over one round trip instead of one execute() per row: a
    # multi-year ledger's trips/expenses/odometer_readings are thousands of
    # rows each, all inside one open transaction that also holds the import
    # advisory lock, so per-row round trips directly lengthen that lock's
    # window. An empty rows list is a safe no-op here (confirmed against
    # this project's installed psycopg): executemany's per-row loop simply
    # never runs, so no query is sent.
    cur = conn.cursor()
    await cur.executemany(sql, params)
    return len(params)


async def _insert_tag_rules(conn, rows: list[dict], place_id_map: dict[int, int]) -> int:
    params = [
        (
            account_id(conn),
            place_id_map[row["a_place"]] if row["a_place"] is not None else None,
            row["a_kind"],
            place_id_map[row["b_place"]] if row["b_place"] is not None else None,
            row["b_kind"],
            row["category"],
        )
        for row in rows
    ]
    return await _insert_many(
        conn,
        "INSERT INTO tag_rules (account_id, a_place, a_kind, b_place, b_kind, category) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        params,
    )


async def _upsert_mileage_rates(conn, rows: list[dict]) -> int:
    for row in rows:
        await conn.execute(
            "INSERT INTO mileage_rates (account_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (account_id, year) DO UPDATE SET rate_per_mi = EXCLUDED.rate_per_mi, "
            "rate_h2_per_mi = EXCLUDED.rate_h2_per_mi, h2_start_month = EXCLUDED.h2_start_month, "
            "updated_at = now()",
            (account_id(conn), row["year"], row["rate_per_mi"], row["rate_h2_per_mi"], row["h2_start_month"]),
        )
    return len(rows)


async def _insert_trips(
    conn, rows: list[dict], vehicle_id_map: dict[int, int], place_id_map: dict[int, int]
) -> tuple[int, dict[int, int]]:
    # imported = true unconditionally: every trip in a bundle is by
    # definition unbacked by points in this instance (see
    # migrations/019_trip_imported.sql), whether its source is
    # 'detected' or 'manual' -- the column exists to keep the detector's
    # reconcile pass from treating it as stale and deleting it.
    params = [
        (
            account_id(conn), row["device"], row["source"], row["started_at"], row["ended_at"],
            row["distance_m"], row["has_gap"], row["category"], row["exclusion"],
            row["purpose"], row["notes"],
            vehicle_id_map[row["vehicle"]] if row["vehicle"] is not None else None,
            place_id_map[row["start_place"]] if row["start_place"] is not None else None,
            place_id_map[row["end_place"]] if row["end_place"] is not None else None,
            row["tag_source"], row["start_label"], row["end_label"],
        )
        for row in rows
    ]
    cur = conn.cursor()
    await cur.executemany(
        "INSERT INTO trips (account_id, device, source, started_at, ended_at, distance_m, has_gap, "
        " category, exclusion, purpose, notes, vehicle_id, start_place_id, end_place_id, "
        " tag_source, start_label, end_label, imported) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true) "
        "RETURNING id",
        params,
        returning=True,
    )
    id_map: dict[int, int] = {}
    row_index = 0
    async for result in cur.results():
        id_map[rows[row_index]["$id"]] = (await result.fetchone())[0]
        row_index += 1
    return len(rows), id_map


async def _insert_expenses(
    conn, rows: list[dict], vehicle_id_map: dict[int, int], trip_id_map: dict[int, int]
) -> int:
    params = [
        (
            account_id(conn), vehicle_id_map[row["vehicle"]], row["incurred_on"], row["category"],
            row["amount"], row["treatment"], row["notes"],
            trip_id_map[row["trip"]] if row["trip"] is not None else None,
        )
        for row in rows
    ]
    return await _insert_many(
        conn,
        "INSERT INTO expenses (account_id, vehicle_id, incurred_on, category, amount, treatment, notes, trip_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        params,
    )


async def _insert_odometer_readings(conn, rows: list[dict], vehicle_id_map: dict[int, int]) -> int:
    params = [
        (account_id(conn), vehicle_id_map[row["vehicle"]], row["recorded_at"], row["odometer_m"], row["note"])
        for row in rows
    ]
    return await _insert_many(
        conn,
        "INSERT INTO odometer_readings (account_id, vehicle_id, recorded_at, odometer_m, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        params,
    )


async def _update_settings(conn, settings: dict) -> None:
    await conn.execute(
        "UPDATE account_settings SET auto_assign_default_vehicle = %s, "
        "display_tz = COALESCE(%s, display_tz), updated_at = now() WHERE account_id = %s",
        (settings["auto_assign_default_vehicle"], settings.get("display_tz"), account_id(conn)),
    )


async def _apply_import(conn, bundle: dict) -> dict:
    # Preserve the shared detector/mutation exclusion through the whole import.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(%s)", (DETECTOR_ADVISORY_LOCK_KEY,)
    )

    target_schema_version = await _fetch_schema_version(conn)
    source_schema_version = bundle["schema_version"]
    transition = (
        bundle["format_version"], source_schema_version, target_schema_version
    )
    if transition not in _COMPATIBLE_SCHEMA_TRANSITIONS:
        raise PortableImportError(
            "schema_version_mismatch",
            f"Bundle schema_version {source_schema_version} is not compatible with this "
            f"instance's schema_version {target_schema_version}.",
        )

    # Ordinary personal creates do not take the detector lock. Freeze tables
    # used by the clean-target check so their writes cannot cross its window.
    # Reads remain available; these brief instance-wide write locks are kept
    # within the existing single-account import transaction.
    await conn.execute(
        "LOCK TABLE detector_state, expenses, mileage_rates, odometer_readings, "
        "places, points, stays, tag_rules, trip_boundary_overrides, trips, vehicles "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    conflicts = await _check_clean_target(conn)
    if conflicts:
        raise PortableImportError(
            "target_not_clean",
            "The target account already has data beyond the initial default "
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
        await conn.execute("DELETE FROM tag_rules WHERE account_id = %s", (account_id(conn),))
        await conn.execute("DELETE FROM vehicles WHERE account_id = %s", (account_id(conn),))
        await conn.execute("DELETE FROM mileage_rates WHERE account_id = %s", (account_id(conn),))

        vehicle_id_map = await _insert_vehicles(conn, bundle["vehicles"])
        place_id_map = await _insert_places(conn, bundle["places"])
        tag_rule_count = await _insert_tag_rules(conn, bundle["tag_rules"], place_id_map)
        mileage_rate_count = await _upsert_mileage_rates(conn, bundle["mileage_rates"])
        trip_count, trip_id_map = await _insert_trips(
            conn, bundle["trips"], vehicle_id_map, place_id_map
        )
        expense_count = await _insert_expenses(
            conn, bundle["expenses"], vehicle_id_map, trip_id_map
        )
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
