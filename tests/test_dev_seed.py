"""Calendar logic of scripts/dev_seed.py, with no database: the `week`
scenario must target the last complete Monday-to-Sunday Pacific week no
matter which weekday the seed runs on."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.dashboard import week_bounds
from scripts import dev_seed
from scripts.dev_seed import PACIFIC, build_all_points, parse_args, target_week

# Two weeks of "now" around the fall-back change and the year boundary, so
# every weekday and both unusual week shapes are covered.
_DAYS = [date(2026, 10, 26) + timedelta(days=i) for i in range(15)] + [
    date(2026, 12, 28) + timedelta(days=i) for i in range(15)
]


def _now(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=PACIFIC)


@pytest.mark.parametrize("day", _DAYS)
def test_target_week_is_the_previous_complete_week(day):
    now = _now(day)
    week = target_week(now)

    assert week.monday.weekday() == 0
    assert week.end == week_bounds(day, PACIFIC).start
    assert week.end - week.start >= timedelta(days=6, hours=23)


@pytest.mark.parametrize("day", _DAYS)
def test_week_scenario_drives_every_day_of_the_target_week_and_nothing_else(day):
    now = _now(day)
    week = target_week(now)
    default_points = build_all_points(now)
    points = build_all_points(now, "week")

    in_week = [p for p in points if week.start <= p.t < week.end]
    days = {p.t.astimezone(PACIFIC).date() for p in in_week}
    assert days == {week.monday + timedelta(days=i) for i in range(7)}
    assert all(p.t < now for p in in_week)

    # A default driving day that would land in the week is moved, not dropped.
    assert len(points) - len(in_week) == len(default_points)


def test_scenario_argument_is_restricted_to_known_values():
    args = ["--database-url", "postgresql://x@127.0.0.1/y", "--wipe"]
    assert parse_args(args).scenario is None
    assert parse_args([*args, "--scenario", "week"]).scenario == "week"
    with pytest.raises(SystemExit):
        parse_args([*args, "--scenario", "month"])


def _run_seed_main(monkeypatch, capsys) -> list[str]:
    async def seeded(database_url, scenario):
        print("trips by source/category (count, total points):")

    monkeypatch.setattr(dev_seed, "main_async", seeded)
    assert dev_seed.main(["--database-url", "postgresql://u:p@127.0.0.1:5432/x", "--wipe"]) == 0
    return capsys.readouterr().out.splitlines()


def test_demo_mode_ends_the_seed_output_with_the_notice_and_url(monkeypatch, capsys):
    monkeypatch.setenv("DEMO_MODE", "1")

    assert _run_seed_main(monkeypatch, capsys)[-2:] == [
        "Demo mode: synthetic data, not persistent, authentication disabled.",
        "http://127.0.0.1:8079",
    ]


def test_other_seed_runs_do_not_claim_the_trial_url(monkeypatch, capsys):
    monkeypatch.delenv("DEMO_MODE", raising=False)

    output = _run_seed_main(monkeypatch, capsys)

    assert not any("Demo mode" in line or "8079" in line for line in output)


def test_demo_mode_does_not_relax_the_loopback_guard(monkeypatch, capsys):
    monkeypatch.setenv("DEMO_MODE", "1")

    assert dev_seed.main(["--database-url", "postgresql://u:p@db:5432/x", "--wipe"]) == 1
    assert "refusing --database-url" in capsys.readouterr().err
