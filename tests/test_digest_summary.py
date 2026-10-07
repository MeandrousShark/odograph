"""Exact digest scalar/body parity and ownership of streamed preparation."""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from psycopg.errors import QueryCanceled

from app.digest_summary import DigestSummary, owned_digest_turn
from app.email_digest import _render
from app.formatting import format_miles, format_usd
from app.rates import YearRate
from app.report import build_range_report

TZ = ZoneInfo("America/Los_Angeles")
START, END = date(2026, 1, 1), date(2026, 12, 31)


def exact(value):
    if value is None:
        return (type(None),)
    if math.isnan(value):
        return (float, "nan")
    return (float, value.hex())


def bodies(report, unclassified):
    common = dict(business_mi=format_miles(report.business_m),
                  nondeductible_mi=format_miles(report.nondeductible_m) if report.nondeductible_m else "",
                  deduction=format_usd(report.total_deduction), report_url="https://example.com/report")
    return (
        _render("monthly_summary.txt", **common, month_label="Jan 2026", unclassified=unclassified),
        _render("filing_reminder.txt", **common, year=2026, export_url="https://example.com/export"),
    )


@pytest.mark.parametrize("distances", [[0.0, -0.0], [1e30, 1.0, -1e30, 1e-30],
                                       [float("inf"), -float("inf"), float("nan")]])
@pytest.mark.parametrize("count", [0, 1, 255, 256, 257, 511, 512, 513])
@pytest.mark.parametrize("rates", [{}, {2026: YearRate(.67, .72, 7)},
                                  {2025: YearRate(.5, .6, 6), 2027: YearRate(99)},
                                  {2026: YearRate(float(Decimal("1e131071")))},
                                  {2026: YearRate(float("nan"))},
                                  {2026: YearRate(-float("inf"))},
                                  {2026: YearRate(-0.0)}])
