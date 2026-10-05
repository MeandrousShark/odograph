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
from dataclasses import dataclass, replace
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
CONTROL_POOL_MAX_SIZE = 5
RUNTIME_POOL_MAX_SIZE = 6
OWNED_TABLES = (
    "raw_messages", "points", "stays", "trips", "detector_state", "places",
    "tag_rules", "geocode_cache", "trip_boundary_overrides", "vehicles",
    "mileage_rates", "odometer_readings", "expenses", "nudge_delivery_windows",
    "odometer_reminder_windows", "email_deliveries", "account_settings",
    "tracking_devices", "tracking_device_aliases", "ingest_credentials", "geocode_retry",
)
STORAGE_TABLES = ("account_usage", "device_storage_envelopes")
GEOCODE_TABLES = ("geocode_retry", "geocode_discovery")
GEOCODE_PROTECTED_TABLES = ("geocode_discovery",)
PROTECTED_TABLES = ("email_challenges",) + STORAGE_TABLES + GEOCODE_PROTECTED_TABLES
CONTROL_TABLES = (
    "accounts", "oidc_identities", "instance_state", "invitations",
    "oidc_attempts", "oidc_action_proofs", "account_security_audit",
)
REFERENCE_TABLES = ("schema_migrations", "reference_mileage_rates")
TABLES = OWNED_TABLES + PROTECTED_TABLES + CONTROL_TABLES + REFERENCE_TABLES
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
ACCOUNT_CONTROL_UPDATE_COLUMNS = (
    "updated_at", "avatar_bytes", "avatar_mime", "avatar_updated_at",
)
BOOTSTRAP_INSERT_TABLES = ("accounts", "account_settings", "vehicles", "tag_rules", "mileage_rates")
BOOTSTRAP_LOCK_TABLES = ("accounts", "ingest_credentials", "tracking_devices", "tracking_device_aliases")
SQL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "sql"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
FUNCTION_FILES = (
    "account_bootstrap.sql", "account_admission.sql", "tracking_admission.sql",
    "member_invitations.sql",
    "oidc_attempts.sql",
    "oidc_methods.sql",
    "account_lifecycle.sql",
)
@dataclass(frozen=True)
class FunctionSpec:
    signature: str
    caller: str
    source: str
    owner: str = BOOTSTRAP_ROLE
    security_definer: bool = True
    language: str = "plpgsql"
    volatility: str = "v"


