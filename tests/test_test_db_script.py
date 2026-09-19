from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "test_db.sh"


def _write_fake_podman(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "podman"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"${FAKE_PODMAN_LOG:?}\"\n"
        "case \"$1\" in\n"
        "  run) printf 'owned-container\\n' ;;\n"
        "  exec) exit \"${FAKE_READY_EXIT:-0}\" ;;\n"
        "  port) [ \"${FAKE_PORT_EXIT:-0}\" = 0 ] || exit \"$FAKE_PORT_EXIT\"; printf '127.0.0.1:49153\\n' ;;\n"
        "  ps) printf 'owned-container\\nunowned-container\\n' ;;\n"
        "  inspect)\n"
        "    if [ \"${@: -1}\" = owned-container ]; then printf '1|task-42\\n'; else printf '0|task-42\\n'; fi\n"
        "    ;;\n"
        "  rm) exit 0 ;;\n"
        "  *) echo \"unexpected podman command: $*\" >&2; exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *args: str, **env) -> subprocess.CompletedProcess[str]:
    bin_dir = _write_fake_podman(tmp_path)
    log = tmp_path / "podman.log"
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_PODMAN_LOG": str(log),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_start_creates_labelled_container_with_an_automatic_loopback_port(tmp_path):
    result = _run(tmp_path, "start", "task-42")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "export TEST_DATABASE_URL='postgresql://mileage:testpw@127.0.0.1:49153/mileage'\n"
    )
    log = (tmp_path / "podman.log").read_text()
    assert "run -d --name odograph-testdb-task-42-" in log
    assert "--label io.odograph.test-db=1" in log
    assert "--label io.odograph.test-db-task=task-42" in log
    assert "-p 127.0.0.1::5432" in log
    assert not any(line.startswith("rm ") for line in log.splitlines())


def test_cleanup_removes_only_containers_with_both_ownership_labels(tmp_path):
    result = _run(tmp_path, "cleanup", "task-42")

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "podman.log").read_text()
    assert "rm -f --volumes owned-container" in log
    assert not any(line.startswith("rm ") and "unowned-container" in line for line in log.splitlines())
    assert "volume rm" not in log


@pytest.mark.parametrize("failure", [{"FAKE_READY_EXIT": "1"}, {"FAKE_PORT_EXIT": "7"}])
def test_failed_start_removes_the_container_and_its_anonymous_volumes(tmp_path, failure):
    result = _run(tmp_path, "start", "task-42", **failure)

    assert result.returncode != 0
    assert "TEST_DATABASE_URL" not in result.stdout
    lines = (tmp_path / "podman.log").read_text().splitlines()
    assert lines.count("rm -f --volumes owned-container") == 1
    assert not any(line.startswith("volume ") for line in lines)


def test_rejects_task_ids_that_cannot_be_used_safely_in_labels(tmp_path):
    result = _run(tmp_path, "start", "not safe")

    assert result.returncode == 1
    assert "TASK_ID may contain only" in result.stderr
    assert not (tmp_path / "podman.log").exists()
