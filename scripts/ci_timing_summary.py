#!/usr/bin/env python3
"""Summarize pytest timing records as Markdown for a CI step summary.

Reads the JSON lines tests/timing_records.py writes, one file per shard, from
the current run's directory and from zero or more baseline directories. Each
shard is compared against the newest baseline shard recorded by a push to
main with the same format, schema version and CPU model, because the same
shard's cost varies by about 1.6x across runner CPUs.

Reporting only: any error is written into the summary and the exit status is
always 0, so the summary can never fail a job.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MAIN_REF = "refs/heads/main"
TIERS = ("unit", "ops", "db")
SLOWEST_PER_TIER = 5
LARGEST_CHANGES = 10
MAX_PROBLEMS = 10
MAX_NODEID = 120
# The step summary limit is 1 MiB; a normal summary is a few KB.
MAX_BYTES = 64 * 1024


@dataclass
class Shard:
    header: dict
    source: str
    collection: dict | None = None
    session: dict | None = None
    cases: list[dict] = field(default_factory=list)
    bad_lines: int = 0

    @property
    def name(self) -> str:
        return str(self.header.get("shard") or "unknown")

    @property
    def run_id(self) -> str:
        return str(self.header.get("run_id") or "")

    @property
    def attempt(self) -> str:
        return str(self.header.get("run_attempt") or "")

    @property
    def cpu_model(self) -> str | None:
        environment = self.header.get("environment")
        return environment.get("cpu_model") if isinstance(environment, dict) else None

    @property
    def order(self) -> tuple[int, int]:
        return _number(self.run_id), _number(self.attempt)

    def label(self) -> str:
        return f"run {self.run_id or '?'} attempt {self.attempt or '?'}"


def _number(value: str) -> int:
    return int(value) if value.isdecimal() else -1


def read_shard(path: Path, source: str) -> Shard:
    shard = None
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                record = json.loads(line)
                kind = record["kind"]
            except (ValueError, TypeError, KeyError):
                if shard is not None:
                    shard.bad_lines += 1
                continue
            if kind == "header":
                if shard is not None:
                    raise ValueError("more than one header")
                shard = Shard(record, source)
            elif shard is None:
                raise ValueError("records before the header")
            elif kind == "collection":
                shard.collection = record
            elif kind == "session":
                shard.session = record
            elif kind == "case":
                shard.cases.append(record)
    if shard is None:
        raise ValueError("no header record")
    return shard


def load(directory: Path, problems: list[str]) -> list[Shard]:
    """Every shard under `directory`, keeping the newest attempt of each run's shard."""
    newest: dict[tuple[str, str], Shard] = {}
    if not directory.is_dir():
        return []
    for path in sorted(directory.rglob("*.jsonl")):
        # Relative names only: summaries must not show runner paths.
        source = path.relative_to(directory).as_posix()
        try:
            shard = read_shard(path, source)
        except (OSError, ValueError) as error:
            problems.append(f"{source}: {error}")
            continue
        if shard.bad_lines:
            problems.append(f"{source}: skipped {shard.bad_lines} unreadable lines")
        key = (shard.run_id, shard.name)
        if key not in newest or shard.order > newest[key].order:
            newest[key] = shard
    return sorted(newest.values(), key=lambda shard: shard.name)


