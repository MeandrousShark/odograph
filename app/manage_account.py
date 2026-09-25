from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from psycopg import errors

from psycopg.rows import dict_row

from app.accounts import (
    account_exists,
    create_admin,
    normalize_email,
    valid_email,
)
from app.auth import MIN_LOCAL_PASSWORD_LENGTH
from app.application_roles import application_role_pools
from app.account_context import control_connection
from app.local_auth import hash_password
from app.password_reset import host_reset_password


def _read_value(prompt: str, *, secret: bool = False) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt) if secret else input(prompt)
    value = sys.stdin.readline()
    if value == "":
        raise ValueError("Input ended before all required values were read.")
    return value.rstrip("\r\n")


def _read_new_password() -> str:
    password = _read_value("Password: ", secret=True)
    confirmation = _read_value("Confirm password: ", secret=True)
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    if len(password) < MIN_LOCAL_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_LOCAL_PASSWORD_LENGTH} characters."
        )
    return password


async def _create_admin(database_url: str) -> None:
    email = normalize_email(_read_value("Administrator email: "))
    if not valid_email(email):
        raise ValueError("Enter a valid ASCII email address.")
    password = _read_new_password()
    password_hash = await asyncio.to_thread(hash_password, password)

    async with application_role_pools(database_url) as pools:
        async with control_connection(pools.control) as conn:
            if await account_exists(conn):
                raise ValueError("An administrator account already exists.")
            try:
                await create_admin(conn, email, password_hash, display_timezone=os.environ.get("DISPLAY_TZ", "UTC"))
            except (errors.UniqueViolation, errors.CheckViolation) as exc:
                raise ValueError("An administrator account already exists.") from exc


async def _account_rows(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, email, is_admin, is_enabled, email_verified_at IS NOT NULL AS verified "
        "FROM accounts ORDER BY id"
    )
    return await cur.fetchall()


def _describe(account: dict) -> str:
    return (
        f"{account['id']}\t{account['email']}\t"
        f"{'admin' if account['is_admin'] else 'member'}\t"
        f"{'enabled' if account['is_enabled'] else 'disabled'}\t"
        f"{'verified' if account['verified'] else 'unverified'}"
    )


async def _list_accounts(database_url: str) -> None:
    async with application_role_pools(database_url) as pools:
        async with control_connection(pools.control) as conn:
            rows = await _account_rows(conn)
    print("ID\tLOGIN EMAIL\tROLE\tSTATUS\tEMAIL")
    for account in rows:
        print(_describe(account))


async def _reset_password(database_url: str, account_id: int) -> None:
    """Explicit-target host recovery. Never falls back to another account."""
    async with application_role_pools(database_url) as pools:
        async with control_connection(pools.control) as conn:
            rows = {row["id"]: row for row in await _account_rows(conn)}
        target = rows.get(account_id)
        if target is None:
            raise ValueError("No account has that ID. Run list-accounts to find it.")
        if not target["is_enabled"]:
            raise ValueError("That account is disabled; host recovery does not re-enable it.")
        print("ID\tLOGIN EMAIL\tROLE\tSTATUS\tEMAIL")
        print(_describe(target))
        answer = _read_value("Reset this account's password? Type yes to continue: ")
        if answer.strip().lower() != "yes":
            raise ValueError("Cancelled; nothing changed.")
        password = _read_new_password()
        password_hash = await asyncio.to_thread(hash_password, password)
        async with control_connection(pools.control) as conn:
            async with conn.transaction():
                updated = await host_reset_password(conn, account_id, password_hash)
        if updated is None:
            raise ValueError("The password could not be reset; the account is missing or disabled.")


def _account_id(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError("invalid account ID")
    number = int(value)
    if not 1 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("invalid account ID")
    return number


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Create the first Odograph administrator, list accounts, or recover "
        "one account's password on this host."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    # No subcommand accepts a credential argument; passwords are prompted.
    subcommands.add_parser("create-admin")
    subcommands.add_parser("list-accounts")
    reset = subcommands.add_parser("reset-password")
    reset.add_argument("account_id", type=_account_id)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("error: DATABASE_URL is not configured", file=sys.stderr)
        return 1
    try:
        if args.command == "create-admin":
            asyncio.run(_create_admin(database_url))
        elif args.command == "list-accounts":
            asyncio.run(_list_accounts(database_url))
            return 0
        else:
            asyncio.run(_reset_password(database_url, args.account_id))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Database and driver exceptions can contain connection details.
        # Recovery failures stay generic and never echo DATABASE_URL.
        print("error: account operation failed", file=sys.stderr)
        return 1
    print(
        "Administrator account created."
        if args.command == "create-admin"
        else "Password reset. That account's existing sessions are no longer valid."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
