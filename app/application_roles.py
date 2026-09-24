"""Managed identities and the activated ownership security contract.

The isolated P0 fixture keeps its own validator. This module validates the
live schema exactly: every account-owned table has its account policies with
row-level security enabled and forced, while control and reference tables
have no row-level security.
"""
from __future__ import annotations

import secrets
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from app.account_context import CONTROL_ROLE, MIGRATE_ROLE, RUNTIME_ROLE, check_runtime_privileges
from app.db import ROLE_SETUP_ADVISORY_LOCK_KEY
from app.role_setup import (
    ALL_ROLES, BOOTSTRAP_ROLE, ManagedRoleState, RolePools, RoleSetupError,
    _SafeConnection, _identities, _policy_expression, role_conninfo,
)

CONTRACT_VERSION = "ownership-activated-v1"
STATE_SCHEMA = "odograph_service"
OWNED_TABLES = (
    "raw_messages", "points", "stays", "trips", "detector_state", "places",
    "tag_rules", "geocode_cache", "trip_boundary_overrides", "vehicles",
    "mileage_rates", "odometer_readings", "expenses", "nudge_delivery_windows",
    "odometer_reminder_windows", "email_deliveries", "account_settings",
    "tracking_devices", "tracking_device_aliases", "ingest_credentials",
)
CONTROL_TABLES = ("accounts", "oidc_identities", "instance_state")
REFERENCE_TABLES = ("schema_migrations", "reference_mileage_rates")
TABLES = OWNED_TABLES + CONTROL_TABLES + REFERENCE_TABLES
CREDENTIAL_COLUMNS = (
    "public_id", "basic_username", "account_id", "tracking_device_id", "kind",
    "generation", "revoked_at", "created_at", "updated_at",
)
COLUMN_SELECT = {
    (RUNTIME_ROLE, "ingest_credentials"): CREDENTIAL_COLUMNS,
    (CONTROL_ROLE, "ingest_credentials"): (
        "public_id", "basic_username", "secret_hash", "account_id", "tracking_device_id",
        "kind", "generation", "revoked_at",
    ),
    (CONTROL_ROLE, "tracking_devices"): ("id", "account_id", "enabled", "generation", "revoked_at"),
}
BOOTSTRAP_INSERT_TABLES = ("accounts", "account_settings", "vehicles", "tag_rules", "mileage_rates")
BOOTSTRAP_LOCK_TABLES = ("accounts", "ingest_credentials", "tracking_devices", "tracking_device_aliases")
SQL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "sql"
FUNCTION_FILES = ("account_bootstrap.sql", "account_admission.sql", "tracking_admission.sql")
FUNCTIONS = {
    "public.bootstrap_first_account(text,text,text)": CONTROL_ROLE,
    "public.assert_account_active(bigint,bigint)": RUNTIME_ROLE,
    "public.assert_tracking_credential(text,bigint,bigint,bigint,text)": RUNTIME_ROLE,
}
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _require(ok: bool) -> None:
    if not ok:
        raise RoleSetupError("application database security contract mismatch")


class _RolesUsedElsewhere(RoleSetupError):
    pass


class _ContractMismatch(RoleSetupError):
    """A named security-contract drift. The message is always safe to log."""


def _require_contract(ok: bool, cause: str) -> None:
    if not ok:
        raise _ContractMismatch(f"application database security contract mismatch: {cause}")