def test_every_batch_split_matches_exact_oracle_and_complete_bodies(count, rates, distances):
    trips = []
    for index in range(count):
        trips.append(dict(started_at=datetime(2026, (index % 12) + 1, 15, tzinfo=TZ),
                          category=["business", "personal", "unclassified"][index % 3],
                          exclusion=[None, "not_deductible", "not_my_vehicle"][(index // 3) % 3],
                          display_distance_m=distances[(index // 9) % len(distances)]))
    oracle = build_range_report(trips, rates, TZ, START, END)
    rows = [(t["started_at"], t["category"], t["exclusion"], t["display_distance_m"]) for t in trips]
    for split in range(max(1, len(rows) + 1)):
        summary = DigestSummary()
        summary.fold(rows[:split], TZ, START, END)
        for offset in range(split, len(rows), 256):
            summary.fold(rows[offset:offset + 256], TZ, START, END)
        summary.finish(rates, 2026)
        assert exact(summary.business_m) == exact(oracle.business_m)
        assert exact(summary.nondeductible_m) == exact(oracle.nondeductible_m)
        assert exact(summary.total_deduction) == exact(oracle.total_deduction)
        assert summary.unclassified_trips == oracle.caveats.unclassified_trips
        assert len(summary.business_by_month) <= 12
        assert bodies(summary, summary.unclassified_trips) == bodies(oracle, oracle.caveats.unclassified_trips)


def test_local_boundaries_dst_unclassified_before_exclusion_and_zero_business():
    timestamps = [datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc),
                  datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
                  datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc),
                  datetime(2026, 3, 8, 10, tzinfo=timezone.utc),
                  datetime(2026, 11, 1, 8, 59, tzinfo=timezone.utc),
                  datetime(2026, 11, 1, 9, tzinfo=timezone.utc),
                  datetime(2027, 1, 1, 8, tzinfo=timezone.utc)]
    trips = [dict(started_at=t, category="business", display_distance_m=-0.0) for t in timestamps]
    trips.append(dict(started_at=timestamps[1], category="unclassified", exclusion="not_my_vehicle", display_distance_m=99))
    summary = DigestSummary()
    summary.fold([(t["started_at"], t["category"], t.get("exclusion"), t["display_distance_m"]) for t in trips], TZ, START, END)
    rates = {2025: YearRate(.4, .7, 7)}
    summary.finish(rates, 2026)
    oracle = build_range_report(trips, rates, TZ, START, END)
    assert summary.unclassified_trips == 1
    assert exact(summary.business_m) == exact(oracle.business_m)
    assert exact(summary.total_deduction) == exact(oracle.total_deduction)


def test_repeated_waiter_cancel_drains_query_and_finalizers():
    async def scenario():
        query_started, query_drained = asyncio.Event(), asyncio.Event()
        close_started, close_done = asyncio.Event(), asyncio.Event()
        rollback_started, rollback_done = asyncio.Event(), asyncio.Event()
        release_query, release_close, release_rollback = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def aggregate():
            query_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_query.wait()
                query_drained.set()
                raise
            finally:
                close_started.set()
                await release_close.wait()
                close_done.set()

        async def turn(control):
            try:
                control.active = asyncio.create_task(aggregate())
                await asyncio.shield(control.active)
            finally:
                rollback_started.set()
                await release_rollback.wait()
                rollback_done.set()

        waiter = asyncio.create_task(owned_digest_turn(turn))
        await query_started.wait()
        waiter.cancel()
        await asyncio.sleep(0)
        for _ in range(3):
            waiter.cancel()
            await asyncio.sleep(0)
        assert not waiter.done()
        release_query.set()
        await close_started.wait()
        assert query_drained.is_set()
        waiter.cancel()
        await asyncio.sleep(0)
        assert not close_done.is_set() and not waiter.done()
        release_close.set()
        await rollback_started.wait()
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done()
        release_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert close_done.is_set() and rollback_done.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", ["OperationalError", "InterfaceError"])
def test_uncertain_cleanup_retains_owner_and_lease_through_repeated_cancel(monkeypatch, caplog, error_type):
    import psycopg
    import app.digest_summary as module
    from app.account_context import AccountPrincipal
    from app.capacity import AdmissionManager

    async def scenario():
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        retained, release_fixture = asyncio.Event(), asyncio.Event()
        rotated = []

        async def retain():
            retained.set()
            await release_fixture.wait()

        monkeypatch.setattr(module, "_retain_unconfirmed_cleanup", retain)

        async def operation():
            async with manager.operation("background", principal):
                async with manager.lease([41]):
                    async def turn(control):
                        control.preparation_started = True
                        # A failed rollback wait may contain connection details.
                        raise getattr(psycopg, error_type)("recipient and row must never reach diagnostics")
                    await owned_digest_turn(turn)
                    rotated.append("next kind")

        waiter = asyncio.create_task(operation())
        await retained.wait()
        for _ in range(3):
            waiter.cancel()
            await asyncio.sleep(0)
        assert not waiter.done()
        assert len(manager._active["background"]) == 1
        assert len(manager._leases) == 1 and rotated == []
        assert error_type in caplog.text and "recipient and row" not in caplog.text
        # Only the injected fixture retention hook can release this test task.
        release_fixture.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not manager._active["background"] and not manager._leases
    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [ValueError, QueryCanceled])
def test_confirmed_rollback_after_ordinary_preparation_failure_releases_owner(error_type):
    from app.account_context import AccountPrincipal
    from app.capacity import AdmissionManager

    async def scenario():
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        rolled_back = []
        async with manager.operation("background", principal):
            async with manager.lease([41]):
                async def turn(control):
                    control.preparation_started = True
                    try:
                        raise error_type("preparation failed")
                    finally:
                        await asyncio.sleep(0)
                        rolled_back.append(True)
                with pytest.raises(error_type):
                    await owned_digest_turn(turn)
        assert rolled_back == [True]
        assert not manager._active["background"] and not manager._leases
    asyncio.run(scenario())