FUNCTION_SPECS = (
    FunctionSpec("public.bootstrap_first_account(text,text,text)", CONTROL_ROLE, "account_bootstrap.sql"),
    FunctionSpec("public.assert_account_active(bigint,bigint)", RUNTIME_ROLE, "account_admission.sql"),
    FunctionSpec("public.assert_import_account_exclusive(bigint,bigint)", RUNTIME_ROLE,
                 "039_import_account_admission.sql", MIGRATE_ROLE),
    FunctionSpec("public.assert_tracking_credential(text,bigint,bigint,bigint,text)", RUNTIME_ROLE, "tracking_admission.sql"),
    FunctionSpec("public.issue_member_invitation(bigint,bigint,text,text)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.resend_member_invitation(bigint,bigint,bigint,text)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.revoke_member_invitation(bigint,bigint,bigint)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.list_member_invitations(bigint,bigint)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.admit_member_invitation_send(bigint,bigint,bigint)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.redeem_member_invitation(text,text,text)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.redeem_oidc_member_invitation(text,text,text,text,text,text)", CONTROL_ROLE, "member_invitations.sql"),
    FunctionSpec("public.start_oidc_attempt(text,text,text,text,bigint,bigint,text,text,text)", CONTROL_ROLE, "oidc_attempts.sql"),
    FunctionSpec("public.consume_oidc_attempt(text,text,text,text,bigint,bigint)", CONTROL_ROLE, "oidc_attempts.sql"),
    FunctionSpec("public.finish_oidc_reauth(text,text,text,bigint,bigint,text,text,timestamptz)", CONTROL_ROLE, "oidc_attempts.sql"),
    FunctionSpec("public.consume_oidc_action_proof(bigint,bigint,text,text,text)", CONTROL_ROLE, "oidc_attempts.sql"),
    FunctionSpec("public.link_oidc_identity(bigint,bigint,text,text,text,text)", CONTROL_ROLE, "oidc_methods.sql"),
    FunctionSpec("public.unlink_oidc_identity(bigint,bigint,text,text)", CONTROL_ROLE, "oidc_methods.sql"),
    FunctionSpec("public.replace_account_password(bigint,bigint,text)", CONTROL_ROLE, "oidc_methods.sql"),
    FunctionSpec("public.sign_out_account_everywhere(bigint,bigint)", CONTROL_ROLE, "oidc_methods.sql"),
    FunctionSpec("public.issue_email_challenge(bigint,bigint,text,text,text)", CONTROL_ROLE, "031_password_reset.sql"),
    FunctionSpec("public.revoke_email_challenge(bigint,text,text)", CONTROL_ROLE, "030_email_challenges.sql"),
    FunctionSpec("public.consume_email_challenge(bigint,bigint,text,text)", CONTROL_ROLE, "030_email_challenges.sql"),
    FunctionSpec("public.email_challenge_send_usable(bigint,bigint,text,text)", CONTROL_ROLE, "035_admin_recovery.sql"),
    FunctionSpec("public.issue_password_reset(bigint,text,text,text)", CONTROL_ROLE, "035_admin_recovery.sql"),
    FunctionSpec("public.issue_admin_password_reset(bigint,bigint,bigint,text)", CONTROL_ROLE, "035_admin_recovery.sql"),
    FunctionSpec("public.revoke_password_reset(text)", CONTROL_ROLE, "031_password_reset.sql"),
    FunctionSpec("public.password_reset_usable(text,boolean)", CONTROL_ROLE, "031_password_reset.sql"),
    FunctionSpec("public.password_reset_send_usable(text,bigint,bigint)", CONTROL_ROLE, "035_admin_recovery.sql"),
    FunctionSpec("public.consume_password_reset(text,text)", CONTROL_ROLE, "031_password_reset.sql"),
    FunctionSpec("public.host_reset_password(bigint,text)", CONTROL_ROLE, "031_password_reset.sql"),
    FunctionSpec("public.admin_set_account_enabled(bigint,bigint,bigint,boolean)", CONTROL_ROLE, "account_lifecycle.sql"),
    FunctionSpec("public.list_account_security_audit(bigint,bigint)", CONTROL_ROLE, "account_lifecycle.sql"),
    FunctionSpec("public.prune_account_security_audit()", CONTROL_ROLE, "account_lifecycle.sql"),
    FunctionSpec("public.admin_request_account_deletion(bigint,bigint,bigint,text,boolean)", CONTROL_ROLE, "account_lifecycle.sql"),
    FunctionSpec("public.admin_cancel_account_deletion(bigint,bigint,bigint)", CONTROL_ROLE, "account_lifecycle.sql"),
    FunctionSpec("public.admin_purge_account(bigint,bigint,bigint,text,boolean,text,text)", CONTROL_ROLE, "account_lifecycle.sql"),
)
# Compatibility views for older-schema upgrade fixtures and focused tests.
INVITATION_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS if s.source == "member_invitations.sql")
OIDC_ATTEMPT_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS if s.source == "oidc_attempts.sql")
OIDC_METHOD_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS if s.source == "oidc_methods.sql")
ACCOUNT_LIFECYCLE_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS if s.source == "account_lifecycle.sql")
IMPORT_ADMISSION_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS
                                   if s.source == "039_import_account_admission.sql")
EMAIL_CHALLENGE_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS if "email_challenge" in s.signature)
PASSWORD_RESET_FUNCTIONS = tuple(s.signature for s in FUNCTION_SPECS
                                 if s.source in ("031_password_reset.sql", "035_admin_recovery.sql")
                                 and s.signature not in EMAIL_CHALLENGE_FUNCTIONS)
