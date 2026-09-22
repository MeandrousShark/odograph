"""Export: pure bundle shaping (`build_export_bundle` and its per-table
`_export_*` helpers) plus the DB fetch helpers that feed them. The shaping
half touches no DB/IO of its own, only already-fetched rows in and a
JSON-shaped dict out, so it stays unit-testable the same way
`build_export_rows` is in `app/export.py`.
"""
from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row

from app.portable.format import FORMAT, FORMAT_VERSION
from app.account_context import account_id
from app.trip_queries import DISPLAY_DISTANCE_SQL


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
            "display_tz": settings["display_tz"],
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
        "category": row["category"], "exclusion": row["exclusion"],
        "purpose": row["purpose"], "notes": row["notes"],
        "vehicle": row["vehicle_id"], "start_place": row["start_place_id"],
        "end_place": row["end_place_id"], "tag_source": row["tag_source"],
        "start_label": row["start_label"], "end_label": row["end_label"],
    }


def _export_expense(row: dict) -> dict:
    # Money round-trips as a string, not a float: numeric(12,2) is exact and
    # a JSON float would risk a cent-level drift on the way back in (see
    # app/export.py's module docstring for the same reasoning).
    return {
        "vehicle": row["vehicle_id"], "incurred_on": row["incurred_on"].isoformat(),
        "category": row["category"], "amount": str(row["amount"]),
        "treatment": row["treatment"], "notes": row["notes"],
        "trip": row.get("trip_id"),
    }


def _export_odometer_reading(row: dict) -> dict:
    return {
        "vehicle": row["vehicle_id"], "recorded_at": row["recorded_at"].isoformat(),
        "odometer_m": row["odometer_m"], "note": row["note"],
    }


# ---------------------------------------------------------------------------
# Export: DB fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_all(conn, sql: str) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(sql, (account_id(conn),))
    return await cur.fetchall()


async def _fetch_export_vehicles(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT id, name, make, model, plate, is_default, active FROM vehicles WHERE account_id = %s ORDER BY id",
    )


async def _fetch_export_places(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT id, name, kind::text AS kind, "
        " ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon, radius_m "
        "FROM places WHERE account_id = %s ORDER BY id",
    )


async def _fetch_export_tag_rules(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT a_place, a_kind::text AS a_kind, b_place, b_kind::text AS b_kind, "
        " category::text AS category FROM tag_rules WHERE account_id = %s ORDER BY id",
    )


async def _fetch_export_mileage_rates(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT year, rate_per_mi::float AS rate_per_mi, "
        " rate_h2_per_mi::float AS rate_h2_per_mi, h2_start_month "
        "FROM mileage_rates WHERE account_id = %s ORDER BY year",
    )


async def _fetch_export_trips(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT id, device, source::text AS source, started_at, ended_at, "
        # The distance a report actually counts -- snapped when available,
        # raw otherwise -- travels with the trip even though the path
        # geometry that produced a snapped distance does not; recomputing it
        # without that geometry isn't possible, and re-exporting the raw
        # figure instead would silently change a source instance's own
        # report totals on round trip.
        f" {DISPLAY_DISTANCE_SQL} AS distance_m, has_gap, "
        " category::text AS category, exclusion::text AS exclusion, "
        " purpose, notes, vehicle_id, "
        " start_place_id, end_place_id, tag_source::text AS tag_source, "
        " start_label, end_label "
        "FROM trips WHERE account_id = %s ORDER BY id",
    )


async def _fetch_export_expenses(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT vehicle_id, incurred_on, category::text AS category, amount, "
        " treatment::text AS treatment, notes, trip_id FROM expenses WHERE account_id = %s ORDER BY id",
    )


async def _fetch_export_odometer_readings(conn) -> list[dict]:
    return await _fetch_all(
        conn,
        "SELECT vehicle_id, recorded_at, odometer_m, note FROM odometer_readings WHERE account_id = %s ORDER BY id",
    )


async def _fetch_export_settings(conn) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT auto_assign_default_vehicle, display_tz FROM account_settings WHERE account_id = %s", (account_id(conn),))
    return await cur.fetchone()
