"""Restore the complete prepared application schema from a real PG16 archive."""
from __future__ import annotations

import asyncio
import hashlib

import psycopg
import pytest
from psycopg import sql

from app.account_context import AccountPool, AccountPrincipal
from app.application_roles import (
    TABLES, application_role_pools, finalize_application_restore,
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


async def _verify(database_url, first):
    async with application_role_pools(database_url) as roles:
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
        asyncio.run(_verify(target.database_url, first))
    finally:
        clusters.close()