STORAGE_FUNCTIONS = tuple(
    "public.storage_charge_" + table + "(public." + table + ")" for table in OWNED_TABLES if table not in GEOCODE_TABLES
) + (
    "public.storage_account_init()", "public.storage_apply_statement()",
    "public.storage_row_account_guard()",
    "public.storage_avatar_change()", "public.storage_check_envelope()",
    "public.storage_write_admission()", "public.storage_envelope_metadata()",
    "public.storage_expected_usage()", "public.storage_expected_envelopes()",
    "public.storage_usage_consistent()", "public.reconcile_storage_usage()",
)
FUNCTION_SPECS += tuple(
    FunctionSpec(signature, MIGRATE_ROLE, "040_storage_accounting.sql", MIGRATE_ROLE,
                 not signature.startswith("public.storage_charge_"),
                 "sql" if signature.startswith(("public.storage_charge_", "public.storage_expected_",
                                                "public.storage_usage_consistent")) else "plpgsql",
                 "i" if signature.startswith("public.storage_charge_") else
                 "s" if signature.startswith(("public.storage_expected_", "public.storage_usage_consistent")) else "v")
    for signature in STORAGE_FUNCTIONS
)
GEOCODE_FUNCTION_SPECS = tuple(
    FunctionSpec("public.storage_charge_" + table + "(public." + table + ")",
                 MIGRATE_ROLE, "041_geocode_progress.sql", MIGRATE_ROLE, False, "sql", "i")
    for table in GEOCODE_TABLES
) + tuple(
    FunctionSpec(signature, caller, "041_geocode_progress.sql", MIGRATE_ROLE)
    for signature, caller in (
        ("public.geocode_trip_generation()", MIGRATE_ROLE),
        ("public.geocode_device_generation()", MIGRATE_ROLE),
        ("public.geocode_endpoint_intents()", MIGRATE_ROLE),
        ("public.geocode_account_init()", MIGRATE_ROLE),
        ("public.geocode_record_coordinate_turn(bigint)", RUNTIME_ROLE),
        ("public.geocode_discover_page(bigint)", RUNTIME_ROLE),
        ("public.geocode_representative_source(bigint,numeric,numeric)", RUNTIME_ROLE),
    )
)
GEOCODE_FUNCTIONS = tuple(spec.signature for spec in GEOCODE_FUNCTION_SPECS)
FUNCTION_SPECS = tuple(
    replace(spec, source="041_geocode_progress.sql")
    if spec.signature == "public.storage_expected_usage()" else spec
    for spec in FUNCTION_SPECS
) + GEOCODE_FUNCTION_SPECS
STORAGE_FUNCTIONS += tuple(spec.signature for spec in GEOCODE_FUNCTION_SPECS
                           if spec.signature.startswith("public.storage_charge_"))
FUNCTIONS = {spec.signature: spec.caller for spec in FUNCTION_SPECS}
FUNCTION_OWNERS = {spec.signature: spec.owner for spec in FUNCTION_SPECS}
RESTORE_AUTH_VERSION_STEP = 1_000_000_000
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


