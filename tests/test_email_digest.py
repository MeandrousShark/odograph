"""Pure-function tests for the email digest boundary computations and
template rendering (app/email_digest.py). DB-backed worker/ledger behavior
lives in tests/test_email_digest_db.py, mirroring the nudge/odometer split.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.email_digest import (
    EmailDigestWorker,
    _parse_mmdd,
    _render,
    _url,
    covered_month,
    latest_filing_reminder_at,
    latest_month_boundary,
)

TZ = ZoneInfo("America/Los_Angeles")


def _worker(**overrides) -> EmailDigestWorker:
    defaults = dict(
        pool=None, mailer=None, app_url="", display_tz=TZ,
        nudge_weekly_hour=18, odometer_reminder_hour=9, digest_hour=9,
        filing_reminder_mmdd="01-15",
        email_weekly_nudge=True, email_monthly_summary=True,
        email_filing_reminder=True, email_odometer_reminder=True,
    )
    defaults.update(overrides)
    return EmailDigestWorker(**defaults)


def test_guarded_records_a_per_kind_failure_onto_status_without_raising():
    # `_guarded`'s whole point is to keep one kind's exception from stopping
    # the others (see its docstring) -- which also means it never reaches
    # `IntervalWorker._run_guarded()`'s except clause, so it has to record
    # the failure onto `status` itself or diagnostics would never see it.
    worker = _worker()

    async def failing(now):
        raise RuntimeError("smtp down")

    asyncio.run(worker._guarded("weekly_nudge", failing, datetime.now(TZ)))
    assert worker.status.last_failure_type == "RuntimeError"
    assert worker.status.last_failure_at is not None


def test_guarded_leaves_status_untouched_on_success():
    worker = _worker()

    async def ok(now):
        return None

    asyncio.run(worker._guarded("weekly_nudge", ok, datetime.now(TZ)))
    assert worker.status.last_failure_type is None
    assert worker.status.last_failure_at is None


def test_latest_month_boundary_uses_current_month_after_the_hour():
    now = datetime(2026, 7, 1, 9, 30, tzinfo=TZ)
    assert latest_month_boundary(now, 9) == datetime(2026, 7, 1, 9, tzinfo=TZ)


def test_latest_month_boundary_uses_prior_month_before_the_hour():
    now = datetime(2026, 7, 1, 8, 59, tzinfo=TZ)
    assert latest_month_boundary(now, 9) == datetime(2026, 6, 1, 9, tzinfo=TZ)


def test_latest_month_boundary_mid_month_uses_this_months_start():
    now = datetime(2026, 7, 15, 12, tzinfo=TZ)
    assert latest_month_boundary(now, 9) == datetime(2026, 7, 1, 9, tzinfo=TZ)


def test_latest_month_boundary_handles_january_year_rollover():
    now = datetime(2026, 1, 1, 8, tzinfo=TZ)
    assert latest_month_boundary(now, 9) == datetime(2025, 12, 1, 9, tzinfo=TZ)


def test_latest_month_boundary_requires_timezone_aware_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_month_boundary(datetime(2026, 7, 1, 9), 9)


def test_covered_month_is_the_month_before_the_boundary():
    assert covered_month(datetime(2026, 8, 1, 9, tzinfo=TZ)) == (2026, 7)


def test_covered_month_handles_january_boundary_as_prior_december():
    assert covered_month(datetime(2027, 1, 1, 9, tzinfo=TZ)) == (2026, 12)


def test_parse_mmdd_reads_month_and_day():
    assert _parse_mmdd("01-15") == (1, 15)
    assert _parse_mmdd("12-31") == (12, 31)


@pytest.mark.parametrize("mmdd", ["13-01", "01-32", "0", "01", "ab-cd"])
def test_parse_mmdd_rejects_invalid_input(mmdd):
    with pytest.raises(ValueError):
        _parse_mmdd(mmdd)


def test_latest_filing_reminder_at_uses_this_years_boundary_after_it():
    now = datetime(2027, 1, 15, 10, tzinfo=TZ)
    assert latest_filing_reminder_at(now, "01-15", 9) == datetime(2027, 1, 15, 9, tzinfo=TZ)


def test_latest_filing_reminder_at_uses_last_years_boundary_before_it():
    now = datetime(2027, 1, 14, 23, tzinfo=TZ)
    assert latest_filing_reminder_at(now, "01-15", 9) == datetime(2026, 1, 15, 9, tzinfo=TZ)


def test_latest_filing_reminder_at_handles_year_boundary_mmdd():
    # A December MM-DD boundary is still "this year" once now has passed
    # it -- exercises the same month-rollover math from the other side.
    now = datetime(2026, 12, 31, 10, tzinfo=TZ)
    assert latest_filing_reminder_at(now, "12-30", 9) == datetime(2026, 12, 30, 9, tzinfo=TZ)


def test_latest_filing_reminder_at_requires_timezone_aware_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_filing_reminder_at(datetime(2027, 1, 15, 9), "01-15", 9)


def test_url_is_empty_when_app_url_unset():
    assert _url("", "/review") == ""


def test_url_joins_app_url_and_path():
    assert _url("https://miles.example.com", "/review") == "https://miles.example.com/review"


def test_weekly_nudge_template_omits_link_when_review_url_empty():
    body = _render("weekly_nudge.txt", count=2, noun="trips", review_url="")
    assert "2 unclassified trips in the past week" in body
    assert "http" not in body


def test_weekly_nudge_template_includes_link_when_review_url_set():
    body = _render(
        "weekly_nudge.txt", count=1, noun="trip",
        review_url="https://miles.example.com/review",
    )
    assert "https://miles.example.com/review" in body


def test_monthly_summary_template_includes_figures_and_link():
    body = _render(
        "monthly_summary.txt", month_label="Jul 2026", business_mi="12.3",
        deduction="$8.61", unclassified=1,
        report_url="https://miles.example.com/report/range?from=2026-07-01&to=2026-07-31",
    )
    assert "Jul 2026 summary" in body
    assert "Business miles: 12.3" in body
    assert "Deduction: $8.61" in body
    assert "Unclassified trips: 1" in body
    assert "https://miles.example.com/report/range" in body


def test_filing_reminder_template_has_both_links_and_no_attachment_reference():
    body = _render(
        "filing_reminder.txt", year=2026, business_mi="1000.0", deduction="$680.00",
        report_url="https://miles.example.com/report/2026",
        export_url="https://miles.example.com/report/2026/export",
    )
    assert "2026 filing reminder" in body
    assert "https://miles.example.com/report/2026" in body
    assert "https://miles.example.com/report/2026/export" in body
    assert "attach" not in body.lower()


def test_quarterly_odometer_template_names_vehicles():
    body = _render(
        "quarterly_odometer.txt", vehicles="Truck, Sedan", noun="vehicles",
        settings_url="https://miles.example.com/settings",
    )
    assert "Truck, Sedan (vehicles)" in body
    assert "https://miles.example.com/settings" in body
