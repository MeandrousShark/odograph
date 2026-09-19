"""Fail-closed ownership migration and one-time legacy preference import."""
from __future__ import annotations

import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_settings import AccountSettings, CONFIG_PREFERENCE_COLUMNS
from app.local_auth import hash_password


class OwnershipMigrationError(RuntimeError):
    """The legacy database cannot be assigned an owner without a reviewed repair."""


PERSONAL_TABLES = (
    "raw_messages", "points", "stays", "trips", "places", "geocode_cache",
    "trip_boundary_overrides", "odometer_readings", "expenses",
    "nudge_delivery_windows", "odometer_reminder_windows", "email_deliveries",
)
REFERENCE_RATES = ((2025, Decimal("0.7000"), None, None),
                   (2026, Decimal("0.7250"), None, None))


async def _rows(conn, query: str):
    return await (await conn.execute(query)).fetchall()


async def preflight_ownership(conn) -> None:
    """Read only; call before migration 026 in its privileged transaction."""
    count = (await _rows(conn, "SELECT count(*) FROM accounts"))[0][0]
    if count > 1:
        raise OwnershipMigrationError("ownership migration requires at most one established account")
    checkpoints = await _rows(
        conn, "SELECT id, last_run_at, detector_version FROM detector_state"
    )
    if len(checkpoints) != 1 or checkpoints[0][0] != 1:
        raise OwnershipMigrationError("legacy detector checkpoint is inconsistent")
    settings = await _rows(
        conn, "SELECT id, auto_assign_default_vehicle FROM app_settings"
    )
    if len(settings) != 1 or settings[0][0] != 1:
        raise OwnershipMigrationError("legacy preferences are inconsistent")
    invalid = await _rows(conn, """
        SELECT EXISTS (
            SELECT 1 FROM points p LEFT JOIN trips t ON t.id = p.trip_id
            WHERE p.trip_id IS NOT NULL AND
                (t.id IS NULL OR p.device <> t.device OR t.source <> 'detected' OR t.imported)
        ) OR EXISTS (
            SELECT 1 FROM trip_boundary_overrides o LEFT JOIN points p ON p.id = o.point_id
            WHERE o.point_id IS NOT NULL AND (p.id IS NULL OR p.device <> o.device)
        ) OR EXISTS (
            SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
            WHERE n.nspname='public' AND c.contype IN ('f','c') AND NOT c.convalidated
        )
    """)
    if invalid[0][0]:
        raise OwnershipMigrationError("legacy relationships need repair before ownership migration")
    edges = (
        ("trips", "start_place_id", "places"),
        ("trips", "end_place_id", "places"),
        ("trips", "vehicle_id", "vehicles"),
        ("tag_rules", "a_place", "places"), ("tag_rules", "b_place", "places"),
        ("expenses", "vehicle_id", "vehicles"), ("expenses", "trip_id", "trips"),
        ("odometer_readings", "vehicle_id", "vehicles"),
        ("oidc_identities", "account_id", "accounts"),
    )
    for child, column, parent in edges:
        orphan = await _rows(conn,
            f"SELECT EXISTS (SELECT 1 FROM {child} c LEFT JOIN {parent} p "
            f"ON c.{column}=p.id WHERE c.{column} IS NOT NULL AND p.id IS NULL)")
        if orphan[0][0]:
            raise OwnershipMigrationError("legacy references need repair before ownership migration")
    guard = await _rows(conn, """
        SELECT EXISTS (
            SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            WHERE c.relname='accounts_singleton_idx' AND i.indrelid='accounts'::regclass
                AND i.indisunique AND i.indisvalid AND i.indisready
                AND i.indnkeyatts = 1 AND i.indnatts = 1 AND i.indkey[0] = 0
                AND pg_get_expr(i.indexprs, i.indrelid) = 'true'
                AND i.indpred IS NULL
        ) AND EXISTS (
            SELECT 1 FROM pg_constraint WHERE conrelid='accounts'::regclass
                AND contype='c' AND convalidated
                AND pg_get_constraintdef(oid)='CHECK (is_admin)'
        )
    """)
    if not guard[0][0]:
        raise OwnershipMigrationError("legacy account guards are inconsistent")
    if count:
        return

    for table in PERSONAL_TABLES:
        if (await _rows(conn, f"SELECT EXISTS (SELECT 1 FROM {table})"))[0][0]:
            raise OwnershipMigrationError(
                "personal data has no established account; establish its owner on the previous release"
            )
    vehicles = await _rows(
        conn, "SELECT name, make, model, plate, is_default, active FROM vehicles"
    )
    rules = await _rows(conn, """
        SELECT a_place, a_kind::text, b_place, b_kind::text, category::text
        FROM tag_rules ORDER BY a_kind::text, b_kind::text, category::text
    """)
    rates = await _rows(conn, """
        SELECT year, rate_per_mi, rate_h2_per_mi, h2_start_month
        FROM mileage_rates ORDER BY year
    """)
    if (vehicles != [("My Car", None, None, None, True, True)]
            or rules != [(None, "home", None, "work", "personal"),
                         (None, "work", None, "work", "business")]
            or tuple(rates) != REFERENCE_RATES
            or settings != [(1, False)]
            or checkpoints != [(1, None, 0)]):
        raise OwnershipMigrationError(
            "modified defaults have no established account; establish their owner on the previous release"
        )


