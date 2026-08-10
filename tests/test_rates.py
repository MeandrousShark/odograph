"""Tests for IRS mileage-rate lookup/deduction math."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.rates import YearRate, deduction, rate_for

RATES = {2025: YearRate(0.7000), 2026: YearRate(0.7250)}


def test_exact_year_lookup():
    assert rate_for(RATES, 2026) == 0.7250


def test_fallback_to_most_recent_earlier_year():
    # January before the new year's IRS notice: only last year's rate exists yet.
    assert rate_for({2025: YearRate(0.70)}, 2026) == 0.70


def test_fallback_skips_over_future_years():
    rates = {2020: YearRate(0.575), 2025: YearRate(0.70), 2026: YearRate(0.725)}
    assert rate_for(rates, 2027) == 0.725


def test_no_rate_available():
    assert rate_for({}, 2026) is None
    assert rate_for({2027: YearRate(0.75)}, 2026) is None  # only a later year on file


def test_deduction_math():
    # One mile at the 2025 rate.
    assert deduction(1609.344, 2025, RATES) == pytest.approx(0.70)


def test_deduction_none_when_no_rate():
    assert deduction(1609.344, 2020, RATES) is None


def test_midyear_split_prices_each_half_at_its_own_rate():
    # 2022 style: 58.5c/mi Jan-Jun, 62.5c/mi from Jul 1.
    rates = {2022: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    assert rate_for(rates, 2022, 1) == 0.585
    assert rate_for(rates, 2022, 6) == 0.585
    assert rate_for(rates, 2022, 7) == 0.625
    assert rate_for(rates, 2022, 12) == 0.625


def test_deduction_default_month_uses_first_half_rate():
    rates = {2022: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    assert deduction(1609.344, 2022, rates) == pytest.approx(0.585)
    assert deduction(1609.344, 2022, rates, month=8) == pytest.approx(0.625)


def test_fallback_uses_prior_year_latest_rate():
    # Jan of a new year before its IRS notice, when the prior year had a
    # mid-year bump: the prior year's second-half rate is the best proxy.
    rates = {2022: YearRate(0.585, rate_h2_per_mi=0.625, h2_start_month=7)}
    assert rate_for(rates, 2023, 1) == 0.625


def test_year_attribution_at_utc_local_boundary():
    # 2026-01-01 00:30 UTC is still 2025-12-31 16:30 in Los Angeles, so the
    # trip must be attributed to 2025 (and priced at the 2025 rate), not
    # 2026, so month/YTD bucketing must use the local year, not UTC's.
    tz = ZoneInfo("America/Los_Angeles")
    started_at_utc = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    local_year = started_at_utc.astimezone(tz).year
    assert local_year == 2025
    assert rate_for(RATES, local_year) == 0.70
