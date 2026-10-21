import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci_timing_summary.py"
SPEC = importlib.util.spec_from_file_location("ci_timing_summary", SCRIPT)
summary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = summary
SPEC.loader.exec_module(summary)

CPU = "AMD EPYC 7763 64-Core Processor"


def header(shard="db-1", run_id="200", attempt="1", event="push", ref="refs/heads/main",
           fmt=2, schema=47, cpu=CPU):
    return {"kind": "header", "shard": shard, "run_id": run_id, "run_attempt": attempt,
            "event": event, "ref": ref, "format": fmt, "schema_version": schema,
            "environment": {"cpu_model": cpu}}


def case(nodeid, seconds, tier="db", outcome="passed", shared=None, shard="db-1"):
    record = {"kind": "case", "shard": shard, "nodeid": nodeid, "tier": tier,
              "outcome": outcome, "setup": 0.0, "call": seconds, "teardown": 0.0, "spans": {}}
    if shared:
        record["shared_fixtures"] = shared
    return record


def write(directory: Path, head: dict, cases=(), extra_lines=()) -> Path:
    path = directory / f"pytest-timing-{head['run_id']}-{head['run_attempt']}-{head['shard']}" / "pytest-timing.jsonl"
    path.parent.mkdir(parents=True)
    records = [head, {"kind": "collection", "shard": head["shard"], "seconds": 1.5, "selected": len(cases)},
               *cases, {"kind": "session", "shard": head["shard"], "seconds": 9.0, "exitstatus": 0}]
    path.write_text("".join(json.dumps(record) + "\n" for record in records) + "".join(extra_lines))
    return path


def shard(**fields):
    return summary.Shard(header(**fields), "test")


def test_tier_stats_combine_passing_cases_and_set_shared_setup_apart():
    cases = [
        case("a", 1.0), case("b", 2.0), case("c", 3.0), case("d", 4.0),
        case("e", 100.0, outcome="failed"),
        case("f", 50.0, shared=["_migrated_schema"]),
    ]
    cases[0]["setup"], cases[0]["teardown"] = 0.5, None

    stats = summary.tier_stats(cases)

    assert stats.passing == 5
    assert stats.median == 2.5
    assert stats.p90 == 4.0
    assert (stats.setup, stats.call, stats.teardown) == (0.5, 160.0, 0.0)
    assert (stats.shared_cases, stats.shared_seconds) == (1, 50.0)
    assert summary.tier_stats([case("x", 1.0, shared=["s"])]).median is None


def test_percentile_is_nearest_rank():
    values = [float(value) for value in range(1, 11)]
    assert summary.percentile(values, 0.9) == 9.0
    assert summary.percentile(values, 0.5) == 5.0
    assert summary.percentile([7.0], 0.9) == 7.0


def test_median_spans_all_shards_not_per_shard_medians(tmp_path):
    current = tmp_path / "current"
    write(current, header(shard="db-1"), [case("a", 1.0), case("b", 1.0), case("c", 1.0)])
    write(current, header(shard="db-2"), [case("d", 9.0, shard="db-2")])

    text = summary.render(current, [])

    # Per-shard medians would give 1.0 and 9.0; the combined median is 1.0.
    assert "| db | 4 | 1.000 | 9.000 |" in text


def test_newest_compatible_baseline_is_matched():
    current = shard(run_id="300")
    older = shard(run_id="100")
    newer = shard(run_id="200", attempt="2")
    newest_other_cpu = shard(run_id="250", cpu="AMD EPYC 9V74 80-Core Processor")
    other_shard = shard(run_id="290", shard="db-2")

    matched, reason = summary.match_baseline(current, [older, newer, newest_other_cpu, other_shard])

    assert matched is newer and reason == ""
    assert newer.label() == "run 200 attempt 2"


@pytest.mark.parametrize("fields, reason", [
    ({"event": "pull_request"}, "event"),
    ({"ref": "refs/pull/7/merge"}, "event"),
    ({"fmt": 1}, "format"),
    ({"schema": 46}, "schema"),
    ({"schema": None}, "schema"),
    ({"cpu": "INTEL(R) XEON(R) PLATINUM 8573C"}, "cpu"),
])
def test_each_incompatibility_is_named(fields, reason):
    current = shard(run_id="300")
    candidate = shard(run_id="200", **fields)

    assert summary.incompatibility(current, candidate) == reason
    assert summary.match_baseline(current, [candidate]) == (None, f"{reason} 1")


def test_current_without_schema_version_matches_nothing():
    current = shard(run_id="300", schema=None)
    assert summary.match_baseline(current, [shard(run_id="200", schema=None)]) == (None, "schema 1")


def test_missing_baseline_and_reason_counts():
    current = shard(run_id="300")
    assert summary.match_baseline(current, []) == (None, "missing")
    assert summary.match_baseline(current, [shard(run_id="200", shard="db-2")]) == (None, "missing")
    assert summary.match_baseline(current, [current]) == (None, "missing")
    rejected = [shard(run_id="200", fmt=1), shard(run_id="201", cpu="other"),
                shard(run_id="202", cpu="other")]
    assert summary.match_baseline(current, rejected) == (None, "cpu 2, format 1")


