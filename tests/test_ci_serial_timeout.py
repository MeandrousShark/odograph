"""Keeps the serial full-suite CI jobs' timeout-minutes ahead of test growth.

release.yml `test` and release-preflight.yml `quality` run the whole suite in
one pytest process and only run on a version change or a tag, so a stale
timeout would otherwise surface on release day. The required timeout is
derived from the collected case counts, so adding tests moves the budget.
"""
from __future__ import annotations

from math import ceil
from pathlib import Path
import re
import shlex

import pytest
import yaml

from test_ci_workflow import _collect


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Calibrated from the v1.0.0 release preflight: 1,155 db and 3,247 other cases,
# suite 1,138 s; these constants estimate 1,165 s. Recheck them when a release
# run's measured time differs from the estimate by more than 20 percent.
DB_SECONDS = 0.7
OTHER_SECONDS = 0.11
OVERHEAD_MINUTES = 3
HEADROOM = 2.0

# Jobs that run pytest without a selection and must stay inside the budget.
GUARDED_JOBS = {("release.yml", "test"), ("release-preflight.yml", "quality")}
# Jobs that run pytest sharded by marker; they fail loudly on every PR.
SHARDED_JOBS = {("test.yml", "suite")}

# GitHub's default when a job sets no timeout-minutes.
DEFAULT_TIMEOUT_MINUTES = 360
SHARD_VARIABLES = ("PYTEST_MARKER", "ODOGRAPH_DB_SHARD")
# Options that narrow what pytest runs, plus anything that looks like a path.
SELECTION_OPTION = re.compile(r"^(-m|-k|--deselect|--ignore|--lf|--last-failed|--sw|--stepwise)")
PATH_ARGUMENT = re.compile(r"(::|\.py\b|(^|/)tests?(/|$))")


def estimate_minutes(db_cases: int, other_cases: int) -> float:
    seconds = db_cases * DB_SECONDS + other_cases * OTHER_SECONDS
    return seconds / 60 + OVERHEAD_MINUTES


def required_timeout(db_cases: int, other_cases: int) -> int:
    return ceil(HEADROOM * estimate_minutes(db_cases, other_cases))


def load_workflows() -> dict[str, dict]:
    return {
        path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for path in sorted(WORKFLOWS.glob("*.yml"))
    }


def pytest_invocations(script: str) -> list[list[str]]:
    """Return the arguments of every pytest command in a shell `run` script."""
    invocations = []
    for line in script.replace("\\\n", " ").splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = line.split()
        segment: list[str] = []
        for index, token in enumerate(tokens):
            if set(token) <= set("();<>|&"):
                segment = []
                continue
            segment.append(token)
            is_pytest = token == "pytest" or token.endswith("/pytest")
            if not is_pytest or "install" in segment:
                continue
            args = []
            for argument in tokens[index + 1:]:
                if set(argument) <= set("();<>|&"):
                    break
                args.append(argument)
            invocations.append(args)
    return invocations


def job_pytest_invocations(job: dict) -> list[list[str]]:
    return [
        args
        for step in job.get("steps", [])
        for args in pytest_invocations(step.get("run", ""))
    ]


def is_unselected(args: list[str]) -> bool:
    return not any(SELECTION_OPTION.match(arg) or PATH_ARGUMENT.search(arg) for arg in args)


def job_kind(job: dict) -> str | None:
    """None without pytest; "serial" for an unselected, unsharded run; else "other"."""
    invocations = job_pytest_invocations(job)
    if not invocations:
        return None
    sharded = any(name in repr(job) for name in SHARD_VARIABLES)
    if not sharded and any(is_unselected(args) for args in invocations):
        return "serial"
    return "other"


def check_workflows(workflows: dict[str, dict], db_cases: int, other_cases: int) -> list[str]:
    """Return one message per classification or timeout problem."""
    problems = []
    serial = set()
    for filename, workflow in workflows.items():
        for name, job in workflow.get("jobs", {}).items():
            kind = job_kind(job)
            key = (filename, name)
            if kind is None or key in SHARDED_JOBS:
                continue
            if kind == "serial" and key in GUARDED_JOBS:
                serial.add(key)
            elif kind == "serial":
                problems.append(
                    f"{filename} job {name!r} runs the full suite serially but is not in "
                    "GUARDED_JOBS; add it there so it is held to the timeout budget, "
                    "or shard the job and list it in SHARDED_JOBS")
            else:
                problems.append(
                    f"{filename} job {name!r} runs pytest with a selection or shard "
                    "variable but is not in SHARDED_JOBS; add it there or run the "
                    "unselected suite in a guarded job")
    for key in sorted(GUARDED_JOBS - serial):
        problems.append(f"guarded job {key[0]} {key[1]!r} no longer runs the serial full suite; "
                        "update GUARDED_JOBS")
    estimate = estimate_minutes(db_cases, other_cases)
    required = required_timeout(db_cases, other_cases)
    for filename, name in sorted(serial):
        raw = workflows[filename]["jobs"][name].get("timeout-minutes", DEFAULT_TIMEOUT_MINUTES)
        try:
            timeout = int(raw)
        except ValueError:
            problems.append(f"{filename} job {name!r} has a non-numeric timeout-minutes {raw!r}")
            continue
        if timeout < required:
            problems.append(
                f"{filename} job {name!r} has timeout-minutes {timeout}, but the serial suite "
                f"({db_cases} db and {other_cases} other cases) is estimated at {estimate:.1f} "
                f"minutes and needs at least {required} ({HEADROOM}x headroom). Raise that "
                f"job's timeout-minutes in .github/workflows/{filename} or shard the job.")
    return problems


