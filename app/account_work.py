"""Cross-process leases for admitted external work that can outlive a caller."""
import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext

from psycopg import InterfaceError, OperationalError

from app.account_context import AccountPrincipal
from app.role_setup import _SafeConnection

log = logging.getLogger(__name__)


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


@asynccontextmanager
async def report_account_work(pool, principal: AccountPrincipal):
    """Keep purge excluded while report snapshots close before file transmission."""
    if not isinstance(principal, AccountPrincipal) or not principal.enabled:
        raise ValueError("report lease requires a validated enabled principal")
    from app.capacity import CapacityContractError, ManagedPool, current_owner
    managed = isinstance(pool, ManagedPool)
    if managed:
        owner = current_owner()
        if owner is None or owner.manager is not pool.capacity or owner.lane != 'foreground' or owner.principal != principal:
            raise CapacityContractError('report lease does not match foreground ownership')
    admission = pool.capacity.lease((principal.account_id,)) if managed else nullcontext()
    async with admission:
        try:
            async with _lease_connection(pool) as conn:
                await conn.execute("SET lock_timeout = '5s'")
                await conn.execute("SET statement_timeout = '15s'")
                entered = False
                try:
                    await conn.execute("SELECT pg_advisory_lock_shared(%s)", (-principal.account_id,))
                    entered = True
                    yield conn
                finally:
                    try:
                        cur = await conn.execute("SELECT pg_advisory_unlock_shared(%s)", (-principal.account_id,))
                        unlocked = (await cur.fetchone())[0]
                    except BaseException as exc:
                        log.error('report lease cleanup unconfirmed (%s)', type(exc).__name__)
                        # Closing a client socket does not acknowledge server unlock.
                        # Preserve the serving owner and lease until process recovery.
                        while True:
                            try:
                                await asyncio.Future()
                            except asyncio.CancelledError:
                                continue
                    if entered and not unlocked:
                        raise RuntimeError('report lease was not held at cleanup')
        except (OperationalError, InterfaceError) as exc:
            if exc.sqlstate is not None:
                raise
            log.error('report lease backend cleanup unconfirmed (%s)', type(exc).__name__)
            while True:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    continue
