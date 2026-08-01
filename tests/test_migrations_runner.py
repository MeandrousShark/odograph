"""Non-DB tests for app.db.run_migrations's filename validation.

Validation runs before any pool connection is opened, so this can be
exercised without a real database: a `FakePool` whose `.connection()` would
raise if ever called proves nothing was applied.
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import run_migrations


class FakePool:
    def connection(self):
        raise AssertionError("run_migrations touched the pool before validating filenames")


def test_bad_migration_filename_rejected_before_applying_anything(tmp_path, monkeypatch):
    (tmp_path / "001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "not_numbered.sql").write_text("SELECT 1;")

    import app.db as db_module

    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", tmp_path)

    with pytest.raises(ValueError, match="not_numbered.sql"):
        asyncio.run(run_migrations(FakePool()))
