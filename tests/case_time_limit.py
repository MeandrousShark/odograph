"""Per-case hang limit: dump every thread's stack and exit the run.

One timer covers a case's setup, call and teardown together, so a hang shows
where it is stuck in seconds instead of when an outer timeout kills the run.
The exit is immediate, so a hung case leaves no timing record.

faulthandler has one pending timer, which pytest's own faulthandler_timeout
also uses; that option is refused rather than letting one silently replace
the other.
"""
from __future__ import annotations

import faulthandler
import os
import sys

import pytest

LIMIT_SECONDS = 60.0
# Only for the test that exercises the limit.
_ENV_LIMIT = "ODOGRAPH_CASE_TIME_LIMIT"


def _stderr_fileno() -> int:
    """Mirror pytest's faulthandler plugin: sys.stderr may not be a real file."""
    try:
        fileno = sys.stderr.fileno()
        if fileno == -1:
            raise AttributeError()
        return fileno
    except (AttributeError, ValueError):
        assert sys.__stderr__ is not None
        return sys.__stderr__.fileno()


class CaseTimeLimit:
    def __init__(self, seconds: float, fileno: int):
        self.seconds = seconds
        self.fileno = fileno

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        faulthandler.dump_traceback_later(self.seconds, exit=True, file=self.fileno)
        try:
            return (yield)
        finally:
            faulthandler.cancel_dump_traceback_later()

    def pytest_unconfigure(self, config):
        os.close(self.fileno)


def activate(config) -> None:
    try:
        pytest_timeout = float(config.getini("faulthandler_timeout") or 0.0)
    except ValueError:
        # Unknown when pytest's faulthandler plugin is disabled.
        pytest_timeout = 0.0
    if pytest_timeout > 0:
        raise pytest.UsageError(
            "faulthandler_timeout would replace the per-case hang limit in "
            "tests/case_time_limit.py (faulthandler has one timer); unset it")
    value = os.environ.get(_ENV_LIMIT, "")
    try:
        seconds = float(value) if value else LIMIT_SECONDS
    except ValueError:
        seconds = 0.0
    if not seconds > 0:
        raise pytest.UsageError(f"{_ENV_LIMIT} must be a positive number of seconds, got {value!r}")
    # Configure runs while capture is suspended, so this duplicates the real
    # stderr and the dump survives fd capture during the case.
    fileno = os.dup(_stderr_fileno())
    config.pluginmanager.register(CaseTimeLimit(seconds, fileno), "odograph-case-time-limit")