async def _check_role_database_ownership(conn) -> None:
    """Fixed cluster identities may belong to only this installation."""
    cur = await conn.execute(
        "WITH managed AS (SELECT oid FROM pg_roles WHERE rolname=ANY(%s)), "
        "here AS (SELECT oid FROM pg_database WHERE datname=current_database()) "
        "SELECT EXISTS (SELECT 1 FROM pg_shdepend d "
        "WHERE d.refclassid='pg_authid'::regclass AND d.refobjid IN (SELECT oid FROM managed) "
        "AND d.dbid<>0 AND d.dbid<>(SELECT oid FROM here)) "
        "OR EXISTS (SELECT 1 FROM pg_database d WHERE d.oid<>(SELECT oid FROM here) "
        "AND (d.datdba IN (SELECT oid FROM managed) OR EXISTS "
        "(SELECT 1 FROM aclexplode(d.datacl) a WHERE a.grantee IN (SELECT oid FROM managed) "
        "OR a.grantor IN (SELECT oid FROM managed)))) "
        "OR EXISTS (SELECT 1 FROM pg_db_role_setting s WHERE s.setdatabase<>0 "
        "AND s.setdatabase<>(SELECT oid FROM here) AND s.setrole IN (SELECT oid FROM managed))",
        (list(ALL_ROLES),),
    )
    if (await cur.fetchone())[0]:
        raise _RolesUsedElsewhere(
            "managed Odograph roles are used by another database; use a separate PostgreSQL cluster"
        )


def _table_rights(role: str, table: str) -> set[str]:
    if role == RUNTIME_ROLE:
        if table in OWNED_TABLES:
            return {"INSERT", "UPDATE", "DELETE"} | ({"SELECT"} if table != "ingest_credentials" else set())
        if table in REFERENCE_TABLES:
            return {"SELECT"}
    if role == CONTROL_ROLE:
        if table == "accounts":
            return {"SELECT", "UPDATE"}
        if table == "oidc_identities":
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table in ("instance_state", "schema_migrations"):
            return {"SELECT"}
    if role == BOOTSTRAP_ROLE:
        rights = set()
        if table in BOOTSTRAP_INSERT_TABLES:
            rights.add("INSERT")
        if table in BOOTSTRAP_LOCK_TABLES or table == "instance_state":
            rights.update(("SELECT", "UPDATE"))
        if table == "reference_mileage_rates":
            rights.add("SELECT")
        return rights
    return set()


def _policy_contract() -> dict[tuple[str, str], tuple[str, str, str | None, str | None]]:
    expression = "account_id = NULLIF(current_setting('app.account_id'::text,true),''::text)::bigint"
    policies = {(table, "account_isolation"): (RUNTIME_ROLE, "ALL", expression, expression)
                for table in OWNED_TABLES}
    for table in ("ingest_credentials", "tracking_devices"):
        policies[table, "control_lookup"] = (CONTROL_ROLE, "SELECT", "true", None)
    for table in OWNED_TABLES:
        if _table_rights(BOOTSTRAP_ROLE, table):
            policies[table, "bootstrap_defaults"] = (BOOTSTRAP_ROLE, "ALL", "true", "true")
    return policies


async def _sequences(conn):
    cur = await conn.execute(
        "SELECT t.relname,s.relname FROM pg_class s JOIN pg_depend d ON d.objid=s.oid "
        "JOIN pg_class t ON t.oid=d.refobjid JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE s.relkind='S' AND n.nspname='public' AND t.relname=ANY(%s) "
        "AND d.deptype IN ('a','i')", (list(TABLES),))
    return await cur.fetchall()


async def _load_state(conn) -> ManagedRoleState:
    cur = await conn.execute(
        "SELECT current_database(),installation_id,contract_version,owner_role,"
        "runtime_password,control_password FROM odograph_service.managed_role_state WHERE id=1"
    )
    row = await cur.fetchone()
    _require_contract(row is not None and row[2:4] == (CONTRACT_VERSION, MIGRATE_ROLE),
                      "managed role state version or owner")
    _require(all(isinstance(value, str) and len(value) >= 32 for value in row[4:]))
    return ManagedRoleState(*row)