def test_load_keeps_the_newest_attempt_of_each_shard(tmp_path):
    write(tmp_path, header(attempt="1"), [case("a", 1.0)])
    write(tmp_path, header(attempt="2"), [case("a", 2.0)])
    write(tmp_path, header(attempt="1", shard="db-2"), [case("b", 1.0, shard="db-2")])
    problems = []

    shards = summary.load(tmp_path, problems)

    assert [(item.name, item.attempt) for item in shards] == [("db-1", "2"), ("db-2", "1")]
    assert problems == []
    assert summary.load(tmp_path / "absent", problems) == []


def test_render_reports_matches_rejections_and_largest_increases(tmp_path):
    current, baselines = tmp_path / "current", tmp_path / "baselines"
    write(current, header(run_id="300", event="pull_request", ref="refs/pull/7/merge"), [
        case("tests/test_a_db.py::test_slow[a|b]", 3.0),
        case("tests/test_a_db.py::test_new", 1.0),
        case("tests/test_a_db.py::test_flaky", 1.0, outcome="failed"),
    ])
    write(current, header(run_id="300", shard="db-2"), [case("tests/test_b_db.py::test_b", 1.0, shard="db-2")])
    write(baselines, header(run_id="200"), [
        case("tests/test_a_db.py::test_slow[a|b]", 2.0),
        case("tests/test_a_db.py::test_flaky", 0.5),
    ])
    write(baselines, header(run_id="200", shard="db-2", cpu="other"),
          [case("tests/test_b_db.py::test_b", 1.0, shard="db-2")])
    write(baselines, header(run_id="199", shard="db-2", event="pull_request"),
          [case("tests/test_b_db.py::test_b", 1.0, shard="db-2")])

    text = summary.render(current, [baselines])

    assert text.startswith("## Test timing\n")
    assert "run 300 attempt 1: 2 shards, 4 cases." in text
    assert "| db-1 | AMD EPYC 7763 64-Core Processor | 3 | failed 1 | 1.500 | 9.000 | 5.000 | run 200 attempt 1 | +50.0% over 1 cases |" in text
    assert "| db-2 | other" not in text
    assert "no compatible baseline (cpu 1, event 1)" in text
    assert "| `tests/test_a_db.py::test_slow[a\\|b]` | db-1 | 3.000 | 2.000 | +1.000 |" in text
    assert "test_new` | db-1 | 1.000 |" not in text.split("### Largest increases")[1]
    assert str(tmp_path) not in text


def test_render_marks_shared_setup_and_limits_slowest_cases(tmp_path):
    cases = [case(f"tests/test_u.py::test_{index}", float(index), tier="unit") for index in range(8)]
    cases.append(case("tests/test_u.py::test_shared", 50.0, tier="unit", shared=["tmp_path_factory"]))
    cases.append(case("tests/test_u.py::test_untiered", 0.1, tier=None))
    write(tmp_path, header(shard="unit-ops"), cases)

    text = summary.render(tmp_path, [])

    slowest = text.split("### Slowest cases")[1].split("### Shards")[0]
    assert "| unit | `tests/test_u.py::test_shared` | unit-ops | 50.0 | passed (shared setup) |" in slowest
    assert slowest.count("| unit |") == summary.SLOWEST_PER_TIER
    assert "| none | `tests/test_u.py::test_untiered`" in slowest
    assert "| unit | 9 | 3.500 | 7.000 |" in text
    assert "1 (50.0 s)" in text
    assert "no compatible baseline (missing)" in text
    assert len(text.encode()) < 8 * 1024


def test_unreadable_records_are_listed_without_runner_paths(tmp_path):
    write(tmp_path, header(), [case("a", 1.0)], extra_lines=["{not json\n"])
    no_header = tmp_path / "no-header" / "pytest-timing.jsonl"
    no_header.parent.mkdir()
    no_header.write_text(json.dumps(case("b", 1.0)) + "\n")
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "pytest-timing.jsonl").write_text("")

    text = summary.render(tmp_path, [])

    assert "### Unreadable records" in text
    assert "- empty/pytest-timing.jsonl: no header record" in text
    assert "- no-header/pytest-timing.jsonl: records before the header" in text
    assert "skipped 1 unreadable lines" in text
    assert str(tmp_path) not in text


def test_no_current_records_still_writes_a_summary(tmp_path, capsys):
    assert summary.main(["--current", str(tmp_path / "absent")]) == 0
    assert capsys.readouterr().out == "## Test timing\n\nNo timing records were found for this run.\n"


def test_errors_are_reported_in_the_summary_and_exit_zero(tmp_path, monkeypatch, capsys):
    def explode(current, baselines):
        raise RuntimeError("broken record")
    monkeypatch.setattr(summary, "render", explode)

    assert summary.main(["--current", str(tmp_path)]) == 0
    assert "The timing summary failed: `RuntimeError: broken record`" in capsys.readouterr().out

    monkeypatch.undo()
    assert summary.main(["--unknown"]) == 0
    assert "The timing summary failed: `SystemExit: 2`" in capsys.readouterr().out


def test_oversized_summary_is_truncated(tmp_path, monkeypatch):
    write(tmp_path, header(), [case("a", 1.0)])
    monkeypatch.setattr(summary, "MAX_BYTES", 200)

    text = summary.render(tmp_path, [])

    assert len(text.encode()) < 250
    assert text.endswith("Summary truncated.\n")


def test_command_line_exits_zero_on_bad_arguments():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert "The timing summary failed" in result.stdout
