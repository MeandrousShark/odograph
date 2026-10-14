"""Run pending detector work for one account outside the background scheduler.

Background turns defer a device whose rewind window exceeds
DETECTOR_TURN_MAX_POINTS. This command runs the same incremental or full pass
for that account with no turn limit, one device per transaction, while
holding the account's detector lock. It uses the restricted runtime role and
row-level security like the application, and storage admission still applies.
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
from app.capacity import AdmissionManager
from app.config import Config
from app.db import SchemaTooNewError
from app.detector.runner import DetectorRunner, DeviceNotAdmitted
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
        description="Run pending detector work for one account without the background "
        "turn limit (DETECTOR_TURN_MAX_POINTS)."
    )
    parser.add_argument("--account", type=_positive_id, required=True, metavar="ACCOUNT_ID")
    parser.add_argument("--device", type=_positive_id, metavar="DEVICE_ID",
                        help="one tracking device; default is every device with pending work")
    return parser


async def _reprocess_device(runner: DetectorRunner, device: int, *, explicit: bool) -> str:
    async with runner.pool.connection() as conn:
        # Own process and connection: no serving lock timeout, and a large
        # window may legitimately run longer than any request deadline.
        await conn.execute("SELECT set_config('lock_timeout', '0', true)")
        await conn.execute("SELECT set_config('statement_timeout', '0', true)")
        try:
            processed = await runner.run_pending_in(conn, device)
        except DeviceNotAdmitted:
            if explicit:
                raise
            return f"device {device}: no longer enabled, skipped"
    stats = runner.last_stats
    if not processed or stats is None:
        return f"device {device}: no pending work"
    return (
        f"device {device}: {stats['points']} points processed, "
        f"{stats['stays']} stays and {stats['trips']} trips detected "
        f"({stats['updated']} updated, {stats['new']} new, {stats['deleted']} deleted)"
    )


async def _reprocess(cfg: Config, owner: int, device: int | None) -> bool:
    """Return True when every selected device completed."""
    async with application_role_pools(cfg.database_url) as pools:
        async with control_connection(pools.control) as conn:
            cur = await conn.execute(
                "SELECT id, is_enabled, auth_version FROM accounts WHERE id = %s", (owner,))
            row = await cur.fetchone()
        if row is None:
            raise ValueError("No account has that ID. Run python -m app.manage_account list-accounts to find it.")
        if not row[1]:
            raise ValueError("That account is disabled; its detector work was not run.")
        principal = AccountPrincipal(*row)
        # The detector's CPU step requires an operation owner. This process
        # has its own admission manager, so the owner is honest here without
        # taking any slot from the server's lanes; the database lock and
        # storage checks still apply across processes.
        capacity = AdmissionManager(cfg)
        async with external_account_work(pools.control, owner):
            pool = AccountPool(pools.runtime, principal)
            runner = DetectorRunner(pool, cfg.detector_params, cfg.full_reprocess_warn_points)
            if device is None:
                async with pool.connection() as conn:
                    devices = await runner.pending_devices(conn)
                if not devices:
                    print(f"account {owner}: no pending work")
                    return True
            else:
                devices = [device]
            ok = True
            for selected in devices:
                try:
                    async with capacity.operation("background", principal=principal):
                        message = await _reprocess_device(runner, selected, explicit=device is not None)
                    print(message)
                except DeviceNotAdmitted as exc:
                    print(f"error: device {selected}: {exc}", file=sys.stderr)
                    ok = False
                except errors.RaiseException as exc:
                    if is_storage_capacity_error(exc):
                        print(f"error: device {selected}: storage is full for this account; "
                              "nothing was saved for this device", file=sys.stderr)
                    else:
                        print(f"error: device {selected}: reprocess failed ({type(exc).__name__})",
                              file=sys.stderr)
                    ok = False
                except Exception as exc:
                    # Database exceptions can carry connection details or data.
                    print(f"error: device {selected}: reprocess failed ({type(exc).__name__})",
                          file=sys.stderr)
                    ok = False
            return ok


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
        ok = asyncio.run(_reprocess(cfg, args.account, args.device))
    except (ValueError, SchemaTooNewError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: detector reprocess failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
