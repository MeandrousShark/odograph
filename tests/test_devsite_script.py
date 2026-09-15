"""Tests for scripts/devsite.sh.

Every fake podman used here also enforces the isolation guarantee as a
standing side effect: the QA site and the disposable test databases must
never collect each other's containers, since one holds hand-built QA data
and the other is destroyed constantly. It exits loudly if any invocation
ever mentions a scripts/test_db.sh container name or label
(see the "poison pill" in _FAKE_PODMAN_SCRIPT below), so any test in this
file would catch devsite.sh reaching into disposable-test-db territory, not
just the test named for it.

The database container is faked throughout (no real Postgres, matching the
`ops` tier's no-DB-infrastructure rule); container lifecycle correctness
against a real podman was verified manually and is recorded in the
implementation report, not re-proven here. uvicorn is faked with a tiny
stdlib HTTP server bound to a real loopback port, so devsite.sh's own
process-lifecycle logic (pid tracking, healthz polling, SIGTERM shutdown) is
exercised for real rather than simulated.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "devsite.sh"
TEST_DB_SCRIPT = REPO_ROOT / "scripts" / "test_db.sh"

_FAKE_PODMAN_SCRIPT = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${FAKE_PODMAN_LOG:?}"

# Isolation-direction poison pill: devsite.sh must never construct a podman
# invocation naming scripts/test_db.sh's containers or labels. If one ever
# shows up here, devsite.sh emitted it, not this fake.
case " $* " in
    *"test-db"*)
        echo "fake podman received a disposable-test-db token from devsite.sh: $*" >&2
        exit 9
        ;;
esac

case "$1" in
    container)
        [ "${2:-}" = exists ] || { echo "unexpected podman command: $*" >&2; exit 2; }
        exit "${FAKE_PODMAN_CONTAINER_EXISTS:-1}"
        ;;
    volume)
        case "${2:-}" in
            exists) exit "${FAKE_PODMAN_VOLUME_EXISTS:-1}" ;;
            create) exit 0 ;;
            *) echo "unexpected podman command: $*" >&2; exit 2 ;;
        esac
        ;;
    run) printf 'devsite-container\\n'; exit 0 ;;
    start) exit 0 ;;
    stop) exit 0 ;;
    inspect) printf '%s\\n' "${FAKE_PODMAN_CONTAINER_RUNNING:-false}"; exit 0 ;;
    exec) exit "${FAKE_PODMAN_EXEC_EXIT:-0}" ;;
    logs) printf 'fake db log line\\n'; exit 0 ;;
    *) echo "unexpected podman command: $*" >&2; exit 2 ;;
esac
"""

_FAKE_UVICORN_SCRIPT = '''#!/usr/bin/env python3
"""Stand-in for uvicorn: binds --port and answers 200 to any request, so
devsite.sh's healthz probe and pid-based process lifecycle can be tested
without a real FastAPI app or database."""
import sys
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

port = 8000
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--port" and i + 1 < len(args):
        port = int(args[i + 1])


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass


class _Server(TCPServer):
    allow_reuse_address = True


with _Server(("127.0.0.1", port), _Handler) as httpd:
    httpd.serve_forever()
'''


