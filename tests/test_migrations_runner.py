"""Non-DB tests for app.db.run_migrations's filename validation.

Validation runs before any pool connection is opened, so this can be
exercised without a real database: a `FakePool` whose `.connection()` would
raise if ever called proves nothing was applied.
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import (
    DETECTOR_ADVISORY_LOCK_KEY,
    EMAIL_DIGEST_ADVISORY_LOCK_KEY,
    NUDGE_ADVISORY_LOCK_KEY,
    ODOMETER_REMINDER_ADVISORY_LOCK_KEY,
    RUN_MIGRATIONS_ADVISORY_LOCK_KEY,
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


def test_bad_migration_filename_rejected_before_applying_anything(tmp_path, monkeypatch):
    (tmp_path / "001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "not_numbered.sql").write_text("SELECT 1;")

    import app.db as db_module

    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", tmp_path)

    with pytest.raises(ValueError, match="not_numbered.sql"):
        asyncio.run(run_migrations(FakePool()))
