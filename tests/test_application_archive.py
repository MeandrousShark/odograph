"""Restore the complete activated application schema from a real PG16 archive."""
from __future__ import annotations

import asyncio
import hashlib

import psycopg
import pytest
from psycopg import sql

from app.account_context import AccountPool, AccountPrincipal
from app.application_roles import (
    OWNED_TABLES, PROTECTED_TABLES, TABLES, _policy_contract, application_role_pools, finalize_application_restore,
    prepare_application_restore,
)
from app.db import make_pool, run_migrations
from app.portable.export import _fetch_export_trips
from test_account_context_archive import (
    DB_OWNER, DB_OWNER_PASSWORD, _Clusters, _assert_pg16_clients,
    _dump_archive, _restore_archive, _role_password,
)

pytestmark = pytest.mark.ops


def _snapshot(database_url):
    """Compare all rows and sequence states without exposing credential values."""
    with psycopg.connect(database_url) as conn:
        rows = []
        for schema, table in [("public", table) for table in TABLES] + [
            ("odograph_service", "managed_role_state"),
            ("odograph_service", "recovery_metadata"),
        ]:
            data = conn.execute(sql.SQL("SELECT * FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table))).fetchall()
            rows.append((schema, table, sorted(data, key=repr)))
        for (sequence,) in conn.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"
        ).fetchall():
            rows.append((sequence, conn.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(
                sql.Identifier("public", sequence))).fetchone()))
        return hashlib.sha256(repr(rows).encode()).hexdigest()


async def _seed(database_url):
    from app.accounts import create_admin
    from app.tracking import create_device
    pool = make_pool(database_url)
    await pool.open(wait=True)
    try:
        await run_migrations(pool)
        async with application_role_pools(database_url) as roles:
            async with roles.control.connection() as conn:
                first = await create_admin(conn, "a@example.invalid", "synthetic-hash")
            # Test-only multi-account fixture. Shipping migrations retain both
            # the singleton and admin-only constraints.
            async with pool.connection() as conn:
                await conn.execute("DROP INDEX accounts_singleton_idx")
                await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) VALUES(73,'b@example.invalid','synthetic-hash',true)")
                await conn.execute("INSERT INTO account_settings(account_id) VALUES(73)")
            for owner in (first["id"], 73):
                bound = AccountPool(roles.runtime, AccountPrincipal(owner, True, 1))
                async with bound.connection() as conn:
                    await create_device(conn, "same-label")
                    await conn.execute(
                        "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m,category) "
                        "VALUES(%s,'portable provenance','manual','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',%s,'business')",
                        (owner, owner * 100),
                    )
            return first["id"], roles.runtime.conninfo
    finally:
        await pool.close()


def _row_security(database_url):
    """RLS flags on every table and every policy, as the catalog reports them."""
    with psycopg.connect(database_url) as conn:
        flags = conn.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relname=ANY(%s) ORDER BY relname",
            (list(TABLES),)).fetchall()
        policies = conn.execute(
            "SELECT tablename,policyname,roles::text,cmd,qual,with_check FROM pg_policies "
            "WHERE schemaname='public' ORDER BY 1,2").fetchall()
        return flags, policies


async def _verify(database_url, first):
    async with application_role_pools(database_url) as roles:
        # Unscoped SQL on the runtime role: only the restored policies isolate.
        async with roles.runtime.connection() as conn:
            for table in OWNED_TABLES:
                count = (await (await conn.execute(sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(table)))).fetchone())[0]
                assert count == 0, table
        for owner, other in ((first, 73), (73, first)):
            bound = AccountPool(roles.runtime, AccountPrincipal(owner, True, 1))
            async with bound.connection() as conn:
                owners = await (await conn.execute(
                    "SELECT (SELECT array_agg(DISTINCT account_id) FROM trips),"
                    "(SELECT array_agg(DISTINCT account_id) FROM tracking_devices),"
                    "(SELECT array_agg(DISTINCT account_id) FROM ingest_credentials)")).fetchone()
                assert owners == ([owner], [owner], [owner])
                assert (await conn.execute("UPDATE trips SET notes='cross' WHERE account_id=%s",
                                           (other,))).rowcount == 0
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO trips(account_id,device,source,started_at,ended_at,distance_m) "
                            "VALUES(%s,'cross','manual','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',1)",
                            (other,))
        for owner in (first, 73):
            bound = AccountPool(roles.runtime, AccountPrincipal(owner, True, 1))
            async with bound.connection() as conn:
                trips = await _fetch_export_trips(conn)
            assert len(trips) == 1
            assert trips[0]["distance_m"] == owner * 100
        async with roles.control.connection() as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT * FROM trips")


def test_application_archive_preserves_rows_credentials_sequences_and_restricted_sessions(tmp_path):
    clusters = _Clusters()
    try:
        source, target = clusters.start(), clusters.start()
        _assert_pg16_clients(source)
        first, runtime_info = asyncio.run(_seed(source.database_url))
        before = _snapshot(source.database_url)
        security_before = _row_security(source.database_url)
        assert [row[0] for row in security_before[0] if row[1:] == (True, True)] == sorted(OWNED_TABLES + PROTECTED_TABLES)
        assert {row[:2] for row in security_before[1]} == set(_policy_contract())
        archive = tmp_path / "application.dump"
        result = _dump_archive(source, user=DB_OWNER, password=DB_OWNER_PASSWORD, archive=archive)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        restricted = _dump_archive(source, user="odograph_runtime", password=_role_password(runtime_info),
                                   archive=tmp_path / "restricted.dump")
        assert restricted.returncode != 0
        del runtime_info
        asyncio.run(prepare_application_restore(target.database_url))
        restored = _restore_archive(target, archive)
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")
        asyncio.run(finalize_application_restore(target.database_url))
        assert _snapshot(target.database_url) == before
        assert _row_security(target.database_url) == security_before
        asyncio.run(_verify(target.database_url, first))
    finally:
        clusters.close()