def legacy_rate_overrides(environ) -> dict[int, Decimal]:
    """Match legacy effective overrides without rounding them to four decimals."""
    result = {}
    for key, value in environ.items():
        if not key.startswith("MILEAGE_RATE_"):
            continue
        try:
            year = int(key.removeprefix("MILEAGE_RATE_"))
            number = float(value)
        except (ValueError, OverflowError):
            continue
        if math.isfinite(number) and number > 0:
            if not -(2**31) <= year < 2**31:
                raise OwnershipMigrationError("a legacy mileage-rate year does not fit the database")
            result[year] = Decimal(str(number))
    return result


def _legacy_location_label(payload: dict, received_at: datetime) -> str | None:
    lat, lon, tst = (payload.get(key) for key in ("lat", "lon", "tst"))
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           for value in (lat, lon, tst)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    if not math.isfinite(tst) or not 0 < tst <= 4102444800:
        return None
    if datetime.fromtimestamp(tst, timezone.utc) > received_at + timedelta(minutes=5):
        return None
    if payload.get("t") is not None and not isinstance(payload["t"], str):
        return None
    label = payload.get("tid")
    # jsonb canonicalizes object key order; a historical str(dict) stream
    # label cannot be recovered reliably from that reordered representation.
    if isinstance(label, (dict, list)) and label:
        return None
    return str(label or "default")


async def _attribute_legacy_raw_messages(conn, owner: int) -> None:
    # Only a historically valid location object establishes a parsed stream.
    # Invalid/non-location objects keep their owner and nullable attribution.
    # The original Python str()/truthiness behavior is deliberately preserved.
    async with conn.cursor(name="ownership_raw_messages", row_factory=dict_row) as cur:
        await cur.execute("SELECT id, received_at, payload FROM raw_messages WHERE account_id = %s ORDER BY id", (owner,))
        async for row in cur:
            payload = row["payload"]
            if not isinstance(payload, dict) or payload.get("_type") != "location":
                continue
            label = _legacy_location_label(payload, row["received_at"])
            if label is None:
                continue
            lookup = await conn.execute(
                "SELECT tracking_device_id FROM tracking_device_aliases "
                "WHERE account_id=%s AND original_label=%s", (owner, label)
            )
            found = await lookup.fetchone()
            if found is None:
                inserted = await conn.execute(
                    "INSERT INTO tracking_devices(account_id,label) VALUES(%s,%s) RETURNING id",
                    (owner, label),
                )
                device_id = (await inserted.fetchone())[0]
                await conn.execute(
                    "INSERT INTO tracking_device_aliases(account_id,original_label,tracking_device_id) "
                    "VALUES(%s,%s,%s)", (owner, label, device_id),
                )
                await conn.execute(
                    "INSERT INTO detector_state(account_id,tracking_device_id,last_run_at,detector_version) "
                    "SELECT %s,%s,last_run_at,detector_version FROM ownership_legacy_checkpoint",
                    (owner, device_id),
                )
            else:
                device_id = found[0]
            await conn.execute(
                "UPDATE raw_messages SET tracking_device_id=%s WHERE account_id=%s AND id=%s",
                (device_id, owner, row["id"]),
            )


async def import_legacy_configuration(conn, config=None, *, environ=None) -> None:
    """Complete migration 026 before its ledger record/transaction commits.

    A missing config means neutral preferences (for explicit disposable test
    setup), never a second environment load. Normal startup passes its already
    parsed Config. Rate overrides are snapshotted once because the legacy rate
    loader read them outside Config.
    """
    row = (await _rows(conn, """
        SELECT first_account_id, legacy_config_imported_at
        FROM instance_state WHERE id=1 FOR UPDATE
    """))[0]
    owner, imported_at = row
    if imported_at is not None:
        return
    if owner is not None:
        settings = AccountSettings(display_tz=ZoneInfo("UTC"))
        values = {
            name: getattr(config, name) if config is not None else getattr(settings, name)
            for name in CONFIG_PREFERENCE_COLUMNS
        }
        values["display_tz"] = str(values["display_tz"])
        await conn.execute(
            "UPDATE account_settings SET "
            + ", ".join(f"{name}=%s" for name in values)
            + " WHERE account_id=%s", tuple(values.values()) + (owner,),
        )
        rate_environ = dict(os.environ) if environ is None else dict(environ)
        for year, rate in legacy_rate_overrides(rate_environ).items():
            await conn.execute(
                "INSERT INTO mileage_rates(account_id,year,rate_per_mi) VALUES(%s,%s,%s) "
                "ON CONFLICT(account_id,year) DO UPDATE SET rate_per_mi=excluded.rate_per_mi, "
                "rate_h2_per_mi=NULL,h2_start_month=NULL,updated_at=now()",
                (owner, year, rate),
            )
        if config is not None and config.ingest_username and config.ingest_password:
            await conn.execute(
                "INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,account_id,kind) "
                "VALUES(%s,%s,%s,%s,'legacy')",
                (secrets.token_urlsafe(24), config.ingest_username,
                 hash_password(config.ingest_password), owner),
            )
        await _attribute_legacy_raw_messages(conn, owner)
    await conn.execute("UPDATE instance_state SET legacy_config_imported_at=now() WHERE id=1")
