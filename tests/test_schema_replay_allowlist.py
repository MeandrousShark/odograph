"""Pins the test modules allowed to drop or replay the test schema.

A drop and replay of every migration costs several times a reset_db()
truncate, and copy-pasted helpers that replayed before every case once
dominated the db tier. Ordinary tests use reset_db() and the provisioned role
pools. A module that tests migrations, role provisioning or committed schema
drift is added here deliberately, and must leave the schema migrated and
provisioned when it finishes, for example through the `restores_test_schema`
fixture.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPLAY = re.compile(r"\b(full_schema_reset|drop_and_recreate_schema|restores?_test_schema)")

_SCHEMA_REPLAY_MODULE_STEMS = {
    "test_account_avatar_db",  # migration 024 over a partial schema
    "test_account_context_db",  # drops the cluster-wide roles it shares
    "test_accounts_db",  # migration 020 over a partial schema
    "test_application_cluster_roles_db",  # role-free schema for a second install
    "test_application_roles_db",  # committed contract function drop
    "test_conftest_db",  # the reset machinery itself
    "test_email_change_integration_db",  # schema 29 upgrades
    "test_geocode_roles_db",  # committed worker contract drift
    "test_migration_027_db",
    "test_migrations_concurrency_db",
    "test_ownership_db",  # legacy schema 25 upgrades
    "test_storage_accounting_db",  # schema 39 forward backfill
    "test_storage_ceilings_db",  # schema 40 and 41 role contracts
    "test_storage_roles_db",  # committed accounting trigger drift
    "test_upgrade_contract_db",  # historical provisioned upgrades
}


def test_only_allowlisted_modules_drop_or_replay_the_schema():
    tests_dir = Path(__file__).resolve().parent
    callers = {
        path.stem for path in tests_dir.rglob("*.py")
        if path.name != "conftest.py" and path.resolve() != Path(__file__).resolve()
        and _REPLAY.search(path.read_text(encoding="utf-8"))
    }

    assert callers == _SCHEMA_REPLAY_MODULE_STEMS, (
        f"unexpected schema replay callers: {sorted(callers - _SCHEMA_REPLAY_MODULE_STEMS)}; "
        f"stale allowlist entries: {sorted(_SCHEMA_REPLAY_MODULE_STEMS - callers)}"
    )
