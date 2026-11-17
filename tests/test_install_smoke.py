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
