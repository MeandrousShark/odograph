"""The role contract validator runs on every new pooled connection, so it
fetches effective rights in a fixed number of set-based statements. These
cases pin that bound and the exact rejection for each batched check."""
from __future__ import annotations

import asyncio
import os

import psycopg
import pytest

from app.application_roles import prepare_application_roles, validate_application_contract
from app.db import make_pool
from app.role_setup import RoleSetupError
from conftest import provisioned_role_pools, reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable PostGIS")

# One validation issued 657 statements before rights were batched and 27
# after. The margin allows a few new set-based checks, not per-object loops.
MAX_VALIDATION_STATEMENTS = 32

PREFIX = "application database security contract mismatch: "
LEGACY_ISSUE = "public.issue_member_invitation(bigint,text,text)"
# Each drift is applied in a rolled-back transaction. The expected messages
# are the ones the per-object validator produced before batching.
DRIFTS = (
    ("GRANT SELECT ON public.trips TO odograph_control",
     "table privilege: odograph_control public.trips ['SELECT']"),
    ("REVOKE DELETE ON public.trips FROM odograph_runtime",
     "table privilege: odograph_runtime public.trips ['DELETE']"),
    ("GRANT TRUNCATE ON public.accounts TO odograph_bootstrap",
     "table privilege: odograph_bootstrap public.accounts ['TRUNCATE']"),
    # The first role in validation order is reported.
    ("GRANT SELECT ON public.trips TO odograph_bootstrap; GRANT SELECT ON public.trips TO odograph_control",
     "table privilege: odograph_control public.trips ['SELECT']"),
    ("GRANT SELECT (password_hash) ON public.accounts TO odograph_runtime",
     "column privilege: odograph_runtime public.accounts [('password_hash', 'SELECT')]"),
    ("REVOKE UPDATE (display_name) ON public.accounts FROM odograph_control",
     "column privilege: odograph_control public.accounts [('display_name', 'UPDATE')]"),
    ("GRANT UPDATE (email) ON public.accounts TO odograph_control",
     "column privilege: odograph_control public.accounts [('email', 'UPDATE')]"),
    ("GRANT SELECT (secret_hash) ON public.ingest_credentials TO odograph_runtime",
     "column privilege: odograph_runtime public.ingest_credentials [('secret_hash', 'SELECT')]"),
    ("REVOKE SELECT (enabled) ON public.tracking_devices FROM odograph_control",
     "column privilege: odograph_control public.tracking_devices [('enabled', 'SELECT')]"),
    ("GRANT REFERENCES (id) ON public.vehicles TO odograph_runtime",
     "column privilege: odograph_runtime public.vehicles [('id', 'REFERENCES')]"),
    ("GRANT EXECUTE ON FUNCTION public.host_reset_password(bigint,text) TO odograph_runtime",
     "function privilege: odograph_runtime public.host_reset_password(bigint,text)"),
    ("REVOKE EXECUTE ON FUNCTION public.assert_account_active(bigint,bigint) FROM odograph_runtime",
     "function privilege: odograph_runtime public.assert_account_active(bigint,bigint)"),
    ("GRANT EXECUTE ON FUNCTION public.storage_instance_status() TO odograph_bootstrap",
     "function privilege: odograph_bootstrap public.storage_instance_status()"),
    ("ALTER FUNCTION public.storage_charge_trips(public.trips) OWNER TO odograph_bootstrap",
     "function privilege: odograph_bootstrap public.storage_charge_trips(public.trips)"),
    ("GRANT SELECT ON SEQUENCE public.trips_id_seq TO odograph_runtime",
     "sequence privilege: odograph_runtime public.trips_id_seq ['SELECT']"),
    ("REVOKE USAGE ON SEQUENCE public.trips_id_seq FROM odograph_runtime",
     "sequence privilege: odograph_runtime public.trips_id_seq ['USAGE']"),
    ("GRANT USAGE ON SEQUENCE public.accounts_id_seq TO odograph_control",
     "sequence privilege: odograph_control public.accounts_id_seq ['USAGE']"),
    ("GRANT SELECT ON odograph_service.managed_role_state TO odograph_runtime",
     "table privilege: odograph_runtime odograph_service.managed_role_state ['SELECT']"),
    ("REVOKE SELECT ON odograph_service.recovery_metadata FROM odograph_control",
     "table privilege: odograph_control odograph_service.recovery_metadata ['SELECT']"),
    ("GRANT CREATE ON SCHEMA public TO odograph_bootstrap",
     "database or schema privilege: odograph_bootstrap"),
    ("GRANT CREATE ON DATABASE {database} TO odograph_runtime",
     "database or schema privilege: odograph_runtime"),
    ("ALTER FUNCTION public.host_reset_password(bigint,text) SET search_path = public",
     "function definition: public.host_reset_password(bigint,text)"),
    ("ALTER FUNCTION public.storage_expected_usage() VOLATILE",
     "function definition: public.storage_expected_usage()"),
    ("ALTER FUNCTION public.list_member_invitations(bigint,bigint) SECURITY INVOKER",
     "function definition: public.list_member_invitations(bigint,bigint)"),
    ("CREATE OR REPLACE FUNCTION public.storage_charge_trips(r public.trips) RETURNS bigint[] "
     "LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$ SELECT ARRAY[0::bigint] $$",
     "function definition: public.storage_charge_trips(public.trips)"),
    (f"GRANT EXECUTE ON FUNCTION {LEGACY_ISSUE} TO odograph_control",
     "extra security-definer function: ['issue_member_invitation(bigint,text,text)']"),
    (f"ALTER FUNCTION {LEGACY_ISSUE} SECURITY INVOKER; GRANT EXECUTE ON FUNCTION {LEGACY_ISSUE} TO odograph_runtime",
     f"legacy function privilege: odograph_runtime {LEGACY_ISSUE}"),
)


