"""Vehicle CRUD. Trips carry a nullable
`vehicle_id` (migrations/008_vehicles.sql) so the per-vehicle IRS mileage
deduction can be priced correctly -- each vehicle has its own basis/
depreciation history in the taxpayer's own records, so lumping all trips
into one deduction would be wrong once a second vehicle is in the mix.

These helpers all take an already-open `conn` (not the pool) so a caller
that needs several of them -- e.g. `set_default_vehicle`'s two UPDATEs --
gets them in the same transaction for free, the same convention
`app/rates.py`'s `load_rates(conn)` uses.
"""
from __future__ import annotations

from psycopg.rows import dict_row
from fastapi import HTTPException

from app.account_context import account_id


async def list_vehicles(conn, include_inactive: bool = False) -> list[dict]:
    """Active vehicles by default -- the picker for *new* trip assignment
    (settings' add form, the manual-trip form, the filter bar) shouldn't
    offer a vehicle the user has retired. Callers that also need to render
    a trip's already-assigned (possibly inactive) vehicle pass
    `include_inactive=True` or otherwise union in that one row themselves.
    """
    cur = conn.cursor(row_factory=dict_row)
    where = "" if include_inactive else " AND active"
    await cur.execute(
        f"SELECT id, name, make, model, plate, is_default, active "
        f"FROM vehicles WHERE account_id = %s{where} ORDER BY name",
        (account_id(conn),),
    )
    return await cur.fetchall()


async def create_vehicle(
    conn,
    name: str,
    make: str | None = None,
    model: str | None = None,
    plate: str | None = None,
    is_default: bool = False,
) -> int:
    if is_default:
        # Clear any existing default first so the partial unique index
        # (`vehicles_one_default_idx`, WHERE is_default) never sees two
        # true rows at once, even momentarily within this transaction.
        await conn.execute(
            "UPDATE vehicles SET is_default = false WHERE account_id = %s AND is_default",
            (account_id(conn),),
        )
    cur = await conn.execute(
        "INSERT INTO vehicles (account_id, name, make, model, plate, is_default) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (account_id(conn), name, make, model, plate, is_default),
    )
    row = await cur.fetchone()
    return row[0]


async def update_vehicle(
    conn,
    vehicle_id: int,
    name: str,
    make: str | None = None,
    model: str | None = None,
    plate: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE vehicles SET name = %s, make = %s, model = %s, plate = %s WHERE id = %s AND account_id = %s",
        (name, make, model, plate, vehicle_id, account_id(conn)),
    )


async def set_default_vehicle(conn, vehicle_id: int) -> None:
    """Clear the previous default and set `vehicle_id` as the new one.
    Clearing first, then setting, is load-bearing: doing it the other way
    (or as one UPDATE ... OR) would momentarily (or permanently, if the two
    rows differ) violate `vehicles_one_default_idx`, which only tolerates
    zero or one `is_default = true` row at a time.
    """
    cur = await conn.execute(
        "SELECT id FROM vehicles WHERE id = %s AND account_id = %s AND active FOR UPDATE",
        (vehicle_id, account_id(conn)),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="No such active vehicle")
    await conn.execute(
        "UPDATE vehicles SET is_default = false WHERE account_id = %s AND is_default",
        (account_id(conn),),
    )
    await conn.execute(
        "UPDATE vehicles SET is_default = true WHERE id = %s AND account_id = %s",
        (vehicle_id, account_id(conn)),
    )


async def deactivate_vehicle(conn, vehicle_id: int) -> None:
    """Soft-delete: `active = false` drops the vehicle from `list_vehicles`'
    default (new-selection) picker while leaving it, and every trip still
    pointing at it, untouched -- trips keep their vehicle_name via the
    TRIP_COLUMNS subselect regardless of `active`.

    Also clears `is_default`: a retired vehicle staying flagged default
    would show up as the settings table's default while every active picker
    omits it, and would keep getting auto-assigned to newly detected trips.
    """
    await conn.execute(
        "UPDATE vehicles SET active = false, is_default = false WHERE id = %s AND account_id = %s",
        (vehicle_id, account_id(conn)),
    )


async def get_auto_assign_default_vehicle(conn) -> bool:
    cur = await conn.execute(
        "SELECT auto_assign_default_vehicle FROM account_settings WHERE account_id = %s",
        (account_id(conn),),
    )
    return (await cur.fetchone())[0]


async def set_auto_assign_default_vehicle(conn, enabled: bool) -> None:
    await conn.execute(
        "UPDATE account_settings SET auto_assign_default_vehicle = %s, updated_at = now() "
        "WHERE account_id = %s",
        (enabled, account_id(conn)),
    )
