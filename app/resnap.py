"""Queue an account's split road snaps to be snapped again.

Road snapping used to ask OSRM to split a trace at every time gap of about a
minute, so a trip that waited at a stoplight with a distance-based tracker was
stored as several snapped pieces and the distance between the pieces was
dropped. Snapping no longer splits at time gaps, but trips snapped earlier keep
their old multi-part path. Run this once for each account after upgrading to
the release that stops time-gap splits:

    python -m app.resnap --account ACCOUNT_ID [--device DEVICE_ID]

It queues every detected, non-imported trip whose snapped path has more than
one part, the same way a detector rewrite does: the snapped path and distance
are cleared and the trip is marked pending. The running server's snap worker
then snaps each one again on its next sweep (SNAP_SWEEP_S, 300 seconds by
default), one trip per turn. Until a trip is snapped again it is shown with its
raw path. Manual and imported trips, and trips with a single-part path, are not
touched, nor are trips on a disabled or revoked tracking device, which the snap
worker does not pick up. The command uses the restricted runtime role and
row-level security like the application, runs as one transaction, and is safe
to repeat.

    python -m app.resnap --account ACCOUNT_ID --declined [--device DEVICE_ID] [--dry-run]

clears the snapped distance the current snap rule would decline: a detected
trip without a recording gap whose snapped distance is more than
app.snap.SNAP_MAX_COVERAGE times its raw distance. The trip is marked
low_confidence and shows its raw distance; its snapped route is kept. Each
cleared trip is printed to standard output as CSV with the value it had, so a
correction can be undone from that list, and the count goes to standard error.
--dry-run prints the same list and changes nothing. No OSRM call is made, so
trips on a disabled or revoked device are included.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys

import psycopg
from psycopg import errors

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_work import external_account_work
from app.application_roles import application_role_pools
from app.config import Config
from app.db import SchemaTooNewError
from app.snap import SNAP_MAX_COVERAGE
from app.storage import is_storage_capacity_error


def _positive_id(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError("invalid ID")
    number = int(value)
    if not 1 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("invalid ID")
    return number


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Queue an account's trips with a split road snap to be snapped again, "
                    "or with --declined clear snapped distances the snap rule declines."
    )
    parser.add_argument("--account", type=_positive_id, required=True, metavar="ACCOUNT_ID")
    parser.add_argument("--device", type=_positive_id, metavar="DEVICE_ID",
                        help="only trips from one tracking device; default is every device")
    parser.add_argument("--declined", action="store_true",
                        help="clear snapped distances the snap rule now declines, instead of queueing")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --declined, list the trips without changing them")
    return parser


_REQUEUE = (
    "UPDATE trips SET snap_status = 'pending'::snap_state, snap_capacity_needed_bytes = 0, "
    "snap_attempted_at = NULL, path_snapped = NULL, distance_snapped_m = NULL, "
    "snapped_at = NULL, updated_at = now() "
    "WHERE account_id = %s AND source = 'detected' AND NOT imported "
    "AND path_snapped IS NOT NULL AND ST_NumGeometries(path_snapped) > 1 "
    "AND EXISTS (SELECT 1 FROM tracking_devices d WHERE d.account_id = trips.account_id "
    "AND d.id = trips.tracking_device_id AND d.enabled AND d.revoked_at IS NULL)"
)


async def _principal(pools, owner: int) -> AccountPrincipal:
    async with control_connection(pools.control) as conn:
        cur = await conn.execute(
            "SELECT id, is_enabled, auth_version FROM accounts WHERE id = %s", (owner,))
        row = await cur.fetchone()
    if row is None:
        raise ValueError("No account has that ID. Run python -m app.manage_account list-accounts to find it.")
    if not row[1]:
        raise ValueError("That account is disabled; its trips were not queued.")
    return AccountPrincipal(*row)


async def _resnap(cfg: Config, owner: int, device: int | None) -> bool:
    """Return True when the trips were queued."""
    async with application_role_pools(cfg.database_url) as pools:
        principal = await _principal(pools, owner)
        # The shared account work lock blocks account purge while the update
        # runs. A snap result already in flight is discarded by the worker's
        # updated_at generation check, because the requeue sets updated_at = now().
        async with external_account_work(pools.control, owner):
            pool = AccountPool(pools.runtime, principal)
            try:
                async with pool.connection() as conn:
                    await conn.execute("SELECT set_config('lock_timeout', '0', true)")
                    await conn.execute("SELECT set_config('statement_timeout', '0', true)")
                    sql, args = _REQUEUE, [owner]
                    if device is not None:
                        cur = await conn.execute(
                            "SELECT enabled, revoked_at IS NOT NULL FROM tracking_devices "
                            "WHERE account_id = %s AND id = %s", (owner, device))
                        found = await cur.fetchone()
                        if found is None:
                            raise ValueError("No tracking device has that ID for this account.")
                        if found[1]:
                            raise ValueError("That tracking device is revoked; its trips were not queued.")
                        if not found[0]:
                            raise ValueError("That tracking device is disabled; its trips were not queued.")
                        sql += " AND tracking_device_id = %s"
                        args.append(device)
                    cur = await conn.execute(sql, args)
                    count = cur.rowcount
            except errors.RaiseException as exc:
                if is_storage_capacity_error(exc):
                    print("error: storage is full for this account; nothing was queued",
                          file=sys.stderr)
                else:
                    print(f"error: queueing failed ({type(exc).__name__})", file=sys.stderr)
                return False
    if count == 0:
        print(f"account {owner}: no split trips")
    else:
        noun = "trip" if count == 1 else "trips"
        print(f"account {owner}: {count} {noun} queued for snapping")
    return True


# app.snap.parse_match_response's ceiling restated on stored columns, so the
# command clears exactly what a new snap would decline. Gap trips are exempt
# there, and a zero raw distance never fires it. Status is not tested: the
# ceiling declines a match whatever its confidence.
_DECLINED_PREDICATE = (
    "t.account_id = %s AND t.source = 'detected' AND NOT t.has_gap "
    "AND t.distance_snapped_m IS NOT NULL AND t.distance_m > 0 "
    "AND t.distance_snapped_m > %s * t.distance_m"
)

_DECLINED_SELECT = (
    "SELECT t.id, (t.started_at AT TIME ZONE "
    "(SELECT s.display_tz FROM account_settings s WHERE s.account_id = t.account_id))::date, "
    "t.category::text, t.distance_m::text, t.distance_snapped_m::text "
    "FROM trips t WHERE " + _DECLINED_PREDICATE
)

_CSV_HEADER = ("trip_id", "local_start_date", "category", "distance_m", "distance_snapped_m")


async def _clear_declined(cfg: Config, owner: int, device: int | None, dry_run: bool) -> bool:
    """Return True when the declined distances were listed, and cleared unless dry_run."""
    async with application_role_pools(cfg.database_url) as pools:
        principal = await _principal(pools, owner)
        # Same account work lock as the requeue. Unlike it, updated_at is left
        # alone: the trips are not pending, and updated_at also orders recent
        # purpose suggestions. A snap in flight cannot be pending on a trip
        # this selects, because those already hold a snapped distance.
        async with external_account_work(pools.control, owner):
            pool = AccountPool(pools.runtime, principal)
            try:
                async with pool.connection() as conn:
                    await conn.execute("SELECT set_config('lock_timeout', '0', true)")
                    await conn.execute("SELECT set_config('statement_timeout', '0', true)")
                    sql, args = _DECLINED_SELECT, [owner, SNAP_MAX_COVERAGE]
                    if device is not None:
                        # No worker is involved, so a disabled or revoked
                        # device's trips are corrected too.
                        cur = await conn.execute(
                            "SELECT 1 FROM tracking_devices WHERE account_id = %s AND id = %s",
                            (owner, device))
                        if await cur.fetchone() is None:
                            raise ValueError("No tracking device has that ID for this account.")
                        sql += " AND t.tracking_device_id = %s"
                        args.append(device)
                    sql += " ORDER BY t.started_at, t.id"
                    if not dry_run:
                        sql += " FOR UPDATE OF t"
                    cur = await conn.execute(sql, args)
                    rows = await cur.fetchall()
                    # Listed before the clear, so a failure after it can
                    # never leave cleared values without their record.
                    writer = csv.writer(sys.stdout, lineterminator="\n")
                    writer.writerow(_CSV_HEADER)
                    writer.writerows(rows)
                    sys.stdout.flush()
                    if rows and not dry_run:
                        await conn.execute(
                            "UPDATE trips SET distance_snapped_m = NULL, "
                            "snap_status = 'low_confidence'::snap_state "
                            "WHERE account_id = %s AND id = ANY(%s)",
                            (owner, [row[0] for row in rows]))
            except psycopg.Error as exc:
                print(f"error: clearing failed ({type(exc).__name__}); nothing was changed",
                      file=sys.stderr)
                return False
    count = len(rows)
    noun = "trip" if count == 1 else "trips"
    if count == 0:
        print(f"account {owner}: no declined snapped distances to clear", file=sys.stderr)
    elif dry_run:
        print(f"account {owner}: {count} {noun} would be cleared", file=sys.stderr)
    else:
        print(f"account {owner}: {count} {noun} cleared", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.dry_run and not args.declined:
        parser.error("--dry-run requires --declined")
    if not os.environ.get("DATABASE_URL", ""):
        print("error: DATABASE_URL is not configured", file=sys.stderr)
        return 1
    try:
        cfg = Config.from_env()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("error: a numeric setting in the environment is invalid", file=sys.stderr)
        return 1
    try:
        if args.declined:
            ok = asyncio.run(_clear_declined(cfg, args.account, args.device, args.dry_run))
        else:
            ok = asyncio.run(_resnap(cfg, args.account, args.device))
    except (ValueError, SchemaTooNewError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Database exceptions can carry connection details or data.
        print(f"error: resnap failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
