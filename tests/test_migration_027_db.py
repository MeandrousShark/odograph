"""Migration 027 deletes stored OwnTracks configuration dumps, nothing else.

Releases before the ingest allowlist stored every authenticated payload in
raw_messages, including "dump" and "configuration" messages that carry the
tracker's username, password and URL.
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from psycopg.types.json import Jsonb

import app.db as db_module
from app.accounts import create_admin
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from conftest import drop_and_recreate_schema, full_schema_reset

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")
SQL_DIR = MIGRATIONS_DIR.parent / "scripts" / "sql"

KEPT = [
    {"_type": "location", "lat": 35.0, "lon": 139.0, "tst": 1_790_000_000},
    {"_type": "transition", "event": "enter", "desc": "Office"},
    {"_type": "waypoint", "desc": "Office", "rad": 50},
    {"_type": "waypoints", "waypoints": []},
    {"_type": "cmd", "action": "reportLocation"},
    {"_type": 7},
    {"lat": 35.0, "lon": 139.0},
]
DELETED = [
    {"_type": "dump", "configuration": {"username": "u", "password": "p", "url": "https://example.invalid"}},
    {"_type": "configuration", "username": "u", "password": "p"},
]


async def _migrate_through_026_with_messages(pool, tmp_path, monkeypatch):
    through_026 = tmp_path / "through_026"
    through_026.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name.split("_", 1)[0]) <= 26:
            shutil.copy(path, through_026 / path.name)
    await drop_and_recreate_schema(pool)
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", through_026)
    await run_migrations(pool)
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
    async with pool.connection() as conn:
        await conn.execute((SQL_DIR / "account_bootstrap.sql").read_text())
        account = await create_admin(conn, "owner@example.invalid", "unused-test-hash")
        for payload in KEPT + DELETED:
            await conn.execute(
                "INSERT INTO raw_messages(account_id, payload) VALUES (%s, %s)",
                (account["id"], Jsonb(payload)))
        cur = await conn.execute("SELECT id, payload FROM raw_messages ORDER BY id")
        return await cur.fetchall()


def test_027_deletes_only_dump_and_configuration_rows(tmp_path, monkeypatch):
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            before = await _migrate_through_026_with_messages(pool, tmp_path, monkeypatch)
            await run_migrations(pool)
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT id, payload FROM raw_messages ORDER BY id")
                after = await cur.fetchall()
                cur = await conn.execute("SELECT max(version) FROM schema_migrations")
                assert (await cur.fetchone())[0] == 33
        finally:
            await full_schema_reset(pool)
            await pool.close()
        assert after == [row for row in before if row[1] in KEPT]
        assert [row[1] for row in after] == KEPT

    asyncio.run(run())