def _fake_podman(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "podman"
    fake.write_text(_FAKE_PODMAN_SCRIPT)
    fake.chmod(0o755)
    return bin_dir, tmp_path / "podman.log"


def _write_fake_uvicorn(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-uvicorn"
    fake.write_text(_FAKE_UVICORN_SCRIPT)
    fake.chmod(0o755)
    return fake


def _free_port() -> int:
    # A brief TOCTOU race is inherent here (something else could grab the
    # port between closing this socket and the fake uvicorn binding it);
    # acceptable for test-only tooling.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _base_env(tmp_path: Path, bin_dir: Path, log: Path, **overrides: str) -> dict[str, str]:
    # A real free port, not a fixed constant: this machine's actual 8078
    # may legitimately be occupied by a real devsite the maintainer is
    # using, and these tests must not depend on that being false.
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_PODMAN_LOG": str(log),
        "DEVSITE_STATE_DIR": str(tmp_path / "state"),
        "DEVSITE_ENV_FILE": str(tmp_path / ".env.devsite"),
        "DEVSITE_DB_PORT": "15432",
        "DEVSITE_APP_PORT": str(_free_port()),
        "DEVSITE_PYTHON": sys.executable,
    }
    env.update(overrides)
    return env


def _run_devsite(tmp_path: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_leftover(pid_file: Path) -> None:
    """Best-effort cleanup for a test that spawned a real fake-uvicorn
    process and failed an assertion before reaching its own teardown."""
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_up_creates_a_distinctly_labelled_container_with_a_fixed_port_and_named_volume(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(tmp_path, bin_dir, log, DEVSITE_UVICORN=str(tmp_path / "no-such-uvicorn"))

    result = _run_devsite(tmp_path, "up", env=env)

    # DEVSITE_UVICORN doesn't exist, so `up` fails right after the database
    # step -- fast, and it still proves everything up to that point.
    assert result.returncode == 1
    assert "not found" in result.stderr

    lines = log.read_text().splitlines()
    assert any(line.startswith("run -d --name odograph-devsite-db") for line in lines)
    assert any("--label io.odograph.devsite=1" in line for line in lines)
    assert not any("io.odograph.test-db" in line for line in lines)
    assert any("-v odograph-devsite-dbdata:/var/lib/postgresql/data" in line for line in lines)
    assert any("-p 127.0.0.1:15432:5432" in line for line in lines)
    # Fixed, not ephemeral like scripts/test_db.sh's disposable containers.
    assert not any("-p 127.0.0.1::5432" in line for line in lines)

    env_file = Path(env["DEVSITE_ENV_FILE"])
    assert env_file.exists()
    content = env_file.read_text()
    assert "DATABASE_URL=postgresql://mileage:" in content
    assert "@127.0.0.1:15432/mileage_devsite" in content
    assert "INGEST_PASSWORD=" in content
    assert "SESSION_SECRET=" in content
    assert "DEV_NO_AUTH=1" in content
    assert oct(env_file.stat().st_mode)[-3:] == "600"


def test_up_never_overwrites_an_existing_env_file(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(tmp_path, bin_dir, log, DEVSITE_UVICORN=str(tmp_path / "no-such-uvicorn"))
    env_file = Path(env["DEVSITE_ENV_FILE"])
    sentinel = (
        "DATABASE_URL=postgresql://mileage:sentinel@127.0.0.1:15432/mileage_devsite\n"
        "INGEST_PASSWORD=sentinel\nSESSION_SECRET=sentinel\nDEV_NO_AUTH=1\n"
    )
    env_file.write_text(sentinel)

    _run_devsite(tmp_path, "up", env=env)

    assert env_file.read_text() == sentinel


def test_up_refuses_an_orphaned_database_volume(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(tmp_path, bin_dir, log, FAKE_PODMAN_VOLUME_EXISTS="0")

    result = _run_devsite(tmp_path, "up", env=env)

    assert result.returncode == 1
    assert "refusing to attach a new database image" in result.stderr
    lines = log.read_text().splitlines()
    assert not any(line.startswith(("run ", "volume create ", "volume rm ")) for line in lines)


def test_up_does_not_recreate_an_already_running_database_container(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(
        tmp_path, bin_dir, log,
        DEVSITE_UVICORN=str(tmp_path / "no-such-uvicorn"),
        FAKE_PODMAN_CONTAINER_EXISTS="0",
        FAKE_PODMAN_CONTAINER_RUNNING="true",
    )

    result = _run_devsite(tmp_path, "up", env=env)

    assert result.returncode == 1  # still fails fast at the missing uvicorn
    lines = log.read_text().splitlines()
    assert not any(line.startswith("run -d") for line in lines)
    assert not any(line.startswith("start ") for line in lines)
    assert "already running" in result.stdout


def test_migrate_and_reseed_refuse_without_an_env_file(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(tmp_path, bin_dir, log)

    for action in ("migrate", "reseed"):
        result = _run_devsite(tmp_path, action, env=env)
        assert result.returncode == 1
        assert "run 'scripts/devsite.sh up' first" in result.stderr

    # Neither action should touch podman before checking for the env file.
    assert not log.exists()


def test_status_and_logs_before_anything_is_running(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(tmp_path, bin_dir, log)

    status = _run_devsite(tmp_path, "status", env=env)
    assert status.returncode == 0, status.stderr
    assert "not created yet" in status.stdout
    assert "not running" in status.stdout

    logs = _run_devsite(tmp_path, "logs", env=env)
    assert logs.returncode == 1
    assert "no uvicorn log yet" in logs.stderr


def test_bad_usage_prints_help_and_exits_nonzero(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    env = _base_env(tmp_path, bin_dir, log)

    no_args = _run_devsite(tmp_path, env=env)
    assert no_args.returncode == 1
    assert "usage:" in no_args.stderr

    bad_action = _run_devsite(tmp_path, "bogus", env=env)
    assert bad_action.returncode == 1
    assert "usage:" in bad_action.stderr

    bad_log_target = _run_devsite(tmp_path, "logs", "bogus", env=env)
    assert bad_log_target.returncode == 1


def test_up_is_idempotent_for_uvicorn_and_down_then_up_preserves_the_container(tmp_path):
    bin_dir, log = _fake_podman(tmp_path)
    fake_uvicorn = _write_fake_uvicorn(tmp_path)
    env = _base_env(
        tmp_path, bin_dir, log,
        DEVSITE_UVICORN=str(fake_uvicorn),
        # Already running: skip database creation and go straight to the
        # uvicorn lifecycle, which is what this test is actually about.
        FAKE_PODMAN_CONTAINER_EXISTS="0",
        FAKE_PODMAN_CONTAINER_RUNNING="true",
    )
    pid_file = Path(env["DEVSITE_STATE_DIR"]) / "uvicorn.pid"

    try:
        first = _run_devsite(tmp_path, "up", env=env)
        assert first.returncode == 0, first.stderr
        first_pid = int(pid_file.read_text())
        assert _pid_alive(first_pid)

        second = _run_devsite(tmp_path, "up", env=env)
        assert second.returncode == 0, second.stderr
        assert int(pid_file.read_text()) == first_pid, "a second up must not replace the running uvicorn"
        assert _pid_alive(first_pid)
        assert "already running" in second.stdout

        down = _run_devsite(tmp_path, "down", env=env)
        assert down.returncode == 0, down.stderr
        assert not pid_file.exists()
        assert not _pid_alive(first_pid)

        second_down = _run_devsite(tmp_path, "down", env=env)
        assert second_down.returncode == 0, second_down.stderr

        again = _run_devsite(tmp_path, "up", env=env)
        assert again.returncode == 0, again.stderr
        new_pid = int(pid_file.read_text())
        assert new_pid != first_pid
        assert _pid_alive(new_pid)
    finally:
        _kill_leftover(pid_file)


def test_devsite_up_and_down_never_emit_a_disposable_test_db_token(tmp_path):
    """One isolation direction: a full up (fresh create) then down cycle
    must never reference scripts/test_db.sh's containers or labels, or a
    routine test cleanup could destroy the QA site's accumulated data.
    Every fake podman call in
    this file already enforces this as a poison pill; this test exercises
    the broadest realistic set of invocations against it explicitly.
    """
    bin_dir, log = _fake_podman(tmp_path)
    fake_uvicorn = _write_fake_uvicorn(tmp_path)
    env = _base_env(tmp_path, bin_dir, log, DEVSITE_UVICORN=str(fake_uvicorn))
    pid_file = Path(env["DEVSITE_STATE_DIR"]) / "uvicorn.pid"

    try:
        up = _run_devsite(tmp_path, "up", env=env)
        assert up.returncode == 0, up.stderr
        down = _run_devsite(tmp_path, "down", env=env)
        assert down.returncode == 0, down.stderr
    finally:
        _kill_leftover(pid_file)

    text = log.read_text()
    assert "io.odograph.test-db" not in text
    assert "odograph-testdb" not in text


def test_test_db_sh_cleanup_never_touches_a_devsite_labelled_container(tmp_path):
    """The other isolation direction: scripts/test_db.sh's cleanup filters
    containers by its own two ownership labels. This fake deliberately
    returns both a disposable and a devsite-labelled container from `ps`
    regardless of the filter (worst case for a container-listing bug), so
    what actually protects the devsite container is test_db.sh's own
    inspect-based label check before removing anything -- the same defense
    proven generically by test_test_db_script.py, exercised here with a
    devsite-shaped label set specifically.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "podman"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"${FAKE_PODMAN_LOG:?}\"\n"
        "case \"$1\" in\n"
        "  ps) printf 'disposable-container\\n'; printf 'devsite-container\\n' ;;\n"
        "  inspect)\n"
        "    if [ \"${@: -1}\" = disposable-container ]; then printf '1|task-42\\n'; else printf '<no value>|<no value>\\n'; fi\n"
        "    ;;\n"
        "  rm) exit 0 ;;\n"
        "  *) echo \"unexpected podman command: $*\" >&2; exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    log = tmp_path / "podman.log"

    result = subprocess.run(
        ["bash", str(TEST_DB_SCRIPT), "cleanup", "task-42"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_PODMAN_LOG": str(log)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "rm -f disposable-container" in text
    assert "rm -f devsite-container" not in text
