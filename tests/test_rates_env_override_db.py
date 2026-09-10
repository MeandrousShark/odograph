"""DB-backed tests for `load_rates`'s MILEAGE_RATE_<YEAR> env override: a
non-finite or non-positive override must be ignored (and the DB rate, if
any, kept) rather than installing a NaN/Infinity YearRate.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.db import make_pool
from app.rates import ENV_PREFIX, load_rates
from conftest import reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests")


@pytest.mark.parametrize("bad_value", ["nan", "1e400", "-inf", "0", "-5"])
def test_bad_env_override_is_ignored_db_rate_kept(monkeypatch, bad_value):
    asyncio.run(_bad_override_scenario(monkeypatch, bad_value))


async def _bad_override_scenario(monkeypatch, bad_value):
    monkeypatch.setenv(f"{ENV_PREFIX}2026", bad_value)
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            rates = await load_rates(conn)
        # 2026 is seeded at 0.7250 by migrations/002_mileage_rates.sql; the
        # bad override must not overwrite it.
        assert rates[2026].rate_per_mi == pytest.approx(0.7250)
    finally:
        await pool.close()


def test_valid_env_override_replaces_db_rate(monkeypatch):
    asyncio.run(_valid_override_scenario(monkeypatch))


async def _valid_override_scenario(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}2026", "0.7")
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await reset_db(pool)
        async with pool.connection() as conn:
            rates = await load_rates(conn)
        assert rates[2026].rate_per_mi == pytest.approx(0.7)
        assert rates[2026].rate_h2_per_mi is None
    finally:
        await pool.close()
