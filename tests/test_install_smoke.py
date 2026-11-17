"""The install smoke runs the guide's own block, so its checks are pinned here."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/install-compose.md"
SCRIPT = ROOT / "scripts/install_smoke.sh"
REAL_CURL = shutil.which("curl")

FAKE_ENGINE = r"""#!/usr/bin/env bash
state="$FAKE_STATE"
up() { [ -e "$state/up" ] && [ ! -e "$state/gone" ]; }
if [ "$(basename "$0")" = podman-compose ] || [ "$1" = compose ]; then
    [ "$1" = compose ] && shift
    echo "COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-} $(basename "$0") $*" >> "$state/calls"
    case "$*" in
    "up -d") touch "$state/up" ;;
    *" down -v") touch "$state/gone" ;;
    *create-admin) cat > "$state/admin" ;;
    *list-accounts)
        if [ "$FAKE_SCENARIO" != admin-lost ]; then head -n 1 "$state/admin"; fi
        ;;
    esac
    exit 0
fi
case "$1" in
ps)
    case "$*" in
    *label=*) up && echo 0123456789ab ;;
    esac
    ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in file://*) exec "$REAL_CURL" "$@" ;; esac
done
[ "$FAKE_SCENARIO" != unhealthy ] && [ -e "$FAKE_STATE/up" ]
"""

FAKE_SS = r"""#!/usr/bin/env bash
[ "$FAKE_SCENARIO" != port-busy ] || echo "LISTEN 0 4096 127.0.0.1:8077 0.0.0.0:*"
"""


def _guide(tmp_path: Path, change=None) -> Path:
    text = GUIDE.read_text()
    if change:
        changed = change(text)
        assert changed != text
        text = changed
    path = tmp_path / "install-compose.md"
    path.write_text(text)
    return path


def _run_smoke(
    tmp_path: Path, scenario: str = "works", guide: Path = GUIDE, compose_cmd: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "calls").touch()
    tools = {
        "docker": FAKE_ENGINE, "podman": FAKE_ENGINE, "podman-compose": FAKE_ENGINE,
        "curl": FAKE_CURL, "ss": FAKE_SS, "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, source in tools.items():
        tool = fake_bin / name
        tool.write_text(source)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"COMPOSE_CMD", "COMPOSE_PROJECT_NAME", "GITHUB_STEP_SUMMARY"}
    }
    environment.update(
        PATH=f"{fake_bin}{os.pathsep}{environment['PATH']}", FAKE_STATE=str(state),
        FAKE_SCENARIO=scenario, REAL_CURL=REAL_CURL,
        INSTALL_SMOKE_TIMEOUT="5", INSTALL_SMOKE_GUIDE=str(guide),
    )
    if compose_cmd:
        environment["COMPOSE_CMD"] = compose_cmd
    result = subprocess.run(
        ["bash", str(SCRIPT), str(ROOT / "compose.yaml")],
        env=environment, capture_output=True, text=True, timeout=45,
    )
    return result, (state / "calls").read_text().splitlines()


def test_smoke_runs_the_guide_block_under_its_own_project(tmp_path):
    result, calls = _run_smoke(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "ok: a second run fails and leaves compose.yaml and .env alone" in result.stdout
    [up] = [call for call in calls if call.endswith(" up -d")]
    project = up.split()[0].removeprefix("COMPOSE_PROJECT_NAME=")
    assert up.split()[1:] == ["docker", "up", "-d"]
    assert project.startswith("odograph-install-smoke-")
    assert any(call.endswith(f"-p {project} down -v") for call in calls)
    assert any("create-admin" in call for call in calls)


def test_smoke_runs_podman_compose_in_place_of_docker_compose(tmp_path):
    result, calls = _run_smoke(tmp_path, compose_cmd="podman-compose")

    assert result.returncode == 0, result.stderr
    [up] = [call for call in calls if call.endswith(" up -d")]
    assert up.split()[1:] == ["podman-compose", "up", "-d"]
    assert all(call.split()[1] == "podman-compose" for call in calls)


@pytest.mark.parametrize(("scenario", "message"), [
    ("unhealthy", "/healthz did not answer within 5s"),
    ("admin-lost", "the new administrator is not listed"),
])
def test_smoke_fails_a_broken_install_and_cleans_up(tmp_path, scenario, message):
    result, calls = _run_smoke(tmp_path, scenario)

    assert result.returncode == 1
    assert message in result.stderr
    assert calls[-1].endswith(" down -v")


def test_smoke_refuses_a_busy_port_before_installing(tmp_path):
    result, calls = _run_smoke(tmp_path, "port-busy")

    assert result.returncode == 1
    assert "port 8077 is already in use" in result.stderr
    assert calls == []


@pytest.mark.parametrize(("change", "message"), [
    (lambda text: text.replace("## Install\n", "## Setup\n"), "no ```sh block under '## Install'"),
    (lambda text: text.replace(
        "releases/download/vX.Y.Z/compose.yaml", "releases/download/v1.2.3/compose.yaml"
    ), "releases/download/vX.Y.Z/ 0 times"),
    (lambda text: text.replace("/vX.Y.Z/.env.example", "/main/.env.example"), "vX.Y.Z/ 0 times"),
])
def test_smoke_refuses_a_guide_it_cannot_run_offline(tmp_path, change, message):
    result, calls = _run_smoke(tmp_path, guide=_guide(tmp_path, change))

    assert result.returncode != 0
    assert message in result.stderr
    assert calls == []


@pytest.mark.parametrize(("change", "message"), [
    (lambda text: text.replace("    umask 077\n", "    umask 022\n", 1), ".env mode is 644"),
    (lambda text: text.replace("    mkdir odograph\n", "    mkdir -p odograph\n", 1).replace(
        "    set -C\n", "", 1
    ), "a second run of the install block succeeded"),
    (lambda text: text.replace("openssl rand -hex 32", "openssl rand -hex 16", 1),
     "POSTGRES_PASSWORD is not a usable value"),
])
def test_smoke_fails_a_guide_that_breaks_its_promises(tmp_path, change, message):
    result, calls = _run_smoke(tmp_path, guide=_guide(tmp_path, change))

    assert result.returncode == 1
    assert message in result.stderr
    assert calls[-1].endswith(" down -v")


def _install_job() -> dict:
    workflow = yaml.load(
        (ROOT / ".github/workflows/trial-smoke.yml").read_text(), Loader=yaml.BaseLoader
    )
    return workflow["jobs"]["install-smoke"]


def test_every_pull_request_installs_from_a_digest_rendered_asset():
    job = _install_job()
    steps = {step.get("name"): step for step in job["steps"]}
    names = [step.get("name") for step in job["steps"]]

    assert "if" not in job
    assert job["env"]["REGISTRY_IMAGE"].startswith("docker.io/library/registry:3@sha256:")
    publish = steps["Publish the application image to a local registry"]["run"]
    assert "-p 127.0.0.1::5000" in publish
    assert "docker push" in publish and 'docker rmi "$image:ci"' in publish
    render = steps["Render the install compose asset"]["run"]
    assert render.count("--mode install") == 2
    assert "--output" in render and "--verify" in render
    assert render.count("--source compose.yaml") == 2
    assert steps["Install with Docker Compose"]["run"] == (
        'scripts/install_smoke.sh "$RUNNER_TEMP/compose.yaml"'
    )
    podman = steps["Install with podman-compose 1.3.0"]
    assert podman["env"]["COMPOSE_CMD"] == "podman-compose"
    assert "--require-hashes" in podman["run"] and ".github/podman-compose.lock" in podman["run"]
    assert "insecure = true" in podman["run"]
    assert podman["run"].rstrip().endswith('scripts/install_smoke.sh "$RUNNER_TEMP/compose.yaml"')
    assert names.index("Render the install compose asset") < names.index("Install with Docker Compose")
    assert not any("continue-on-error" in step for step in job["steps"])
    assert "continue-on-error" not in job


_RELEASE_PREFIX = "https://github.com/MeandrousShark/odograph/releases/download/vX.Y.Z/"
_RAW_PREFIX = "https://raw.githubusercontent.com/MeandrousShark/odograph/vX.Y.Z/"
_SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(_SH is None, reason="sh is not available")
needs_curl = pytest.mark.skipif(REAL_CURL is None, reason="curl is not available")

_FAKE_DOCKER = '#!/bin/sh\necho "$*" >> "$FAKE_LOG"\n'
_FAKE_OPENSSL_OK = (
    "#!/bin/sh\necho 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
)
_FAKE_OPENSSL_FAIL = "#!/bin/sh\nexit 1\n"
# Real curl for file:// downloads; every other call is the health probe.
_FAKE_CURL = (
    "#!/bin/sh\n"
    'for arg in "$@"; do case "$arg" in file://*) exec "$REAL_CURL" "$@" ;; esac; done\n'
    'echo curl >> "$FAKE_LOG"\n'
    '[ "$FAKE_HEALTH" = up ]\n'
)


def _guide_block(heading: str, index: int = 0) -> str:
    lines = GUIDE.read_text().splitlines()
    position = lines.index(heading)
    for _ in range(index + 1):
        position = lines.index("```sh", position + 1)
    return "\n".join(lines[position + 1:lines.index("```", position + 1)]) + "\n"


def _run_block(
    tmp_path: Path, block: str, tools: dict[str, str], health: str = "up",
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    log = tmp_path / "log"
    log.touch()
    for name, source in {"sleep": "#!/bin/sh\nexit 0\n", **tools}.items():
        tool = fake_bin / name
        tool.write_text(source)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "block.sh"
    script.write_text(block)
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    environment.update(
        FAKE_LOG=str(log), FAKE_HEALTH=health, REAL_CURL=REAL_CURL or "", TZ_NAME="UTC",
    )
    result = subprocess.run(
        [_SH, str(script)], cwd=cwd or tmp_path, env=environment,
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    return result, log.read_text().splitlines()


def _install_setup(tmp_path: Path, missing: bool = False) -> tuple[str, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "compose.yaml").write_text("services: {}\n")
    (source / ".env.example").write_text("LOG_LEVEL=info\nPOSTGRES_PASSWORD=\nSESSION_SECRET=\nDISPLAY_TZ=x\n")
    local = f"file://{source / ('nowhere' if missing else '')}"
    local = local if missing else local + "/"
    block = _guide_block("## Install")
    for prefix in (_RELEASE_PREFIX, _RAW_PREFIX):
        assert block.count(prefix) == 1
        block = block.replace(prefix, local + ("/" if missing else ""))
    work = tmp_path / "work"
    work.mkdir()
    return block, work


@needs_sh
@needs_curl
@pytest.mark.parametrize("failure", ["openssl", "download"])
def test_install_block_stops_before_env_and_compose_on_a_failed_step(tmp_path, failure):
    block, work = _install_setup(tmp_path, missing=failure == "download")
    openssl = _FAKE_OPENSSL_FAIL if failure == "openssl" else _FAKE_OPENSSL_OK
    result, log = _run_block(
        tmp_path, block, {"docker": _FAKE_DOCKER, "openssl": openssl, "curl": _FAKE_CURL},
        cwd=work,
    )

    assert result.returncode != 0
    assert not (work / "odograph" / ".env").exists()
    assert log == []
    if failure == "openssl":
        # It got as far as the secrets, so the failure is the one under test.
        assert (work / "odograph" / ".env.example").exists()


@needs_sh
@needs_curl
def test_install_block_writes_a_private_env_then_starts_compose(tmp_path):
    block, work = _install_setup(tmp_path)
    result, log = _run_block(
        tmp_path, block,
        {"docker": _FAKE_DOCKER, "openssl": _FAKE_OPENSSL_OK, "curl": _FAKE_CURL}, cwd=work,
    )

    assert result.returncode == 0, result.stderr
    env = work / "odograph" / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "POSTGRES_PASSWORD=" + "0123456789abcdef" * 4 in env.read_text().splitlines()
    assert log == ["compose up -d"]


_HEALTH_ADMIN = "compose exec app python -m app.manage_account create-admin"


@needs_sh
def test_wait_block_gives_up_after_36_tries_without_creating_the_admin(tmp_path):
    result, log = _run_block(
        tmp_path, _guide_block("## Install", 1),
        {"docker": _FAKE_DOCKER, "curl": _FAKE_CURL}, health="down",
    )

    assert result.returncode != 0
    assert "not healthy after 3 minutes" in result.stderr
    assert log == ["curl"] * 36


@needs_sh
def test_wait_block_creates_the_admin_once_healthy(tmp_path):
    result, log = _run_block(
        tmp_path, _guide_block("## Install", 1),
        {"docker": _FAKE_DOCKER, "curl": _FAKE_CURL}, health="up",
    )

    assert result.returncode == 0, result.stderr
    assert log == ["curl", _HEALTH_ADMIN]


def _upgrade_run(tmp_path: Path, health: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "compose.yaml").write_text("services: {}\n")
    block = _guide_block("## Upgrading")
    assert block.count(_RELEASE_PREFIX) == 1
    block = block.replace(_RELEASE_PREFIX, f"file://{source}/")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("A=1\n")
    (work / "compose.yaml").write_text("old\n")
    for name in ("backup_database.sh", "restore_database.sh"):
        script = work / name
        script.write_text('#!/bin/sh\nmkdir -p backups\necho "$0" >> "$FAKE_LOG"\n')
        script.chmod(0o755)
    return _run_block(
        tmp_path, block, {"docker": _FAKE_DOCKER, "curl": _FAKE_CURL}, health=health, cwd=work,
    )


@needs_sh
@needs_curl
def test_upgrade_block_waits_a_bounded_time_for_health(tmp_path):
    result, log = _upgrade_run(tmp_path, "down")

    assert result.returncode != 0
    assert "not healthy after 3 minutes" in result.stderr
    assert log.count("curl") == 36
    assert log[:3] == ["./backup_database.sh", "./restore_database.sh", "compose up -d"]


@needs_sh
@needs_curl
def test_upgrade_block_succeeds_when_the_app_is_healthy(tmp_path):
    result, log = _upgrade_run(tmp_path, "up")

    assert result.returncode == 0, result.stderr
    assert log[-2:] == ["compose up -d", "curl"]
