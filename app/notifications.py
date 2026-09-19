from __future__ import annotations

from datetime import datetime

import httpx

from app.odometer import vehicles_due_for_reminder
from app.account_context import account_id
from app.account_settings import SETTINGS_COLUMNS
from psycopg.rows import dict_row


async def notification_preferences_current(conn, **expected) -> bool:
    """Lock the preferences through send/ledger commit and reject stale jobs."""
    if not expected or not set(expected) <= set(SETTINGS_COLUMNS):
        raise ValueError("notification preference comparison is incomplete")
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT " + ", ".join(expected) + " FROM account_settings "
        "WHERE account_id=%s FOR SHARE", (account_id(conn),),
    )
    row = await cur.fetchone()
    return row is not None and all(row[name] == value for name, value in expected.items())


async def count_unclassified_trips(
    conn, window_start: datetime, window_end: datetime,
) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM trips WHERE account_id = %s AND category = 'unclassified' "
        "AND started_at >= %s AND started_at < %s",
        (account_id(conn), window_start, window_end),
    )
    return (await cur.fetchone())[0]


async def odometer_reminder_vehicles(conn, quarter_start: datetime) -> list[str]:
    vehicles_cur = await conn.execute(
        "SELECT id, name FROM vehicles WHERE account_id = %s AND active ORDER BY name",
        (account_id(conn),),
    )
    active_vehicles = await vehicles_cur.fetchall()
    readings_cur = await conn.execute(
        "SELECT DISTINCT vehicle_id FROM odometer_readings WHERE account_id = %s AND recorded_at >= %s",
        (account_id(conn), quarter_start),
    )
    logged_ids = {row[0] for row in await readings_cur.fetchall()}
    return vehicles_due_for_reminder(active_vehicles, logged_ids)


async def publish_ntfy(
    http_client: httpx.AsyncClient,
    ntfy_url: str,
    topic: str,
    token: str,
    username: str,
    password: str,
    message: str,
) -> None:
    headers = {"Title": "Odograph", "Tags": "car", "Priority": "default"}
    auth = None
    if username and password:
        auth = httpx.BasicAuth(username, password)
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    response = await http_client.post(
        f"{ntfy_url.rstrip('/')}/{topic}", content=message, headers=headers, auth=auth,
    )
    response.raise_for_status()