async def _check_contract_functions_exist(conn, signatures) -> None:
    cur = await conn.execute(
        "SELECT signature FROM unnest(%s::text[]) signature "
        "WHERE to_regprocedure(signature) IS NULL", (list(signatures),))
    missing = [row[0] for row in await cur.fetchall()]
    _require_contract(not missing, f"missing contract function: {missing}")


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
        if table in STORAGE_TABLES + GEOCODE_PROTECTED_TABLES:
            return {"SELECT"}
        if table in OWNED_TABLES:
            return {"INSERT", "UPDATE", "DELETE"} | ({"SELECT"} if table != "ingest_credentials" else set())
        if table in REFERENCE_TABLES:
            return {"SELECT"}
    if role == CONTROL_ROLE:
        if table == "accounts":
            return {"SELECT"}
        if table == "oidc_identities":
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table in ("instance_state", "schema_migrations"):
            return {"SELECT"}
    if role == BOOTSTRAP_ROLE:
        rights = {"SELECT", "DELETE"} if table in OWNED_TABLES + STORAGE_TABLES + GEOCODE_PROTECTED_TABLES + ("accounts", "invitations", "email_challenges") else set()
        if table == "account_security_audit":
            return {"SELECT", "INSERT", "DELETE"}
        if table == "invitations":
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table in ("oidc_attempts", "oidc_action_proofs"):
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table == "oidc_identities":
            return {"SELECT", "INSERT", "DELETE"}
        if table == "email_challenges":
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
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
                for table in OWNED_TABLES + PROTECTED_TABLES}
    for table in STORAGE_TABLES + GEOCODE_PROTECTED_TABLES:
        if table in PROTECTED_TABLES:
            policies[table, "account_isolation"] = (RUNTIME_ROLE, "SELECT", expression, None)
    if "public.storage_usage_consistent()" in FUNCTIONS:
        for table in OWNED_TABLES + PROTECTED_TABLES:
            policies[table, "migration_writer"] = (MIGRATE_ROLE, "ALL", "true", "true")
    for table in ("ingest_credentials", "tracking_devices"):
        policies[table, "control_lookup"] = (CONTROL_ROLE, "SELECT", "true", None)
    for table in OWNED_TABLES + PROTECTED_TABLES:
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
    for table in OWNED_TABLES + PROTECTED_TABLES:
        ident = sql.Identifier("public", table)
        await conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident))
        await conn.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident))
    for (role, table), columns in COLUMN_SELECT.items():
        await conn.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier("public", table), sql.Identifier(role)))
    await conn.execute(sql.SQL("GRANT UPDATE ({}) ON public.accounts TO odograph_control").format(
        sql.SQL(",").join(map(sql.Identifier, ACCOUNT_CONTROL_UPDATE_COLUMNS))))
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
        owner = FUNCTION_OWNERS[function]
        await conn.execute(sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
            sql.SQL(function), sql.Identifier(owner)))
        revoked = "PUBLIC,odograph_control,odograph_runtime"
        if owner != BOOTSTRAP_ROLE:
            revoked += ",odograph_bootstrap"
        await conn.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM " + revoked).format(sql.SQL(function)))
        await conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(function), sql.Identifier(role)))
    for table in ("managed_role_state", "recovery_metadata"):
        ident = sql.Identifier(STATE_SCHEMA, table)
        await conn.execute(sql.SQL("ALTER TABLE {} OWNER TO odograph_migrate").format(ident))
        await conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC,odograph_control,odograph_runtime,odograph_bootstrap").format(ident))
    await conn.execute("GRANT USAGE ON SCHEMA odograph_service TO odograph_control,odograph_runtime")
    await conn.execute("GRANT SELECT ON odograph_service.recovery_metadata TO odograph_control,odograph_runtime")