async def _provision(conn) -> None:
    """Initial provisioning or explicit restore only, never a startup repair."""
    await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    await conn.execute("GRANT USAGE,CREATE ON SCHEMA public TO odograph_migrate")
    await conn.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(
        sql.Identifier(conn.info.dbname)))
    for role in (CONTROL_ROLE, RUNTIME_ROLE, BOOTSTRAP_ROLE):
        ident = sql.Identifier(role)
        await conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
            sql.Identifier(conn.info.dbname), ident))
        await conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(conn.info.dbname), ident))
        await conn.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(ident))
        await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
    await conn.execute("ALTER SCHEMA odograph_service OWNER TO odograph_migrate")
    await conn.execute("REVOKE ALL ON SCHEMA odograph_service FROM PUBLIC")
    for table in TABLES:
        ident = sql.Identifier("public", table)
        await conn.execute(sql.SQL("ALTER TABLE {} OWNER TO odograph_migrate").format(ident))
        await conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(ident))
        for role in (CONTROL_ROLE, RUNTIME_ROLE, BOOTSTRAP_ROLE):
            await conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(ident, sql.Identifier(role)))
            rights = _table_rights(role, table)
            if rights:
                await conn.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(",".join(sorted(rights))), ident, sql.Identifier(role)))
    for table in OWNED_TABLES:
        ident = sql.Identifier("public", table)
        await conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident))
        await conn.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident))
    for (role, table), columns in COLUMN_SELECT.items():
        await conn.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier("public", table), sql.Identifier(role)))
    for table, sequence in await _sequences(conn):
        ident = sql.Identifier("public", sequence)
        await conn.execute(sql.SQL("REVOKE ALL ON SEQUENCE {} FROM PUBLIC,odograph_control,odograph_runtime,odograph_bootstrap").format(ident))
        for role in (CONTROL_ROLE, RUNTIME_ROLE, BOOTSTRAP_ROLE):
            if "INSERT" in _table_rights(role, table):
                await conn.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(ident, sql.Identifier(role)))
    for (table, name), (role, command, using, check) in _policy_contract().items():
        ident = sql.Identifier("public", table)
        await conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(sql.Identifier(name), ident))
        statement = sql.SQL("CREATE POLICY {} ON {} FOR {} TO {}").format(
            sql.Identifier(name), ident, sql.SQL(command), sql.Identifier(role))
        if using is not None:
            statement += sql.SQL(" USING ({})").format(sql.SQL(using))
        if check is not None:
            statement += sql.SQL(" WITH CHECK ({})").format(sql.SQL(check))
        await conn.execute(statement)
    for filename in FUNCTION_FILES:
        await conn.execute((SQL_DIR / filename).read_text())
    for function, role in FUNCTIONS.items():
        await conn.execute(sql.SQL("ALTER FUNCTION {} OWNER TO odograph_bootstrap").format(sql.SQL(function)))
        await conn.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC,odograph_control,odograph_runtime").format(sql.SQL(function)))
        await conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(function), sql.Identifier(role)))
    for table in ("managed_role_state", "recovery_metadata"):
        ident = sql.Identifier(STATE_SCHEMA, table)
        await conn.execute(sql.SQL("ALTER TABLE {} OWNER TO odograph_migrate").format(ident))
        await conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC,odograph_control,odograph_runtime,odograph_bootstrap").format(ident))
    await conn.execute("GRANT USAGE ON SCHEMA odograph_service TO odograph_control,odograph_runtime")
    await conn.execute("GRANT SELECT ON odograph_service.recovery_metadata TO odograph_control,odograph_runtime")


