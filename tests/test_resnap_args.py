"""`python -m app.resnap` argument parsing."""
from __future__ import annotations

import pytest

import app.resnap as resnap


def test_arguments_are_parsed():
    args = resnap._parser().parse_args(["--account", "3", "--device", "7"])
    assert (args.account, args.device) == (3, 7)
    assert resnap._parser().parse_args(["--account", "3"]).device is None


@pytest.mark.parametrize("argv", [
    [], ["--device", "1"], ["--account", "0"], ["--account", "abc"], ["--account", "-1"],
    ["--account", "1", "--device", "0"], ["--account", str(2**63)], ["--account", "1\u0663"],
])
def test_invalid_arguments_exit_2_without_echoing_input(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        resnap.main(argv)
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "error: invalid arguments" in err and "abc" not in err


def test_declined_and_dry_run_are_parsed():
    args = resnap._parser().parse_args(["--account", "3", "--declined", "--dry-run", "--device", "7"])
    assert (args.account, args.device, args.declined, args.dry_run) == (3, 7, True, True)
    args = resnap._parser().parse_args(["--account", "3"])
    assert (args.declined, args.dry_run) == (False, False)


def test_dry_run_without_declined_is_refused(capsys, monkeypatch):
    # Refused before any database setting is read.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert resnap.main(["--account", "3", "--dry-run"]) == 2
    assert "error: --dry-run requires --declined" in capsys.readouterr().err


def test_declined_predicate_uses_the_snap_ceiling():
    from app.snap import SNAP_MAX_COVERAGE
    assert resnap.SNAP_MAX_COVERAGE is SNAP_MAX_COVERAGE
    assert "NOT t.has_gap" in resnap._DECLINED_PREDICATE
