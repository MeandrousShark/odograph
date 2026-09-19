"""Account-owned preferences, separate from operator transport configuration."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from app.account_context import account_id


@dataclass(frozen=True, slots=True)
class AccountSettings:
    display_tz: ZoneInfo
    auto_assign_default_vehicle: bool = False
    ntfy_topic: str = ""
    nudge_weekly_hour: int = 18
    odometer_reminder_requested: bool = False
    odometer_reminder_hour: int = 9
    email_to: str = ""
    email_weekly_nudge: bool = False
    email_monthly_summary: bool = False
    email_filing_reminder: bool = False
    email_odometer_reminder: bool = False
    email_digest_hour: int = 9
    email_filing_reminder_mmdd: str = "01-15"


SETTINGS_COLUMNS = tuple(field.name for field in fields(AccountSettings))
CONFIG_PREFERENCE_COLUMNS = tuple(
    name for name in SETTINGS_COLUMNS if name != "auto_assign_default_vehicle"
)


async def load_account_settings(conn) -> AccountSettings:
    owner = account_id(conn)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {', '.join(SETTINGS_COLUMNS)} FROM account_settings WHERE account_id = %s",
        (owner,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("account preferences are missing")
    row["display_tz"] = ZoneInfo(row["display_tz"])
    return AccountSettings(**row)


def config_for_account(config, settings: AccountSettings):
    """Return a request/job-local configuration with this account's preferences."""
    return replace(config, **{
        name: getattr(settings, name) for name in CONFIG_PREFERENCE_COLUMNS
    })