def serial_job(timeout: str, command: str = "python -m pytest -q") -> dict:
    return {"timeout-minutes": timeout, "steps": [{"run": f"pip install -r x\n{command}\n"}]}


def guarded_workflows(timeout: str) -> dict[str, dict]:
    return {
        "release.yml": {"jobs": {"test": serial_job(timeout)}},
        "release-preflight.yml": {"jobs": {"quality": serial_job(timeout, "python -m pytest")}},
        "test.yml": {"jobs": {"suite": {"timeout-minutes": "20", "steps": [{
            "run": 'python -m pytest -m "$PYTEST_MARKER" -vv > "$RUNNER_TEMP/pytest-shard.log" 2>&1',
        }]}}},
    }


def test_serial_budget_matches_the_v1_0_0_calibration():
    assert estimate_minutes(1155, 3247) * 60 - OVERHEAD_MINUTES * 60 == pytest.approx(1165, abs=1)
    assert required_timeout(1155, 3247) == 45
    assert required_timeout(0, 0) == 6


def test_real_workflows_guard_exactly_the_serial_suite_jobs():
    workflows = load_workflows()
    kinds = {
        (filename, name): job_kind(job)
        for filename, workflow in workflows.items()
        for name, job in workflow["jobs"].items()
        if job_kind(job)
    }

    assert {key for key, kind in kinds.items() if kind == "serial"} == GUARDED_JOBS
    assert {key for key, kind in kinds.items() if kind == "other"} == SHARDED_JOBS


def test_real_serial_jobs_fit_the_collected_suite():
    db_cases = len(_collect("db", ""))
    other_cases = len(_collect("not db", ""))
    assert db_cases and other_cases

    assert check_workflows(load_workflows(), db_cases, other_cases) == []


def test_stale_timeout_fails_naming_job_estimate_and_remedy():
    problems = check_workflows(guarded_workflows("20"), 1187, 3334)

    assert len(problems) == 2
    message = problems[0]
    assert "release-preflight.yml job 'quality'" in message or "release.yml job 'test'" in message
    assert "timeout-minutes 20" in message
    assert "estimated at 23.0 minutes" in message
    assert "at least 46" in message
    assert "Raise that job's timeout-minutes" in message and "shard the job" in message
    assert check_workflows(guarded_workflows("46"), 1187, 3334) == []
    assert len(check_workflows(guarded_workflows("45"), 1187, 3334)) == 2


def test_injected_counts_fail_without_collecting():
    problems = check_workflows(guarded_workflows("50"), 8000, 3334)

    assert len(problems) == 2
    assert all("8000 db" in problem for problem in problems)


def test_unlisted_unmarked_pytest_job_fails_classification():
    workflows = guarded_workflows("50")
    workflows["extra.yml"] = {"jobs": {"checks": serial_job("50", "pytest -q | tee out.txt")}}

    [problem] = check_workflows(workflows, 1187, 3334)

    assert "extra.yml job 'checks'" in problem and "GUARDED_JOBS" in problem


def test_unlisted_selected_pytest_job_fails_classification():
    workflows = guarded_workflows("50")
    workflows["extra.yml"] = {"jobs": {"checks": serial_job("50", "python -m pytest -m ops")}}

    [problem] = check_workflows(workflows, 1187, 3334)

    assert "extra.yml job 'checks'" in problem and "SHARDED_JOBS" in problem


def test_missing_guarded_job_fails_instead_of_passing_vacuously():
    workflows = guarded_workflows("50")
    del workflows["release.yml"]

    [problem] = check_workflows(workflows, 1187, 3334)

    assert "release.yml" in problem and "no longer runs" in problem


def test_sharded_suite_job_is_not_held_to_the_serial_budget():
    workflows = guarded_workflows("50")
    workflows["test.yml"]["jobs"]["suite"]["timeout-minutes"] = "1"

    assert check_workflows(workflows, 1187, 3334) == []


@pytest.mark.parametrize("script, expected", [
    ("python -m pytest -q | tee out.txt", [["-q"]]),
    ("pytest", [[]]),
    ("timeout 15m python -m pytest -m db -vv \\\n  > log 2>&1", [["-m", "db", "-vv"]]),
    ("python -m pip install pytest\ncat pytest-shard.log", []),
    ("python -m pytest -o faulthandler_timeout=120 && echo done", [["-o", "faulthandler_timeout=120"]]),
])
def test_pytest_invocations_reads_shell_commands(script, expected):
    assert pytest_invocations(script) == expected


@pytest.mark.parametrize("args, unselected", [
    ([], True), (["-q"], True), (["-o", "faulthandler_timeout=120"], True),
    (["-m", "db"], False), (["-mdb"], False), (["-k", "x"], False),
    (["tests/test_a.py"], False), (["tests"], False), (["--deselect", "a"], False),
])
def test_selection_detection(args, unselected):
    assert is_unselected(args) is unselected
