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