async def _scenario(callback):
    owner = make_pool(TEST_DB)
    await owner.open(wait=True)
    try:
        await reset_db(owner)
        state = await prepare_application_roles(TEST_DB)
        async with provisioned_role_pools(owner) as pools:
            await callback(owner, pools, state)
    finally:
        await owner.close()


async def _counted_validation(conn, state, monkeypatch) -> int:
    count = 0
    execute = psycopg.AsyncCursor.execute

    async def counting(self, *args, **kwargs):
        nonlocal count
        count += 1
        return await execute(self, *args, **kwargs)

    # Connection.execute delegates to a cursor, so each statement counts once.
    with monkeypatch.context() as patch:
        patch.setattr(psycopg.AsyncCursor, "execute", counting)
        await validate_application_contract(conn, state)
    return count


def test_validation_statement_count_is_bounded_on_every_identity(monkeypatch):
    async def check(owner, pools, state):
        counts = []
        async with owner.connection() as conn:
            counts.append(await _counted_validation(conn, state, monkeypatch))
        for pool in (pools.control, pools.runtime):
            async with pool.connection() as conn:
                counts.append(await _counted_validation(conn, state, monkeypatch))
        assert len(set(counts)) == 1, counts
        assert 0 < counts[0] <= MAX_VALIDATION_STATEMENTS, counts
    asyncio.run(_scenario(check))


def test_batched_rights_checks_reject_each_drift_with_its_exact_cause():
    async def check(owner, pools, state):
        async with owner.connection() as conn:
            statements = [(statement.format(database=conn.info.dbname), cause) for statement, cause in DRIFTS]
            found = {}
            for statement, _ in statements:
                try:
                    async with conn.transaction(force_rollback=True):
                        await conn.execute(statement)
                        await validate_application_contract(conn, state)
                    found[statement] = None
                except RoleSetupError as exc:
                    found[statement] = str(exc)
            assert found == {statement: PREFIX + cause for statement, cause in statements}
            await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


