"""Row-level security on every owned table, through the real runtime role.

These tests use direct, unscoped SQL on purpose: no WHERE account_id filter
helps them, so only the database policies can produce the isolation.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import pytest
from psycopg import errors, sql
from psycopg_pool import AsyncConnectionPool

from app.account_context import RUNTIME_ROLE, AccountPool
from app.application_roles import OWNED_TABLES, _load_state
from app.db import make_pool
from app.role_setup import role_conninfo
from conftest import add_test_account, reset_account_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

B_ID = 84
POINT = "ST_SetSRID(ST_MakePoint(1,1),4326)::geography"


async def _seed_every_owned_table(conn, owner: int) -> None:
    """Give `owner` at least one row in every owned table, as the superuser."""
    async def one(query, params):
        return (await (await conn.execute(query, params)).fetchone())[0]

    label = f"sweep-{owner}"
    device = await one("INSERT INTO tracking_devices(account_id,label) VALUES(%s,%s) RETURNING id", (owner, label))
    await conn.execute("INSERT INTO tracking_device_aliases(account_id,original_label,tracking_device_id) "
                       "VALUES(%s,%s,%s)", (owner, label, device))
    await conn.execute("INSERT INTO detector_state(account_id,tracking_device_id) VALUES(%s,%s)", (owner, device))
    await conn.execute("INSERT INTO ingest_credentials(public_id,basic_username,secret_hash,account_id,"
                       "tracking_device_id,kind) VALUES(%s,%s,'unused',%s,%s,'device')",
                       (label, label, owner, device))
    await conn.execute("INSERT INTO raw_messages(account_id,tracking_device_id,payload) VALUES(%s,%s,'{}')",
                       (owner, device))
    trip = await one("INSERT INTO trips(account_id,tracking_device_id,device,source,started_at,ended_at,"
                     "distance_m) VALUES(%s,%s,%s,'detected','2026-01-01T00:00Z','2026-01-01T01:00Z',1000) "
                     "RETURNING id", (owner, device, label))
    point = await one(f"INSERT INTO points(account_id,tracking_device_id,device,recorded_at,geom,trip_id) "
                      f"VALUES(%s,%s,%s,'2026-01-01T00:00Z',{POINT},%s) RETURNING id",
                      (owner, device, label, trip))
    await conn.execute(f"INSERT INTO stays(account_id,tracking_device_id,device,started_at,ended_at,centroid,"
                       f"point_count) VALUES(%s,%s,%s,'2026-01-01T00:00Z','2026-01-01T00:10Z',{POINT},1)",
                       (owner, device, label))
    await conn.execute("INSERT INTO trip_boundary_overrides(account_id,tracking_device_id,device,kind,point_id) "
                       "VALUES(%s,%s,%s,'force',%s)", (owner, device, label, point))
    place = await one(f"INSERT INTO places(account_id,name,geom) VALUES(%s,%s,{POINT}) RETURNING id", (owner, label))
    await conn.execute("INSERT INTO tag_rules(account_id,a_place,b_kind,category) VALUES(%s,%s,'home','business')",
                       (owner, place))
    await conn.execute("INSERT INTO geocode_cache(account_id,lat,lon) VALUES(%s,1,1)", (owner,))
    vehicle = await one("INSERT INTO vehicles(account_id,name) VALUES(%s,%s) RETURNING id", (owner, label))
    await conn.execute("INSERT INTO mileage_rates(account_id,year,rate_per_mi) VALUES(%s,2099,1)", (owner,))
    await conn.execute("INSERT INTO odometer_readings(account_id,vehicle_id,recorded_at,odometer_m) "
                       "VALUES(%s,%s,'2026-01-01T00:00Z',1000)", (owner, vehicle))
    await conn.execute("INSERT INTO expenses(account_id,vehicle_id,incurred_on,category,amount,treatment,trip_id) "
                       "VALUES(%s,%s,'2026-01-01','parking',1,'fully_business',%s)", (owner, vehicle, trip))
    await conn.execute("INSERT INTO nudge_delivery_windows(account_id,window_ends_at,trip_count) "
                       "VALUES(%s,'2026-01-01T00:00Z',0)", (owner,))
    await conn.execute("INSERT INTO odometer_reminder_windows(account_id,quarter_starts_at,reminded) "
                       "VALUES(%s,'2026-01-01T00:00Z',false)", (owner,))
    await conn.execute("INSERT INTO email_deliveries(account_id,kind,period_end,sent) "
                       "VALUES(%s,'weekly_nudge','2026-01-01T00:00Z',false)", (owner,))


async def _counts(admin, owner: int) -> dict[str, int]:
    async with admin.connection() as conn:
        return {table: (await (await conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE account_id=%s").format(sql.Identifier(table)), (owner,),
        )).fetchone())[0] for table in OWNED_TABLES}


async def _delete_order(admin) -> list[str]:
    """Owned tables with every referencing owned table first."""
    async with admin.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT conrelid::regclass::text,confrelid::regclass::text FROM pg_constraint "
            "WHERE contype='f' AND conrelid<>confrelid")
        edges = [(child, parent) for child, parent in await cur.fetchall()
                 if child in OWNED_TABLES and parent in OWNED_TABLES]
    order, remaining = [], set(OWNED_TABLES)
    while remaining:
        ready = sorted(t for t in remaining if not any(p == t and c in remaining for c, p in edges))
        assert ready, "owned tables have a foreign key cycle"
        order.extend(ready)
        remaining -= set(ready)
    return order


async def _row_json(admin, table: str, owner: int) -> str:
    async with admin.connection() as conn:
        return (await (await conn.execute(
            sql.SQL("SELECT row_to_json(t)::text FROM {} t WHERE account_id=%s LIMIT 1").format(
                sql.Identifier(table)), (owner,))).fetchone())[0]


async def _insert_copy(conn, table: str, row: str) -> None:
    """Insert a verbatim copy of another row; the policy check precedes constraints."""
    async with conn.transaction():
        await conn.execute(sql.SQL(
            "INSERT INTO {} OVERRIDING SYSTEM VALUE SELECT * FROM json_populate_record(NULL::{},%s::json)"
        ).format(sql.Identifier(table), sql.Identifier(table)), (row,))


@asynccontextmanager
async def _two_accounts():
    admin = make_pool(TEST_DB)
    await admin.open(wait=True)
    try:
        a = await reset_account_db(admin)
        b = await add_test_account(admin, B_ID)
        async with admin.connection() as conn:
            await _seed_every_owned_table(conn, a.principal.account_id)
            await _seed_every_owned_table(conn, B_ID)
        yield admin, a, b
    finally:
        await admin.close()


def test_every_owned_table_is_seeded_for_both_accounts():
    async def run():
        async with _two_accounts() as (admin, a, b):
            for owner in (a.principal.account_id, B_ID):
                counts = await _counts(admin, owner)
                assert set(counts) == set(OWNED_TABLES)
                assert [table for table, count in counts.items() if count == 0] == []
    asyncio.run(run())


def test_runtime_role_without_context_reads_and_writes_no_owned_rows():
    async def run():
        async with _two_accounts() as (admin, a, b):
            before = {owner: await _counts(admin, owner) for owner in (a.principal.account_id, B_ID)}
            rows = {table: await _row_json(admin, table, B_ID) for table in OWNED_TABLES}
            async with a.runtime_pool.connection() as conn:
                for table in OWNED_TABLES:
                    ident = sql.Identifier(table)
                    assert (await (await conn.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(ident))).fetchone())[0] == 0, table
                    assert (await conn.execute(
                        sql.SQL("UPDATE {} SET account_id=account_id").format(ident))).rowcount == 0, table
                    assert (await conn.execute(sql.SQL("DELETE FROM {}").format(ident))).rowcount == 0, table
                    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
                        await _insert_copy(conn, table, rows[table])
            after = {owner: await _counts(admin, owner) for owner in (a.principal.account_id, B_ID)}
            assert after == before
    asyncio.run(run())


def test_bound_runtime_role_sees_and_changes_only_its_own_rows():
    async def run():
        async with _two_accounts() as (admin, a, b):
            owner = a.principal.account_id
            mine, theirs = await _counts(admin, owner), await _counts(admin, B_ID)
            rows = {table: await _row_json(admin, table, B_ID) for table in OWNED_TABLES}
            async with a.connection() as conn:
                for table in OWNED_TABLES:
                    ident = sql.Identifier(table)
                    assert (await (await conn.execute(sql.SQL(
                        "SELECT count(*),count(*) FILTER (WHERE account_id<>%s) FROM {}").format(ident), (owner,),
                    )).fetchone()) == (mine[table], 0), table
                    assert (await conn.execute(
                        sql.SQL("UPDATE {} SET account_id=account_id").format(ident))).rowcount == mine[table], table
                    assert (await conn.execute(
                        sql.SQL("DELETE FROM {} WHERE account_id=%s").format(ident), (B_ID,))).rowcount == 0, table
                    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
                        await _insert_copy(conn, table, rows[table])
                    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
                        async with conn.transaction():
                            await conn.execute(sql.SQL("UPDATE {} SET account_id=%s").format(ident), (B_ID,))
            # Unfiltered deletes remove every row this account owns and nothing else.
            async with a.connection() as conn:
                for table in await _delete_order(admin):
                    deleted = (await conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))).rowcount
                    assert deleted == mine[table], table
            assert set((await _counts(admin, owner)).values()) == {0}
            assert await _counts(admin, B_ID) == theirs
    asyncio.run(run())


async def _single_connection_runtime(admin) -> AsyncConnectionPool:
    async with admin.connection() as conn:
        state = await _load_state(conn)
    pool = AsyncConnectionPool(role_conninfo(TEST_DB, state, RUNTIME_ROLE), min_size=1, max_size=1, open=False)
    await pool.open(wait=True)
    return pool


async def _unbound_view(pool) -> tuple:
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT pg_backend_pid(),NULLIF(current_setting('app.account_id',true),''),"
            "(SELECT count(*) FROM trips),(SELECT count(*) FROM vehicles)")).fetchone()


def test_context_never_survives_success_rollback_exception_or_cancellation():
    class Boom(Exception):
        pass

    async def run():
        async with _two_accounts() as (admin, a, b):
            runtime = await _single_connection_runtime(admin)
            try:
                bound = AccountPool(runtime, a.principal)
                pid = (await _unbound_view(runtime))[0]
                assert await _unbound_view(runtime) == (pid, None, 0, 0)

                async with bound.connection() as conn:
                    assert (await (await conn.execute("SELECT count(*) FROM trips")).fetchone())[0] == 1
                assert await _unbound_view(runtime) == (pid, None, 0, 0)

                async with bound.connection() as conn:
                    async with conn.transaction(force_rollback=True):
                        await conn.execute("SELECT set_config('app.account_id',%s,true)", (str(B_ID),))
                assert await _unbound_view(runtime) == (pid, None, 0, 0)

                with pytest.raises(Boom):
                    async with bound.connection() as conn:
                        await conn.execute("SELECT count(*) FROM trips")
                        raise Boom
                assert await _unbound_view(runtime) == (pid, None, 0, 0)

                with pytest.raises(errors.InsufficientPrivilege):
                    async with bound.connection() as conn:
                        await conn.execute("INSERT INTO vehicles(account_id,name) VALUES(%s,'x')", (B_ID,))
                assert await _unbound_view(runtime) == (pid, None, 0, 0)

                entered = asyncio.Event()

                async def hold():
                    async with bound.connection() as conn:
                        await conn.execute("SELECT count(*) FROM trips")
                        entered.set()
                        await asyncio.sleep(3600)

                task = asyncio.create_task(hold())
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                view = await _unbound_view(runtime)
                assert view[1:] == (None, 0, 0)
            finally:
                await runtime.close()
    asyncio.run(run())


def test_concurrent_borrowers_keep_their_own_context():
    async def run():
        async with _two_accounts() as (admin, a, b):
            a_ready, b_ready = asyncio.Event(), asyncio.Event()

            async def borrower(bound, mine, other, theirs):
                async with bound.connection() as conn:
                    mine.set()
                    await asyncio.wait_for(other.wait(), 5)
                    for _ in range(20):
                        row = await (await conn.execute(
                            "SELECT current_setting('app.account_id'),"
                            "(SELECT array_agg(DISTINCT account_id) FROM trips),"
                            "(SELECT array_agg(DISTINCT account_id) FROM tracking_devices)")).fetchone()
                        assert row == (str(bound.principal.account_id), [bound.principal.account_id],
                                       [bound.principal.account_id])
                        await asyncio.sleep(0)
                    pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
                    theirs.append(pid)

            pids = []
            await asyncio.gather(borrower(a, a_ready, b_ready, pids), borrower(b, b_ready, a_ready, pids))
            assert len(set(pids)) == 2
    asyncio.run(run())
