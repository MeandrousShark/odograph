"""Non-DB tests for app.db.run_migrations's filename validation.

Validation runs before any pool connection is opened, so this can be
exercised without a real database: a `FakePool` whose `.connection()` would
raise if ever called proves nothing was applied.
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import (
    DETECTOR_ACCOUNT_LOCK_CLASS_ID,
    DETECTOR_ADVISORY_LOCK_KEY,
    EMAIL_DIGEST_ADVISORY_LOCK_KEY,
    INVITATION_EMAIL_LOCK_CLASS_ID,
    NUDGE_ADVISORY_LOCK_KEY,
    ODOMETER_REMINDER_ADVISORY_LOCK_KEY,
    MIGRATIONS_DIR,
    RUN_MIGRATIONS_ADVISORY_LOCK_KEY,
    SchemaTooNewError,
    newest_migration_version,
    run_migrations,
)


class FakePool:
    def connection(self):
        raise AssertionError("run_migrations touched the pool before validating filenames")


def test_advisory_lock_registry_preserves_every_deployed_key():
    assert DETECTOR_ADVISORY_LOCK_KEY == 0x6D696C6531
    assert NUDGE_ADVISORY_LOCK_KEY == 901405
    assert ODOMETER_REMINDER_ADVISORY_LOCK_KEY == 901406
    assert EMAIL_DIGEST_ADVISORY_LOCK_KEY == 901407
    assert RUN_MIGRATIONS_ADVISORY_LOCK_KEY == 901408
    assert len({
        DETECTOR_ADVISORY_LOCK_KEY,
        NUDGE_ADVISORY_LOCK_KEY,
        ODOMETER_REMINDER_ADVISORY_LOCK_KEY,
        EMAIL_DIGEST_ADVISORY_LOCK_KEY,
        RUN_MIGRATIONS_ADVISORY_LOCK_KEY,
    }) == 5


def test_two_int_advisory_lock_classes_are_distinct():
    # 901411 and 901412 are taken in scripts/sql (OIDC attempts, lifecycle).
    assert INVITATION_EMAIL_LOCK_CLASS_ID == 901410
    assert DETECTOR_ACCOUNT_LOCK_CLASS_ID == 901413


def test_bad_migration_filename_rejected_before_applying_anything(tmp_path, monkeypatch):
    (tmp_path / "001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "not_numbered.sql").write_text("SELECT 1;")

    import app.db as db_module

    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", tmp_path)

    with pytest.raises(ValueError, match="not_numbered.sql"):
        asyncio.run(run_migrations(FakePool()))


def test_newest_migration_version_is_the_highest_number_not_the_last_name(tmp_path):
    for name in ("001_a.sql", "010_b.sql", "002_c.sql", "9_d.sql"):
        (tmp_path / name).write_text("SELECT 1;")

    assert newest_migration_version(sorted(tmp_path.glob("*.sql"))) == 10
    assert newest_migration_version([]) == 0


def test_newest_migration_version_matches_the_shipped_migrations():
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert newest_migration_version(paths) == max(int(p.name.split("_", 1)[0]) for p in paths)


def test_schema_too_new_message_names_both_versions_and_the_recovery():
    error = SchemaTooNewError(45, 44)

    assert (error.stored, error.newest) == (45, 44)
    assert str(error) == (
        "database schema version 45 is newer than this image's newest migration 44, "
        "so a newer release migrated it. Run the newer release that migrated this "
        "database, or restore the verified pre-upgrade backup under this image "
        "(see docs/upgrading.md, Rollback)"
    )