def case_cost(case: dict) -> float:
    return sum(case.get(when) or 0.0 for when in ("setup", "call", "teardown"))


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of sorted values."""
    return values[max(0, math.ceil(fraction * len(values)) - 1)]


def incompatibility(current: Shard, candidate: Shard) -> str | None:
    header = candidate.header
    if header.get("event") != "push" or header.get("ref") != MAIN_REF:
        return "event"
    if header.get("format") != current.header.get("format"):
        return "format"
    schema = current.header.get("schema_version")
    if schema is None or header.get("schema_version") != schema:
        return "schema"
    if current.cpu_model is None or candidate.cpu_model != current.cpu_model:
        return "cpu"
    return None


def match_baseline(current: Shard, baselines: list[Shard]) -> tuple[Shard | None, str]:
    """The newest compatible baseline for this shard, or None and the reasons."""
    candidates = sorted(
        (shard for shard in baselines
         if shard.name == current.name and shard.order != current.order),
        key=lambda shard: shard.order, reverse=True)
    reasons: Counter[str] = Counter()
    for candidate in candidates:
        reason = incompatibility(current, candidate)
        if reason is None:
            return candidate, ""
        reasons[reason] += 1
    if not reasons:
        return None, "missing"
    return None, ", ".join(f"{reason} {count}" for reason, count in sorted(reasons.items()))


def _seconds(value: float) -> str:
    return f"{value:.3f}" if value < 10 else f"{value:.1f}"


def _code(nodeid: str) -> str:
    if len(nodeid) > MAX_NODEID:
        nodeid = nodeid[:MAX_NODEID - 3] + "..."
    return "`" + nodeid.replace("`", "'").replace("|", "\\|") + "`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _tier(case: dict) -> str:
    return case.get("tier") or "none"


@dataclass
class TierStats:
    passing: int
    median: float | None
    p90: float | None
    setup: float
    call: float
    teardown: float
    shared_cases: int
    shared_seconds: float


def tier_stats(cases: list[dict]) -> TierStats:
    """Typical cost of passing cases, leaving out those that paid for shared setup."""
    passing = [case for case in cases if case.get("outcome") == "passed"]
    typical = sorted(case_cost(case) for case in passing if not case.get("shared_fixtures"))
    shared = [case_cost(case) for case in passing if case.get("shared_fixtures")]
    setup, call, teardown = (sum(case.get(when) or 0.0 for case in cases)
                             for when in ("setup", "call", "teardown"))
    return TierStats(
        passing=len(passing),
        median=statistics.median(typical) if typical else None,
        p90=percentile(typical, 0.9) if typical else None,
        setup=setup, call=call, teardown=teardown,
        shared_cases=len(shared), shared_seconds=sum(shared))


def render_tiers(shards: list[Shard]) -> list[str]:
    cases = [(shard, case) for shard in shards for case in shard.cases]
    tiers = [tier for tier in (*TIERS, "none") if any(_tier(case) == tier for _, case in cases)]
    lines = [
        "### Tiers",
        "",
        "| Tier | Passing | Median s | p90 s | Setup s | Call s | Teardown s | Shared setup |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    slowest = []
    for tier in tiers:
        in_tier = [(shard, case) for shard, case in cases if _tier(case) == tier]
        stats = tier_stats([case for _, case in in_tier])
        typical = " | ".join("-" if value is None else _seconds(value)
                             for value in (stats.median, stats.p90))
        lines.append(
            f"| {tier} | {stats.passing} | {typical} | {_seconds(stats.setup)} | "
            f"{_seconds(stats.call)} | {_seconds(stats.teardown)} | "
            f"{stats.shared_cases} ({_seconds(stats.shared_seconds)} s) |")
        ranked = sorted(in_tier, key=lambda item: case_cost(item[1]), reverse=True)
        slowest.extend((tier, shard, case) for shard, case in ranked[:SLOWEST_PER_TIER])
    lines += [
        "",
        "Median and p90 cover passing cases across all shards, except cases whose "
        "setup paid for a module- or session-scoped fixture (Shared setup: cases and "
        "their total). "
        "Phase totals cover every recorded case.",
        "",
        "### Slowest cases",
        "",
        "| Tier | Case | Shard | Seconds | Outcome |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for tier, shard, case in slowest:
        note = " (shared setup)" if case.get("shared_fixtures") else ""
        lines.append(f"| {tier} | {_code(str(case.get('nodeid')))} | {_cell(shard.name)} | "
                     f"{_seconds(case_cost(case))} | {_cell(str(case.get('outcome')))}{note} |")
    return lines


def _passing_costs(shard: Shard) -> dict[str, float]:
    return {case["nodeid"]: case_cost(case) for case in shard.cases
            if case.get("outcome") == "passed" and "nodeid" in case}


def render_shards(current: list[Shard], baselines: list[Shard]) -> list[str]:
    lines = [
        "### Shards",
        "",
        "| Shard | CPU | Cases | Not passed | Collection s | Session s | Case total s | Baseline | Change |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | --- | --- |",
    ]
    changes = []
    collection_total = 0.0
    for shard in current:
        outcomes = Counter(str(case.get("outcome")) for case in shard.cases)
        outcomes.pop("passed", None)
        not_passed = ", ".join(f"{name} {count}" for name, count in sorted(outcomes.items())) or "-"
        collection = (shard.collection or {}).get("seconds")
        session = (shard.session or {}).get("seconds")
        collection_total += collection or 0.0
        baseline, reason = match_baseline(shard, baselines)
        if baseline is None:
            matched, change = f"no compatible baseline ({reason})", "-"
        else:
            now, before = _passing_costs(shard), _passing_costs(baseline)
            common = sorted(set(now) & set(before))
            now_total = sum(now[nodeid] for nodeid in common)
            before_total = sum(before[nodeid] for nodeid in common)
            matched = baseline.label()
            change = (f"{(now_total - before_total) / before_total:+.1%} over {len(common)} cases"
                      if before_total else "-")
            changes.extend((now[nodeid] - before[nodeid], nodeid, shard.name,
                            now[nodeid], before[nodeid]) for nodeid in common)
        lines.append(
            f"| {_cell(shard.name)} | {_cell(shard.cpu_model or 'unknown')} | {len(shard.cases)} | "
            f"{not_passed} | {_seconds(collection) if collection is not None else '-'} | "
            f"{_seconds(session) if session is not None else '-'} | "
            f"{_seconds(sum(case_cost(case) for case in shard.cases))} | {_cell(matched)} | {change} |")
    lines += [
        "",
        f"Collection total: {_seconds(collection_total)} s. A baseline is the newest "
        "main push shard with the same format, schema version and CPU model; Change "
        "compares cases that passed in both runs. Reasons count rejected baselines: "
        "event (not a main push), format, schema or cpu; missing means none was found.",
    ]
    increases = sorted((item for item in changes if item[0] > 0), reverse=True)[:LARGEST_CHANGES]
    if increases:
        lines += [
            "",
            "### Largest increases",
            "",
            "| Case | Shard | Now s | Baseline s | Change s |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        lines += [f"| {_code(nodeid)} | {_cell(name)} | {_seconds(now)} | {_seconds(before)} | "
                  f"+{_seconds(delta)} |" for delta, nodeid, name, now, before in increases]
    return lines


def render(current_dir: Path, baseline_dirs: list[Path]) -> str:
    problems: list[str] = []
    current = load(current_dir, problems)
    baselines = [shard for directory in baseline_dirs for shard in load(directory, problems)]
    lines = ["## Test timing", ""]
    if not current:
        lines.append("No timing records were found for this run.")
    else:
        runs = sorted({shard.label() for shard in current})
        cases = sum(len(shard.cases) for shard in current)
        lines += [
            f"{', '.join(runs)}: {len(current)} shards, {cases} cases. "
            "Case cost is setup + call + teardown seconds. Reporting only, never a gate.",
            "",
            *render_tiers(current),
            "",
            *render_shards(current, baselines),
        ]
    if problems:
        lines += ["", "### Unreadable records", ""]
        lines += [f"- {_cell(problem)}" for problem in problems[:MAX_PROBLEMS]]
        if len(problems) > MAX_PROBLEMS:
            lines.append(f"- and {len(problems) - MAX_PROBLEMS} more")
    text = "\n".join(lines) + "\n"
    if len(text.encode()) > MAX_BYTES:
        text = text.encode()[:MAX_BYTES].decode(errors="ignore") + "\n\nSummary truncated.\n"
    return text


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
        parser.add_argument("--current", type=Path, required=True,
                            help="directory of this run's timing records")
        parser.add_argument("--baseline", type=Path, action="append", default=[],
                            help="directory of candidate baseline records; repeatable")
        args = parser.parse_args(argv)
        text = render(args.current, args.baseline)
    except (Exception, SystemExit) as error:
        text = (f"## Test timing\n\nThe timing summary failed: "
                f"`{type(error).__name__}: {error}`\n")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
