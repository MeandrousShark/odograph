"""Real PostgreSQL-16 archive recovery proof for the P0 fixture.

This test deliberately uses two (and, for the failed-restore assertion, three)
task-owned PostGIS containers. The archive is produced and consumed by the
client binaries in the pinned database image, so a host PostgreSQL client
version cannot make this proof accidentally green.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.account_context import (
    AccountPrincipal,
    MIGRATE_ROLE,
    account_connection,
    control_connection,
)
from app.role_setup import (
    ALL_ROLES,
    BOOTSTRAP_ROLE,
    FIXTURE_SCHEMA,
    RoleSetupError,
    SECURITY_CONTRACT_VERSION,
    create_p0_fixture,
    create_restore_role_identities,
    finalize_fixture_restore,
    managed_role_pools,
    prepare_fixture_roles,
)


pytestmark = pytest.mark.ops

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_SCRIPT = REPO_ROOT / "scripts" / "test_db.sh"
DB_OWNER = "mileage"
DB_OWNER_PASSWORD = "testpw"
INTERNAL_SCHEMA = "odograph_internal"
MANAGED_STATE_TABLE = "managed_role_state"


@dataclass(frozen=True)
class _Cluster:
    task_id: str
    container_id: str
    database_url: str


def _run(command: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=input_bytes,
        capture_output=True,
        timeout=180,
        check=False,
    )


def _start_cluster(task_id: str) -> _Cluster:
    try:
        result = _run(["bash", str(TEST_DB_SCRIPT), "start", task_id])
    except FileNotFoundError as error:
        pytest.fail(f"disposable Postgres helper unavailable: {error}")
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"task-owned Postgres fixture timed out while starting: {error}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        pytest.fail(f"task-owned Postgres fixture failed to start: {detail}")

    output = result.stdout.decode(errors="replace")
    match = re.search(r"postgresql://mileage:testpw@127\.0\.0\.1:(\d+)/mileage", output)
    if not match:
        pytest.fail(f"test_db.sh returned no database URL: {output!r}")

    try:
        containers = _run(
            [
                "podman",
                "ps",
                "-q",
                "--filter",
                "label=io.odograph.test-db=1",
                "--filter",
                f"label=io.odograph.test-db-task={task_id}",
            ]
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        pytest.fail(f"podman disappeared after database start: {error}")
    if containers.returncode != 0:
        pytest.fail(containers.stderr.decode(errors="replace"))
    container_ids = [line for line in containers.stdout.decode().splitlines() if line]
    if len(container_ids) != 1:
        pytest.fail(f"expected one task-owned test database, found {container_ids!r}")
    return _Cluster(task_id, container_ids[0], match.group(0))


def _cleanup_cluster(task_id: str) -> subprocess.CompletedProcess:
    return _run(["bash", str(TEST_DB_SCRIPT), "cleanup", task_id])


class _Clusters:
    def __init__(self) -> None:
        self._task_ids: list[str] = []
        self._podman_checked = False

    def start(self) -> _Cluster:
        if not self._podman_checked:
            try:
                preflight = _run(["podman", "info"])
            except FileNotFoundError as error:
                pytest.skip(f"podman unavailable: {error}")
            if preflight.returncode != 0:
                detail = preflight.stderr.decode(errors="replace").strip()
                pytest.skip(f"podman unavailable: {detail}")
        self._podman_checked = True
        task_id = f"p0-archive-{os.getpid()}-{secrets.token_hex(4)}"
        self._task_ids.append(task_id)
        return _start_cluster(task_id)

    def close(self) -> None:
        failures = []
        for task_id in reversed(self._task_ids):
            result = _cleanup_cluster(task_id)
            if result.returncode != 0:
                failures.append(result.stderr.decode(errors="replace"))
        if failures:
            pytest.fail("task-owned Postgres cleanup failed: " + "\n".join(failures))


def _role_password(conninfo: str) -> str:
    values = conninfo_to_dict(conninfo)
    return values.get("password", "")


def _assert_pg16_clients(cluster: _Cluster) -> None:
    for tool in ("pg_dump", "pg_restore"):
        result = _run(["podman", "exec", cluster.container_id, tool, "--version"])
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        version = result.stdout.decode(errors="replace").strip()
        assert re.search(r"\bPostgreSQL\) 16\.", version), version


def _dump_archive(
    cluster: _Cluster,
    *,
    user: str,
    password: str,
    archive: Path,
    role: str | None = None,
) -> subprocess.CompletedProcess:
    dsn = make_conninfo(
        host="127.0.0.1", port=5432, dbname="mileage", user=user
    )
    role_args = ["--role", role] if role else []
    with archive.open("wb") as output:
        return subprocess.run(
            [
                "podman",
                "exec",
                "--env",
                "PGPASSWORD",
                cluster.container_id,
                "pg_dump",
                *role_args,
                "--format=custom",
                "--dbname",
                dsn,
            ],
            stdout=output,
            stderr=subprocess.PIPE,
            env={**os.environ, "PGPASSWORD": password},
            timeout=180,
            check=False,
        )


def _restore_archive(
    cluster: _Cluster, archive: Path, *, check: bool = False
) -> subprocess.CompletedProcess:
    dsn = make_conninfo(
        host="127.0.0.1", port=5432, dbname="mileage", user=DB_OWNER
    )
    with archive.open("rb") as source:
        return subprocess.run(
            [
                "podman",
                "exec",
                "--env",
                "PGPASSWORD",
                "--interactive",
                cluster.container_id,
                "pg_restore",
                "--dbname",
                dsn,
                "--single-transaction",
                "--exit-on-error",
            ],
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PGPASSWORD": DB_OWNER_PASSWORD},
            timeout=180,
            check=check,
        )


def _safe_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()
    return [
        row[0]
        for row in rows
        if not re.search(r"password|secret|token|credential", row[0], re.I)
    ]


def _database_snapshot(database_url: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    with psycopg.connect(database_url) as conn:
        tables = conn.execute(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN (%s, %s) AND c.relkind IN ('r', 'p') "
            "ORDER BY n.nspname, c.relname",
            (FIXTURE_SCHEMA, INTERNAL_SCHEMA),
        ).fetchall()
        for schema, table in tables:
            columns = _safe_columns(conn, schema, table)
            if columns:
                quoted = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
                query = sql.SQL("SELECT {} FROM {}.{} ").format(
                    quoted, sql.Identifier(schema), sql.Identifier(table)
                )
                rows = conn.execute(query).fetchall()
            else:
                rows = conn.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                ).fetchall()
            snapshot[f"table:{schema}.{table}"] = (
                tuple(columns),
                sorted(rows, key=repr),
            )

        sequence_names = conn.execute(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN (%s, %s) AND c.relkind = 'S' "
            "ORDER BY n.nspname, c.relname",
            (FIXTURE_SCHEMA, INTERNAL_SCHEMA),
        ).fetchall()
        snapshot["sequences"] = [
            (schema, name, *conn.execute(
                sql.SQL("SELECT last_value, is_called FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(name)
                )
            ).fetchone())
            for schema, name in sequence_names
        ]
        snapshot["objects"] = conn.execute(
            "SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner), "
            "coalesce(c.relacl::text, '') FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN (%s, %s) ORDER BY n.nspname, c.relname",
            (FIXTURE_SCHEMA, INTERNAL_SCHEMA),
        ).fetchall()
        snapshot["policies"] = conn.execute(
            "SELECT schemaname, tablename, policyname, permissive, roles::text, "
            "cmd, coalesce(qual, ''), coalesce(with_check, '') FROM pg_policies "
            "WHERE schemaname IN (%s, %s) ORDER BY schemaname, tablename, policyname",
            (FIXTURE_SCHEMA, INTERNAL_SCHEMA),
        ).fetchall()
        snapshot["roles"] = conn.execute(
            "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, "
            "rolcanlogin, rolreplication, rolbypassrls FROM pg_roles "
            "WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(ALL_ROLES),),
        ).fetchall()
        snapshot["memberships"] = conn.execute(
            "SELECT parent.rolname, member.rolname, m.admin_option "
            "FROM pg_auth_members m JOIN pg_roles parent ON parent.oid = m.roleid "
            "JOIN pg_roles member ON member.oid = m.member "
            "WHERE parent.rolname = ANY(%s) OR member.rolname = ANY(%s) "
            "ORDER BY parent.rolname, member.rolname",
            (list(ALL_ROLES), list(ALL_ROLES)),
        ).fetchall()
    return snapshot


def _protected_metadata(database_url: str) -> list[tuple]:
    with psycopg.connect(database_url) as conn:
        columns = _safe_columns(conn, INTERNAL_SCHEMA, MANAGED_STATE_TABLE)
        quoted = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
        return conn.execute(
            sql.SQL("SELECT {} FROM {}.{} ORDER BY 1").format(
                quoted, sql.Identifier(INTERNAL_SCHEMA), sql.Identifier(MANAGED_STATE_TABLE)
            )
        ).fetchall()


def _add_restore_drift(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            sql.SQL(
                "CREATE POLICY p0_archive_permissive_drift ON {}.{} "
                "AS PERMISSIVE FOR SELECT TO odograph_runtime USING (true)"
            ).format(sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("ledger"))
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON {}.{} TO odograph_control").format(
                sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("ledger")
            )
        )


async def _managed_runtime_conninfo(database_url: str) -> str:
    async with managed_role_pools(database_url) as pools:
        return pools.runtime.conninfo


def _seed_fixture_data(database_url: str) -> None:
    """Put representative A/B rows in the otherwise schema-only fixture."""
    with psycopg.connect(database_url) as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.{} (id,email,is_enabled,auth_version,is_admin) "
                "VALUES (1,'a@example.test',true,1,true),(2,'b@example.test',true,3,false)"
            ).format(sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("accounts"))
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.{} (account_id,id,name) VALUES "
                "(1,10,'A car'),(2,20,'B car')"
            ).format(sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("vehicles"))
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.{} (account_id,id,vehicle_id,note) VALUES "
                "(1,100,10,'A note'),(2,200,20,'B note')"
            ).format(sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("ledger"))
        )
        conn.execute(
            sql.SQL(
                "UPDATE {}.{} SET first_account_id=1, completed_at=clock_timestamp() "
                "WHERE id=1"
            ).format(sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("bootstrap_state"))
        )
        for sequence, value in (
            ("accounts_id_seq", 2),
            ("vehicles_id_seq", 20),
            ("ledger_id_seq", 200),
        ):
            conn.execute(
                "SELECT setval(%s::regclass,%s,true)",
                (f"{FIXTURE_SCHEMA}.{sequence}", value),
            )


def _protected_secret_fingerprint(database_url: str) -> str:
    """Compare managed secrets without selecting either plaintext value."""
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            sql.SQL(
                "SELECT md5(runtime_password || ':' || control_password) "
                "FROM {}.{} WHERE id=1"
            ).format(
                sql.Identifier(INTERNAL_SCHEMA), sql.Identifier(MANAGED_STATE_TABLE)
            )
        ).fetchone()[0]


async def _assert_restricted_isolation(database_url: str) -> None:
    async with managed_role_pools(database_url) as pools:
        async with account_connection(
            pools.runtime, AccountPrincipal(account_id=1, enabled=True, auth_version=1)
        ) as conn:
            rows = await (
                await conn.execute(
                    sql.SQL("SELECT account_id, note FROM {}.{} ORDER BY account_id").format(
                        sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("ledger")
                    )
                )
            ).fetchall()
            assert rows == [(1, "A note")]

        async with account_connection(
            pools.runtime, AccountPrincipal(account_id=2, enabled=True, auth_version=3)
        ) as conn:
            rows = await (
                await conn.execute(
                    sql.SQL("SELECT account_id, note FROM {}.{} ORDER BY account_id").format(
                        sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("ledger")
                    )
                )
            ).fetchall()
            assert rows == [(2, "B note")]

        async with control_connection(pools.control) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(
                            sql.Identifier(FIXTURE_SCHEMA), sql.Identifier("ledger")
                        )
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(
                            sql.Identifier(INTERNAL_SCHEMA),
                            sql.Identifier(MANAGED_STATE_TABLE),
                        )
                    )


def test_real_pg16_archive_reconstructs_roles_grants_sequences_and_isolation(tmp_path):
    """A complete archive is recoverable without retaining source passwords."""
    clusters = _Clusters()
    try:
        source = clusters.start()
        failed_target = clusters.start()
        target = clusters.start()
        _assert_pg16_clients(source)

        asyncio.run(create_p0_fixture(source.database_url))
        asyncio.run(prepare_fixture_roles(source.database_url))
        _seed_fixture_data(source.database_url)
        source_snapshot = _database_snapshot(source.database_url)
        source_metadata = _protected_metadata(source.database_url)
        source_secret_fingerprint = _protected_secret_fingerprint(source.database_url)
        runtime_info = asyncio.run(_managed_runtime_conninfo(source.database_url))
        runtime_password = _role_password(runtime_info)
        assert SECURITY_CONTRACT_VERSION
        assert MIGRATE_ROLE in ALL_ROLES and BOOTSTRAP_ROLE in ALL_ROLES

        archive = tmp_path / "p0-fixture.dump"
        backup = _dump_archive(
            source,
            user=DB_OWNER,
            password=DB_OWNER_PASSWORD,
            archive=archive,
        )
        assert backup.returncode == 0, backup.stderr.decode(errors="replace")
        assert archive.stat().st_size > 128

        owner_attempt = _dump_archive(
            source,
            user=DB_OWNER,
            password=DB_OWNER_PASSWORD,
            archive=tmp_path / "owner.dump",
            role=MIGRATE_ROLE,
        )
        assert owner_attempt.returncode != 0
        assert owner_attempt.stderr

        # Runtime credentials can connect, but that identity is visibly not a
        # full-backup identity and pg_dump must fail rather than emit a partial
        # archive that looks valid.
        restricted_archive = tmp_path / "restricted.dump"
        restricted = _dump_archive(
            source,
            user="odograph_runtime",
            password=runtime_password,
            archive=restricted_archive,
        )
        assert restricted.returncode != 0
        assert restricted.stderr
        assert runtime_password.encode() not in restricted.stderr
        del runtime_password, runtime_info

        # Role identities exist before restore, so policy role names and object
        # owners in the custom archive resolve during pg_restore.
        asyncio.run(
            create_restore_role_identities(
                failed_target.database_url, owner_role=MIGRATE_ROLE
            )
        )
        bad_archive = tmp_path / "broken.dump"
        bad_archive.write_bytes(b"not a PostgreSQL custom archive")
        failed = _restore_archive(failed_target, bad_archive)
        assert failed.returncode != 0
        with pytest.raises(RoleSetupError):
            asyncio.run(_managed_runtime_conninfo(failed_target.database_url))

        asyncio.run(
            create_restore_role_identities(target.database_url, owner_role=MIGRATE_ROLE)
        )
        restored = _restore_archive(target, archive)
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")

        _add_restore_drift(target.database_url)
        asyncio.run(finalize_fixture_restore(target.database_url))
        target_snapshot = _database_snapshot(target.database_url)
        target_metadata = _protected_metadata(target.database_url)
        assert _protected_secret_fingerprint(target.database_url) == source_secret_fingerprint
        assert target_snapshot == source_snapshot
        assert target_metadata == source_metadata

        asyncio.run(_assert_restricted_isolation(target.database_url))
    finally:
        clusters.close()