async def _revoke_restored_security_state(conn) -> None:
    """A restore rolls credentials and auth_version back while SESSION_SECRET
    is unchanged, so a cookie revoked after the backup would validate again.
    End every session and revoke outstanding invitations and challenges,
    including password resets, in the same transaction as role recovery.
    Older archives may predate either lifecycle table.

    Versions issued after the backup may already be higher than the restored
    value, so one increment could revive a later-revoked cookie. The large
    step moves past any version a real account could have reached since.
    """
    await conn.execute(
        "UPDATE public.accounts SET auth_version = auth_version + %s", (RESTORE_AUTH_VERSION_STEP,))
    for table in ("invitations", "email_challenges"):
        cur = await conn.execute("SELECT to_regclass(%s)", ("public." + table,))
        if (await cur.fetchone())[0] is not None:
            await conn.execute(sql.SQL(
                "UPDATE {} SET revoked_at = pg_catalog.clock_timestamp() "
                "WHERE consumed_at IS NULL AND revoked_at IS NULL"
            ).format(sql.Identifier("public", table)))
    for table in ("oidc_attempts", "oidc_action_proofs"):
        cur = await conn.execute("SELECT to_regclass(%s)", ("public." + table,))
        if (await cur.fetchone())[0] is not None:
            await conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier("public", table)))


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
                           if row[1:] != (MIGRATE_ROLE, row[0] in OWNED_TABLES + PROTECTED_TABLES,
                                          row[0] in OWNED_TABLES + PROTECTED_TABLES))
    _require_contract(not bad_relations, f"relation ownership or RLS flags: {bad_relations}")
    await _check_contract_functions_exist(conn, FUNCTIONS)
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
    legacy_issue = "public.issue_member_invitation(bigint,text,text)"
    cur = await conn.execute("SELECT to_regprocedure(%s)", (legacy_issue,))
    if (await cur.fetchone())[0] is not None:
        for role in (CONTROL_ROLE, RUNTIME_ROLE):
            cur = await conn.execute(
                "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, legacy_issue),
            )
            _require_contract(not (await cur.fetchone())[0],
                              f"legacy function privilege: {role} {legacy_issue}")
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
        " JOIN pg_namespace n ON n.oid=c.relnamespace WHERE (n.nspname='public' AND c.relname=ANY(%s)) OR n.nspname='odograph_service'"
        " UNION ALL SELECT s.relname,s.relowner,s.relacl FROM pg_class s "
        " JOIN pg_namespace sn ON sn.oid=s.relnamespace "
        " JOIN pg_depend d ON d.objid=s.oid AND d.deptype IN ('a','i') "
        " JOIN pg_class t ON t.oid=d.refobjid JOIN pg_namespace tn ON tn.oid=t.relnamespace "
        " WHERE s.relkind='S' AND sn.nspname='public' AND tn.nspname='public' AND t.relname=ANY(%s)"
        " UNION ALL SELECT p.oid::regprocedure::text,p.proowner,p.proacl FROM pg_proc p "
        " WHERE p.oid=ANY(%s::regprocedure[]))"
        " SELECT DISTINCT o.name FROM objects o CROSS JOIN LATERAL aclexplode(o.acl) x"
        " WHERE x.grantee=0 OR x.grantee NOT IN (SELECT oid FROM pg_roles WHERE rolname=ANY(%s))"
        " OR (x.is_grantable AND x.grantee<>o.owner)",
        (list(TABLES), list(TABLES), list(TABLES), list(FUNCTIONS), list(ALL_ROLES)))
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
                if privilege == "UPDATE" and role == CONTROL_ROLE and table == "accounts":
                    expected_allowed = "UPDATE" in rights or column in ACCOUNT_CONTROL_UPDATE_COLUMNS
                if allowed != expected_allowed:
                    bad_columns.append((column, privilege))
            _require_contract(not bad_columns, f"column privilege: {role} public.{table} {bad_columns}")
        for function, caller in FUNCTIONS.items():
            cur = await conn.execute("SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, function))
            allowed = (await cur.fetchone())[0]
            _require_contract(allowed == (role in (caller, FUNCTION_OWNERS[function])),
                              f"function privilege: {role} {function}")
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
    if "public.storage_usage_consistent()" in FUNCTIONS:
        await _validate_storage_triggers(conn)
    if "public.geocode_discover_page(bigint)" in FUNCTIONS:
        await _validate_geocode_schema(conn)
        await _validate_geocode_indexes(conn)
    sources = {}
    for spec in FUNCTION_SPECS:
        function = spec.signature
        if function not in FUNCTIONS:
            continue
        cur = await conn.execute(
            "SELECT pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig,p.prosrc,l.lanname,p.provolatile "
            "FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang WHERE p.oid=%s::regprocedure",
            (function,))
        row = await cur.fetchone()
        source_dir = MIGRATIONS_DIR if spec.source.endswith(".sql") and spec.source[0].isdigit() else SQL_DIR
        path = source_dir / spec.source
        if path not in sources:
            sources[path] = path.read_text()
        body = _function_body(sources[path], function)
        _require_contract(row == (spec.owner, spec.security_definer, ["search_path=pg_catalog, pg_temp"], body,
                                  spec.language, spec.volatility),
            f"function definition: {function}")


async def _validate_geocode_schema(conn) -> None:
    columns = {
        "geocode_retry": (
            ("account_id", "bigint", True, None),
            ("rounded_lat", "numeric(8,4)", True, None),
            ("rounded_lon", "numeric(8,4)", True, None),
            ("attempted_at", "timestamp with time zone", False, None),
            ("next_attempt_at", "timestamp with time zone", True, "now()"),
            ("failure_count", "integer", True, "0"),
            ("failure_reason", "geocode_failure_reason", False, None),
        ),
        "geocode_discovery": (
            ("account_id", "bigint", True, None),
            ("cursor_trip_id", "bigint", True, "0"),
            ("generation", "bigint", True, "1"),
            ("round_generation", "bigint", True, "1"),
            ("scanned_generation", "bigint", True, "0"),
            ("last_unit", "geocode_work_unit", True, "'coordinate'::geocode_work_unit"),
        ),
        "trips": (("geocode_generation", "bigint", True, "1"),),
        "tracking_devices": (("geocode_generation", "bigint", True, "1"),),
    }
    cur = await conn.execute(
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
        "pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE c.relnamespace='public'::regnamespace AND a.attnum>0 AND NOT a.attisdropped "
        "AND (c.relname=ANY(%s) OR (c.relname IN ('trips','tracking_devices') AND a.attname='geocode_generation')) "
        "ORDER BY c.relname,a.attnum", (list(GEOCODE_TABLES),))
    found = {table: [] for table in columns}
    for table, *definition in await cur.fetchall():
        found[table].append(tuple(definition))
    for table, expected in columns.items():
        _require_contract(found[table] == [row + ("", "") for row in expected],
                          f"geocode column definition: public.{table}")
    constraints = {
        "geocode_retry_account_id_fkey": ("geocode_retry", "FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE"),
        "geocode_retry_pkey": ("geocode_retry", "PRIMARY KEY (account_id, rounded_lat, rounded_lon)"),
        "geocode_retry_failure_count_check": ("geocode_retry", "CHECK (((failure_count >= 0) AND (failure_count <= 31)))"),
        "geocode_discovery_account_id_fkey": ("geocode_discovery", "FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE"),
        "geocode_discovery_pkey": ("geocode_discovery", "PRIMARY KEY (account_id)"),
        "geocode_discovery_cursor_trip_id_check": ("geocode_discovery", "CHECK ((cursor_trip_id >= 0))"),
        "geocode_discovery_generation_check": ("geocode_discovery", "CHECK ((generation > 0))"),
        "geocode_discovery_round_generation_check": ("geocode_discovery", "CHECK ((round_generation > 0))"),
        "geocode_discovery_scanned_generation_check": ("geocode_discovery", "CHECK ((scanned_generation >= 0))"),
        "trips_geocode_generation_check": ("trips", "CHECK ((geocode_generation > 0))"),
        "tracking_devices_geocode_generation_check": ("tracking_devices", "CHECK ((geocode_generation > 0))"),
    }
    for axis, limit in (("lat", 90), ("lon", 180)):
        constraints[f"geocode_retry_rounded_{axis}_check"] = (
            "geocode_retry", f"CHECK (((rounded_{axis} >= ('-{limit}'::integer)::numeric) "
            f"AND (rounded_{axis} <= ({limit})::numeric)))")
    cur = await conn.execute(
        "SELECT co.conname,c.relname,pg_get_constraintdef(co.oid),co.convalidated,"
        "co.condeferrable,co.condeferred,co.connoinherit "
        "FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid "
        "WHERE c.relnamespace='public'::regnamespace "
        "AND (c.relname=ANY(%s) OR co.conname IN ('trips_geocode_generation_check','tracking_devices_geocode_generation_check'))",
        (list(GEOCODE_TABLES),))
    found_constraints = {name: tuple(definition) for name, *definition in await cur.fetchall()}
    _require_contract(found_constraints == {
        name: definition + (True, False, False, not definition[1].startswith("CHECK"))
        for name, definition in constraints.items()
    }, "geocode constraint definition")
    cur = await conn.execute(
        "SELECT t.typname,e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid "
        "WHERE t.typnamespace='public'::regnamespace AND t.typname=ANY(%s) "
        "ORDER BY t.typname,e.enumsortorder", (["geocode_failure_reason", "geocode_work_unit"],))
    _require_contract(await cur.fetchall() == [
        ("geocode_failure_reason", reason) for reason in ("http", "transport", "parse", "source_changed")
    ] + [("geocode_work_unit", unit) for unit in ("discovery", "coordinate")], "geocode enum definition")


async def _validate_geocode_indexes(conn) -> None:
    definitions = {
        "geocode_retry_pkey": "CREATE UNIQUE INDEX geocode_retry_pkey ON public.geocode_retry "
            "USING btree (account_id, rounded_lat, rounded_lon)",
        "geocode_discovery_pkey": "CREATE UNIQUE INDEX geocode_discovery_pkey ON public.geocode_discovery "
            "USING btree (account_id)",
        "geocode_retry_due_idx": "CREATE INDEX geocode_retry_due_idx ON public.geocode_retry "
            "USING btree (account_id, next_attempt_at, attempted_at NULLS FIRST, rounded_lat, rounded_lon)",
        "trips_geocode_discovery_idx": "CREATE INDEX trips_geocode_discovery_idx ON public.trips "
            "USING btree (account_id, id)",
    }
    for endpoint in ("start", "end"):
        name = "trips_geocode_" + endpoint + "_idx"
        definitions[name] = (
            f"CREATE INDEX {name} ON public.trips USING btree (account_id, "
            f"round((st_y(({endpoint}_geom)::geometry))::numeric, 4), "
            f"round((st_x(({endpoint}_geom)::geometry))::numeric, 4), id) "
            f"WHERE (({endpoint}_geom IS NOT NULL) AND ({endpoint}_place_id IS NULL))")
    cur = await conn.execute(
        "SELECT c.relname,pg_get_userbyid(c.relowner),i.indisvalid,i.indisready,i.indislive,"
        "i.indisprimary,pg_get_indexdef(c.oid) FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
        "WHERE c.relnamespace='public'::regnamespace "
        "AND (t.relname=ANY(%s) OR c.relname=ANY(%s))",
        (list(GEOCODE_TABLES), list(definitions)),
    )
    rows = await cur.fetchall()
    found = {row[0] for row in rows}
    _require_contract(found == set(definitions),
        f"geocode index set: unexpected {sorted(found - set(definitions))} "
        f"missing {sorted(set(definitions) - found)}")
    for name, owner, valid, ready, live, primary, definition in rows:
        _require_contract((owner, valid, ready, live, primary, definition) ==
                          (MIGRATE_ROLE, True, True, True, name.endswith("_pkey"), definitions[name]),
                          f"geocode index definition: {name}")


async def _validate_storage_triggers(conn) -> None:
    expected = {}
    for table in OWNED_TABLES + tuple(table for table in GEOCODE_PROTECTED_TABLES if table in PROTECTED_TABLES):
        for event, kind, old_table, new_table in (
            ("insert", 4, None, "storage_new_rows"),
            ("update", 16, "storage_old_rows", "storage_new_rows"),
            ("delete", 8, "storage_old_rows", None),
        ):
            expected[table, "storage_charge_" + event] = (
                "storage_apply_statement", kind, (), False, False, old_table, new_table)
        expected[table, "storage_write_admission"] = (
            "storage_write_admission", 30, (), False, False, None, None)
        expected[table, "storage_row_account_guard"] = (
            "storage_row_account_guard", 17, (), False, False, None, None)
    expected.update({
        ("device_storage_envelopes", "storage_envelope_metadata"):
            ("storage_envelope_metadata", 13, (), False, False, None, None),
        ("accounts", "storage_account_init"): ("storage_account_init", 5, (), False, False, None, None),
        ("accounts", "storage_avatar_change"):
            ("storage_avatar_change", 17, ("avatar_bytes", "avatar_mime"), False, False, None, None),
        ("device_storage_envelopes", "storage_envelope_final"):
            ("storage_check_envelope", 29, (), True, True, None, None),
    })
    if "public.geocode_discover_page(bigint)" in FUNCTIONS:
        expected["trips", "geocode_trip_generation"] = (
            "geocode_trip_generation", 23, (), False, False, None, None)
        expected["tracking_devices", "geocode_device_generation"] = (
            "geocode_device_generation", 23, (), False, False, None, None)
        expected["accounts", "z_geocode_account_init"] = (
            "geocode_account_init", 5, (), False, False, None, None)
        for table in ("trips", "tracking_devices"):
            for event, kind, old_table, new_table in (
                ("insert", 4, None, "geocode_new_rows"),
                ("update", 16, "geocode_old_rows", "geocode_new_rows"),
                ("delete", 8, "geocode_old_rows", None),
            ):
                expected[table, "geocode_intents_" + event] = (
                    "geocode_endpoint_intents", kind, (), False, False, old_table, new_table)
        expected["geocode_cache", "geocode_intents_delete"] = (
            "geocode_endpoint_intents", 8, (), False, False, "geocode_old_rows", None)
    cur = await conn.execute(
        "SELECT c.relname,t.tgname,p.proname,t.tgtype,"
        "ARRAY(SELECT a.attname FROM pg_attribute a WHERE a.attrelid=t.tgrelid "
        "AND a.attnum=ANY(t.tgattr) ORDER BY a.attnum),"
        "t.tgdeferrable,t.tginitdeferred,t.tgenabled,t.tgnargs,t.tgqual IS NULL,"
        "p.pronamespace='public'::regnamespace,t.tgconstraint<>0,t.tgoldtable,t.tgnewtable "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_proc p ON p.oid=t.tgfoid WHERE NOT t.tgisinternal "
        "AND c.relnamespace='public'::regnamespace AND c.relname=ANY(%s)",
        (list(TABLES),))
    rows = await cur.fetchall()
    found = {(row[0], row[1]) for row in rows}
    _require_contract(found == set(expected),
        f"storage trigger set: unexpected {sorted(found - set(expected))} missing {sorted(set(expected) - found)}")
    for (table, name, function, kind, columns, deferred, initially, enabled, args,
         no_qual, public, constraint, old_table, new_table) in rows:
        actual = (function, kind, tuple(columns), deferred, initially, old_table, new_table)
        _require_contract(actual == expected[table, name]
                          and enabled == "O" and args == 0 and no_qual and public
                          and constraint == deferred,
                          f"storage trigger definition: {table}.{name}")


def _function_body(source: str, signature: str) -> str:
    name, arguments = signature.rstrip(")").split("(", 1)
    wanted = arguments.split(",") if arguments else []
    definitions = re.finditer(
        rf"CREATE (?:OR REPLACE )?FUNCTION {re.escape(name)}\((.*?)\)"
        rf".*?AS\s+(\$[a-z_]*\$)(.*?)\2", source, re.S | re.I)
    for definition in definitions:
        params = definition.group(1).split(",") if definition.group(1).strip() else []
        types = [re.split(r"\s+DEFAULT\s+|\s*=\s*", param.strip(), flags=re.I)[0]
                 .split()[-1] for param in params]
        if types == wanted:
            return definition.group(3)
    raise _ContractMismatch("application database security contract mismatch: function source " + signature)


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
                if restoring:
                    await _check_contract_functions_exist(
                        conn, (spec.signature for spec in FUNCTION_SPECS
                               if spec.signature in FUNCTIONS and spec.source not in FUNCTION_FILES))
                for role, password in ((CONTROL_ROLE, state.control_password), (RUNTIME_ROLE, state.runtime_password)):
                    verifier = conn.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256").decode()
                    await conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier)))
                await _provision(conn)
            if restoring:
                await _revoke_restored_security_state(conn)
            await validate_application_contract(conn, state)
            if "public.storage_usage_consistent()" in FUNCTIONS:
                if restoring:
                    await conn.execute("SELECT public.reconcile_storage_usage()")
                cur = await conn.execute("SELECT public.storage_usage_consistent()")
                _require_contract(await cur.fetchone() == (True,), "storage accounting data")
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
            max_size = CONTROL_POOL_MAX_SIZE if role == CONTROL_ROLE else RUNTIME_POOL_MAX_SIZE
            pool = AsyncConnectionPool(conninfo, connection_class=_SafeConnection, min_size=1,
                max_size=max_size, open=False, configure=validate, timeout=5, name=f"application-{role}")
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
