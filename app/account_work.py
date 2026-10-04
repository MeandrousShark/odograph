"""Cross-process leases for admitted external work that can outlive a caller."""
from contextlib import asynccontextmanager, nullcontext

from app.role_setup import _SafeConnection


@asynccontextmanager
async def _lease_connection(pool):
    from app.capacity import ManagedPool, current_owner
    owner = current_owner() if isinstance(pool, ManagedPool) else None
    if isinstance(pool, ManagedPool):
        from app.capacity import CapacityContractError
        if owner is None or owner.manager is not pool.capacity or not owner._lifetime.lease:
            raise CapacityContractError("independent serving connection has no lease owner")
        if owner._lifetime.lease_connection:
            raise CapacityContractError("nested independent lease connection")
        owner._lifetime.lease_connection = True
    try:
        async with await _SafeConnection.connect(pool.conninfo, connect_timeout=5, autocommit=True) as conn:
            yield conn
    finally:
        if owner is not None:
            owner._lifetime.lease_connection = False


@asynccontextmanager
async def external_account_work(pool, *account_ids: int):
    """Keep purge blocked until the task owning this context actually finishes."""
    if not account_ids or any(type(value) is not int or not 1 <= value <= 2**63 - 1 for value in account_ids):
        raise ValueError("external work requires account identities")
    from app.capacity import ManagedPool
    admission = pool.capacity.lease(account_ids) if isinstance(pool, ManagedPool) else nullcontext()
    async with admission:
        async with _lease_connection(pool) as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                await conn.execute("SET LOCAL statement_timeout = '15s'")
                for account_id in sorted(set(account_ids)):
                    await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (-account_id,))
                yield