async def validate_application_contract(conn, state: ManagedRoleState) -> None:
    """Validate effective privileges and policies without fixing drift."""
    cur = await conn.execute(
        "SELECT c.relname,pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f') "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')")
    rows = await cur.fetchall()
    found_tables = {row[0] for row in rows}
    _require_contract(found_tables == set(TABLES),
        f"relation set: unexpected {sorted(found_tables - set(TABLES))} missing {sorted(set(TABLES) - found_tables)}")
    bad_relations = sorted(row[0] for row in rows
                           if row[1:] != (MIGRATE_ROLE, row[0] in OWNED_TABLES, row[0] in OWNED_TABLES))
    _require_contract(not bad_relations, f"relation ownership or RLS flags: {bad_relations}")
    cur = await conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE p.prosecdef AND p.oid <> ALL(%s::regprocedure[]) "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass "
        "AND d.objid=p.oid AND d.deptype='e') "
        "AND EXISTS (SELECT 1 FROM unnest(%s::text[]) role_name "
        "WHERE has_schema_privilege(role_name,n.oid,'USAGE') "
        "AND has_function_privilege(role_name,p.oid,'EXECUTE'))",
        (list(FUNCTIONS), [CONTROL_ROLE, RUNTIME_ROLE]),
    )
    extra_functions = [row[0] for row in await cur.fetchall()]
    _require_contract(not extra_functions, f"extra security-definer function: {extra_functions}")
    cur = await conn.execute("SELECT contract_version,owner_role,installation_id FROM odograph_service.recovery_metadata WHERE id=1")
    _require_contract(await cur.fetchone() == (state.contract_version, MIGRATE_ROLE, state.installation_id), "recovery metadata")
    cur = await conn.execute(
        "SELECT rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolcanlogin "
        "FROM pg_roles WHERE rolname=ANY(%s)", (list(ALL_ROLES),))
    rows = await cur.fetchall()
    found_roles = {row[0] for row in rows}
    _require_contract(found_roles == set(ALL_ROLES),
        f"role attributes: missing {sorted(set(ALL_ROLES) - found_roles)}")
    bad_roles = [row[0] for row in rows if list(row[1:]) != [False]*5 + [row[0] in (CONTROL_ROLE, RUNTIME_ROLE)]]
    _require_contract(not bad_roles, f"role attributes: {bad_roles}")
    cur = await conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname=ANY(%s) AND (oid IN (SELECT member FROM pg_auth_members) "
        "OR oid IN (SELECT roleid FROM pg_auth_members))", (list(ALL_ROLES),))
    memberships = [row[0] for row in await cur.fetchall()]
    _require_contract(not memberships, f"role membership: {memberships}")
    cur = await conn.execute(
        "SELECT r.rolname FROM pg_db_role_setting s JOIN pg_roles r ON r.oid=s.setrole WHERE r.rolname=ANY(%s)",
        (list(ALL_ROLES),))
    overrides = [row[0] for row in await cur.fetchall()]
    _require_contract(not overrides, f"role attributes: per-database overrides for {overrides}")
    cur = await conn.execute(
        "WITH objects AS ("
        " SELECT c.relname AS name,c.relowner AS owner,c.relacl AS acl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        " WHERE (n.nspname='public' AND c.relname=ANY(%s)) OR n.nspname='odograph_service'"
        " UNION ALL SELECT c.relname||'.'||a.attname,c.relowner,a.attacl FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        " JOIN pg_namespace n ON n.oid=c.relnamespace WHERE (n.nspname='public' AND c.relname=ANY(%s)) OR n.nspname='odograph_service')"
        " SELECT DISTINCT o.name FROM objects o CROSS JOIN LATERAL aclexplode(o.acl) x"
        " WHERE x.grantee=0 OR x.grantee NOT IN (SELECT oid FROM pg_roles WHERE rolname=ANY(%s))"
        " OR (x.is_grantable AND x.grantee<>o.owner)", (list(TABLES), list(TABLES), list(ALL_ROLES)))
    extra_grants = [row[0] for row in await cur.fetchall()]
    _require_contract(not extra_grants, f"table/column/sequence/function privilege: unexpected grant on {extra_grants}")
    cur = await conn.execute("SELECT tablename,policyname,roles,cmd,qual,with_check,permissive FROM pg_policies WHERE schemaname='public'")
    rows = await cur.fetchall()
    expected = _policy_contract()
    found_policies = {(row[0], row[1]) for row in rows}
    _require_contract(found_policies == set(expected),
        f"policy set: unexpected {sorted(found_policies - set(expected))} missing {sorted(set(expected) - found_policies)}")
    for table, name, roles, command, using, check, permissive in rows:
        role, expected_command, expected_using, expected_check = expected[table, name]
        _require_contract(roles == [role] and command == expected_command and permissive == "PERMISSIVE",
            f"policy set: {table}.{name}")
        _require_contract(_policy_expression(using) == _policy_expression(expected_using),
            f"policy set: {table}.{name} using expression")
        _require_contract(_policy_expression(check) == _policy_expression(expected_check),
            f"policy set: {table}.{name} check expression")
    for role in (CONTROL_ROLE, RUNTIME_ROLE, BOOTSTRAP_ROLE):
        cur = await conn.execute("SELECT has_database_privilege(%s,current_database(),'CREATE'),has_schema_privilege(%s,'public','CREATE')", (role, role))
        _require_contract(await cur.fetchone() == (False, False), f"database or schema privilege: {role}")
        for table in TABLES:
            rights = _table_rights(role, table)
            cur = await conn.execute(
                "SELECT privilege,has_table_privilege(%s,%s,privilege) FROM unnest(%s::text[]) privilege",
                (role, "public." + table, list(PRIVILEGES)))
            bad_privileges = [privilege for privilege, allowed in await cur.fetchall() if allowed != (privilege in rights)]
            _require_contract(not bad_privileges, f"table privilege: {role} public.{table} {bad_privileges}")
            cur = await conn.execute(
                "SELECT a.attname,p.privilege,has_column_privilege(%s,a.attrelid,a.attnum,p.privilege) "
                "FROM pg_attribute a CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) p(privilege) "
                "WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped", (role, "public." + table))
            bad_columns = []
            for column, privilege, allowed in await cur.fetchall():
                expected_allowed = privilege in rights or (
                    privilege == "SELECT" and column in COLUMN_SELECT.get((role, table), ()))
                if allowed != expected_allowed:
                    bad_columns.append((column, privilege))
            _require_contract(not bad_columns, f"column privilege: {role} public.{table} {bad_columns}")
        for function, caller in FUNCTIONS.items():
            cur = await conn.execute("SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, function))
            allowed = (await cur.fetchone())[0]
            _require_contract(allowed == (role in (caller, BOOTSTRAP_ROLE)), f"function privilege: {role} {function}")
        for table, sequence in await _sequences(conn):
            cur = await conn.execute(
                "SELECT p,has_sequence_privilege(%s,%s,p) FROM unnest(ARRAY['USAGE','SELECT','UPDATE']) p",
                (role, "public." + sequence))
            bad_privileges = [privilege for privilege, allowed in await cur.fetchall()
                              if allowed != (privilege == "USAGE" and "INSERT" in _table_rights(role, table))]
            _require_contract(not bad_privileges, f"sequence privilege: {role} public.{sequence} {bad_privileges}")
        for table in ("managed_role_state", "recovery_metadata"):
            cur = await conn.execute(
                "SELECT p,has_table_privilege(%s,%s,p) FROM unnest(%s::text[]) p",
                (role, STATE_SCHEMA + "." + table, list(PRIVILEGES)))
            bad_privileges = [privilege for privilege, allowed in await cur.fetchall()
                              if allowed != (privilege == "SELECT" and table == "recovery_metadata" and role in (RUNTIME_ROLE, CONTROL_ROLE))]
            _require_contract(not bad_privileges, f"table privilege: {role} {STATE_SCHEMA}.{table} {bad_privileges}")
    for function, filename in zip(FUNCTIONS, FUNCTION_FILES, strict=True):
        cur = await conn.execute(
            "SELECT pg_get_userbyid(proowner),prosecdef,proconfig,prosrc FROM pg_proc WHERE oid=%s::regprocedure", (function,))
        row = await cur.fetchone()
        source = (SQL_DIR / filename).read_text()
        body = re.search(r"AS\s+(\$[a-z_]*\$)(.*?)\1", source, re.S | re.I).group(2)
        _require_contract(row == (BOOTSTRAP_ROLE, True, ["search_path=pg_catalog, pg_temp"], body),
            f"function definition: {function}")


async def prepare_application_roles(database_url: str, *, restoring: bool = False) -> ManagedRoleState:
    try:
        async with await _SafeConnection.connect(database_url) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ROLE_SETUP_ADVISORY_LOCK_KEY,))
            cur = await conn.execute("SELECT security_contract_version FROM public.instance_state WHERE id=1")
            _require_contract(await cur.fetchone() == (CONTRACT_VERSION,), "security contract version")
            cur = await conn.execute("SELECT to_regclass('odograph_service.managed_role_state')")
            new = (await cur.fetchone())[0] is None
            if new or restoring:
                await _check_role_database_ownership(conn)
            if new:
                _require(not restoring)
                await conn.execute("CREATE SCHEMA odograph_service")
                await conn.execute(
                    "CREATE TABLE odograph_service.managed_role_state (id smallint PRIMARY KEY CHECK(id=1),"
                    "contract_version text NOT NULL,owner_role text NOT NULL,installation_id uuid NOT NULL,"
                    "runtime_password text NOT NULL,control_password text NOT NULL);"
                    "CREATE TABLE odograph_service.recovery_metadata (id smallint PRIMARY KEY CHECK(id=1),"
                    "contract_version text NOT NULL,owner_role text NOT NULL,installation_id uuid NOT NULL)")
                installation = uuid4()
                await conn.execute("INSERT INTO odograph_service.managed_role_state VALUES(1,%s,%s,%s,%s,%s)",
                    (CONTRACT_VERSION, MIGRATE_ROLE, installation, secrets.token_urlsafe(32), secrets.token_urlsafe(32)))
                await conn.execute("INSERT INTO odograph_service.recovery_metadata VALUES(1,%s,%s,%s)",
                    (CONTRACT_VERSION, MIGRATE_ROLE, installation))
            state = await _load_state(conn)
            if new or restoring:
                await _identities(conn)
                # ALTER ROLE locks shared catalog rows. Recheck after any
                # concurrent setup commits, before changing its credentials.
                await _check_role_database_ownership(conn)
                for role, password in ((CONTROL_ROLE, state.control_password), (RUNTIME_ROLE, state.runtime_password)):
                    verifier = conn.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256").decode()
                    await conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier)))
                await _provision(conn)
            await validate_application_contract(conn, state)
            return state
    except _RolesUsedElsewhere:
        raise
    except _ContractMismatch:
        raise
    except Exception:
        raise RoleSetupError("application database setup failed") from None


