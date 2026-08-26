from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
        "  exec) exit 0 ;;\n"
        "  port) printf '127.0.0.1:49153\\n' ;;\n"
        "  ps) printf 'owned-container\\nunowned-container\\n' ;;\n"
        "  inspect)\n"
        "    if [ \"${@: -1}\" = owned-container ]; then printf '1|task-42\\n'; else printf '0|task-42\\n'; fi\n"
        "    ;;\n"
        "  rm) exit 0 ;;\n"
        "  *) echo \"unexpected podman command: $*\" >&2; exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _write_fake_podman(tmp_path)
    log = tmp_path / "podman.log"
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_PODMAN_LOG": str(log),
        },
        capture_output=True,
        text=True,
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


def test_cleanup_removes_only_containers_with_both_ownership_labels(tmp_path):
    result = _run(tmp_path, "cleanup", "task-42")

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "podman.log").read_text()
    assert "rm -f owned-container" in log
    assert "rm -f unowned-container" not in log


def test_rejects_task_ids_that_cannot_be_used_safely_in_labels(tmp_path):
    result = _run(tmp_path, "start", "not safe")

    assert result.returncode == 1
    assert "TASK_ID may contain only" in result.stderr
    assert not (tmp_path / "podman.log").exists()