def test_restricted_identities_see_the_same_batched_drift():
    async def check(owner, pools, state):
        drift = "GRANT USAGE ON SEQUENCE public.accounts_id_seq TO odograph_control"
        async with owner.connection() as conn:
            await conn.execute(drift)
        try:
            for pool in (pools.control, pools.runtime):
                async with pool.connection() as conn:
                    with pytest.raises(RoleSetupError) as raised:
                        await validate_application_contract(conn, state)
                    assert str(raised.value) == (
                        PREFIX + "sequence privilege: odograph_control public.accounts_id_seq ['USAGE']")
        finally:
            async with owner.connection() as conn:
                await conn.execute("REVOKE USAGE ON SEQUENCE public.accounts_id_seq FROM odograph_control")
        for pool in (pools.control, pools.runtime):
            async with pool.connection() as conn:
                await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))


# Each batched statement sees a new snapshot. An object renamed away by
# another session between two statements must fail the validation with its
# own cause, never read as an empty, passing result.
VANISHING = (
    ("has_table_privilege",
     "ALTER TABLE public.reference_mileage_rates RENAME TO {moved}",
     "ALTER TABLE public.{moved} RENAME TO reference_mileage_rates",
     "table privilege: odograph_control public.reference_mileage_rates "
     "['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']"),
    ("has_column_privilege",
     "ALTER TABLE public.reference_mileage_rates RENAME TO {moved}",
     "ALTER TABLE public.{moved} RENAME TO reference_mileage_rates",
     "column privilege: odograph_control public.reference_mileage_rates missing"),
    ("has_sequence_privilege",
     "ALTER SEQUENCE public.trips_id_seq RENAME TO {moved}",
     "ALTER SEQUENCE public.{moved} RENAME TO trips_id_seq",
     "sequence privilege: odograph_control public.trips_id_seq ['USAGE', 'SELECT', 'UPDATE']"),
    # Signature resolution may still use this session's cached catalog for
    # the rest of the statement batch; the definition check then refuses it.
    ("CROSS JOIN unnest(%s::text[]) f",
     "ALTER FUNCTION public.host_reset_password(bigint,text) RENAME TO {moved}",
     "ALTER FUNCTION public.{moved}(bigint,text) RENAME TO host_reset_password",
     ("function privilege: odograph_control public.host_reset_password(bigint,text)",
      "function definition: public.host_reset_password(bigint,text)")),
    ("s(signature)",
     "ALTER FUNCTION public.host_reset_password(bigint,text) RENAME TO {moved}",
     "ALTER FUNCTION public.{moved}(bigint,text) RENAME TO host_reset_password",
     "function definition: public.host_reset_password(bigint,text)"),
)


@pytest.mark.parametrize("marker,move,restore,cause", VANISHING,
                         ids=("table", "column", "sequence", "function", "definition"))
def test_object_vanishing_between_batched_reads_fails_closed(monkeypatch, marker, move, restore, cause):
    moved = "contract_vanished"

    async def check(owner, pools, state):
        execute = psycopg.AsyncCursor.execute
        moved_away = []

        async def interleaved(self, query, *args, **kwargs):
            if not moved_away and marker in str(query):
                async with await psycopg.AsyncConnection.connect(TEST_DB, autocommit=True) as other:
                    await other.execute("SET lock_timeout = '5s'")
                    await other.execute(move.format(moved=moved))
                moved_away.append(True)
            return await execute(self, query, *args, **kwargs)

        try:
            async with owner.connection() as conn:
                with monkeypatch.context() as patch:
                    patch.setattr(psycopg.AsyncCursor, "execute", interleaved)
                    with pytest.raises(RoleSetupError) as raised:
                        await validate_application_contract(conn, state)
            assert moved_away == [True]
            causes = cause if isinstance(cause, tuple) else (cause,)
            assert str(raised.value) in {PREFIX + item for item in causes}
        finally:
            if moved_away:
                async with owner.connection() as conn:
                    await conn.execute(restore.format(moved=moved))
        async with owner.connection() as conn:
            await validate_application_contract(conn, state)
    asyncio.run(_scenario(check))
