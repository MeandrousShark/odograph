from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from psycopg import errors

from app.accounts import (
    create_admin,
    get_sole_account,
    normalize_email,
    replace_password,
    valid_email,
)
from app.auth import MIN_LOCAL_PASSWORD_LENGTH
from app.application_roles import application_role_pools
from app.account_context import control_connection
from app.local_auth import hash_password


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
            if await get_sole_account(conn) is not None:
                raise ValueError("An administrator account already exists.")
            try:
                await create_admin(conn, email, password_hash, display_timezone=os.environ.get("DISPLAY_TZ", "UTC"))
            except (errors.UniqueViolation, errors.CheckViolation) as exc:
                raise ValueError("An administrator account already exists.") from exc


async def _reset_password(database_url: str) -> None:
    password = _read_new_password()
    password_hash = await asyncio.to_thread(hash_password, password)

    async with application_role_pools(database_url) as pools:
        async with control_connection(pools.control) as conn:
            account = await get_sole_account(conn)
            if account is None:
                raise ValueError(
                    "No administrator account exists. Run create-admin instead."
                )
            updated = await replace_password(conn, account["id"], password_hash)
            if updated is None:
                raise ValueError("The administrator password could not be reset.")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "error: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Create or recover the sole Odograph administrator account."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    # Build both public subcommands without accepting credential arguments.
    subcommands.add_parser("create-admin")
    subcommands.add_parser("reset-password")
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
        else:
            asyncio.run(_reset_password(database_url))
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
        else "Administrator password reset. Existing sessions are no longer valid."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
