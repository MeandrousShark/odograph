"""Admission checks for results computed outside an account transaction."""
from app.account_context import account_id


async def lock_device_generation(conn, device_id: int, generation: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM tracking_devices WHERE account_id=%s AND id=%s "
        "AND generation=%s AND enabled AND revoked_at IS NULL FOR SHARE",
        (account_id(conn), device_id, generation),
    )
    return await cur.fetchone() is not None
