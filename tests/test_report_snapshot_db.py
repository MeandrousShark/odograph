"""Shared report snapshots preserve role isolation and the purge barrier."""
import asyncio
import os

from psycopg import errors
from psycopg.pq import TransactionStatus
import pytest

from app.account_context import control_connection
from app.account_work import report_account_work
from test_capacity_db import _scenario

pytestmark = [pytest.mark.db, pytest.mark.capacity_contract,
              pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires disposable PostGIS")]


def test_report_snapshot_uses_existing_lease_without_holding_identity_lane():
    async def run():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            first, second = accounts
            owner = first.principal.account_id
            async with manager.operation('foreground', first.principal):
                async with report_account_work(control, first.principal) as metadata:
                    assert (await (await metadata.execute('SELECT current_user')).fetchone())[0] == 'odograph_control'
                    async with metadata.transaction():
                        await metadata.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                        snapshot = (await (await metadata.execute('SELECT pg_export_snapshot()')).fetchone())[0]
                        async with raw.connection() as writer:
                            original = (await (await writer.execute('SELECT name FROM vehicles WHERE account_id=%s', (owner,))).fetchone())[0]
                            await writer.execute("UPDATE vehicles SET name='new vehicle name' WHERE account_id=%s", (owner,))
                        async with first.connection(snapshot_id=snapshot) as conn:
                            rows = await (await conn.execute('SELECT account_id,name FROM vehicles ORDER BY id')).fetchall()
                            assert rows == [(owner, original)]
                            with pytest.raises(errors.InsufficientPrivilege):
                                async with conn.transaction():
                                    await conn.execute('SELECT email FROM accounts')
                        with pytest.raises(errors.InsufficientPrivilege):
                            async with metadata.transaction():
                                await metadata.execute('SELECT name FROM vehicles')
                        async with control_connection(control) as identity:
                            assert (await (await identity.execute('SELECT count(*) FROM accounts')).fetchone())[0] == 2
                        assert manager.snapshot()['identity'] == {'active': 0, 'pending': 0}
                    assert metadata.info.transaction_status == TransactionStatus.IDLE
                    assert manager.snapshot()['leases'] == 1
                    async with raw.connection() as purge:
                        assert (await (await purge.execute('SELECT pg_try_advisory_xact_lock(%s)', (-owner,))).fetchone())[0] is False
                assert manager.snapshot()['leases'] == 0
                async with raw.connection() as purge:
                    assert (await (await purge.execute('SELECT pg_try_advisory_xact_lock(%s)', (-owner,))).fetchone())[0] is True
                async with first.connection() as conn:
                    assert (await (await conn.execute('SELECT name FROM vehicles')).fetchone())[0] == 'new vehicle name'
            async with second.connection() as conn:
                assert (await (await conn.execute('SELECT DISTINCT account_id FROM vehicles')).fetchall()) == [(second.principal.account_id,)]
    asyncio.run(run())


def test_disable_between_snapshot_export_and_runtime_guard_never_exposes_ledger():
    async def run():
        async with _scenario() as (raw, manager, runtime, control, accounts):
            first, _ = accounts
            async with manager.operation('foreground', first.principal):
                async with report_account_work(control, first.principal) as metadata:
                    async with metadata.transaction():
                        await metadata.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                        snapshot = (await (await metadata.execute('SELECT pg_export_snapshot()')).fetchone())[0]
                        async with raw.connection() as writer:
                            await writer.execute('UPDATE accounts SET is_enabled=false WHERE id=%s', (first.principal.account_id,))
                        with pytest.raises(errors.SerializationFailure):
                            async with first.connection(snapshot_id=snapshot):
                                raise AssertionError('disabled report principal reached ledger work')
                    assert metadata.info.transaction_status == TransactionStatus.IDLE
                assert manager.snapshot()['leases'] == 0
    asyncio.run(run())
