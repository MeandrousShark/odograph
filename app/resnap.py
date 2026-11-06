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
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from psycopg import errors

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.account_work import external_account_work
from app.application_roles import application_role_pools
from app.config import Config
from app.db import SchemaTooNewError
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
        description="Queue an account's trips with a split road snap to be snapped again."
    )
    parser.add_argument("--account", type=_positive_id, required=True, metavar="ACCOUNT_ID")
    parser.add_argument("--device", type=_positive_id, metavar="DEVICE_ID",
                        help="only trips from one tracking device; default is every device")
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


async def _resnap(cfg: Config, owner: int, device: int | None) -> bool:
    """Return True when the trips were queued."""
    async with application_role_pools(cfg.database_url) as pools:
        async with control_connection(pools.control) as conn:
            cur = await conn.execute(
                "SELECT id, is_enabled, auth_version FROM accounts WHERE id = %s", (owner,))
            row = await cur.fetchone()
        if row is None:
            raise ValueError("No account has that ID. Run python -m app.manage_account list-accounts to find it.")
        if not row[1]:
            raise ValueError("That account is disabled; its trips were not queued.")
        principal = AccountPrincipal(*row)
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