@asynccontextmanager
async def application_role_pools(database_url: str):
    state = await prepare_application_roles(database_url)
    pools = []
    try:
        for role in (CONTROL_ROLE, RUNTIME_ROLE):
            async def validate(conn, expected_role=role):
                async with conn.transaction():
                    cur = await conn.execute("SELECT session_user,current_user,current_database(),NULLIF(current_setting('app.account_id',true),'')")
                    _require(await cur.fetchone() == (expected_role, expected_role, state.database_name, None))
                    await check_runtime_privileges(conn)
                    await validate_application_contract(conn, state)
            conninfo = role_conninfo(database_url, state, role)
            async with await _SafeConnection.connect(conninfo) as conn:
                await validate(conn)
            pool = AsyncConnectionPool(conninfo, connection_class=_SafeConnection, min_size=1,
                max_size=6, open=False, configure=validate, timeout=5, name=f"application-{role}")
            pools.append(pool)
            await pool.open(wait=True, timeout=5)
        yield RolePools(control=pools[0], runtime=pools[1])
    finally:
        for pool in reversed(pools):
            await pool.close()


async def prepare_application_restore(database_url: str) -> None:
    """Create quarantined identities before restoring role-bearing objects."""
    try:
        async with await _SafeConnection.connect(database_url) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (ROLE_SETUP_ADVISORY_LOCK_KEY,))
            await _check_role_database_ownership(conn)
            await _identities(conn, quarantine=True)
            await _check_role_database_ownership(conn)
    except _RolesUsedElsewhere:
        raise
    except Exception:
        raise RoleSetupError("application restore identity setup failed") from None


async def finalize_application_restore(database_url: str) -> None:
    await prepare_application_roles(database_url, restoring=True)
    async with application_role_pools(database_url):
        pass


def main():
    import argparse
    import asyncio
    import os
    parser = argparse.ArgumentParser(description="Verify or recover the application database role contract")
    parser.add_argument("command", choices=("prepare-restore", "finalize-restore", "verify"))
    args = parser.parse_args()
    async def run():
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RoleSetupError("DATABASE_URL is required")
        if args.command == "prepare-restore":
            await prepare_application_restore(database_url)
        elif args.command == "finalize-restore":
            await finalize_application_restore(database_url)
        else:
            async with application_role_pools(database_url):
                pass
    try:
        asyncio.run(run())
    except RoleSetupError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
