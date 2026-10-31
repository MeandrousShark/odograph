"""compose.try.yaml has no authentication, so its isolation is pinned here."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MIN_SESSION_SECRET_LENGTH = 32
TRY_IMAGE_VARIABLE = (
    "${ODOGRAPH_TRY_IMAGE:?download compose.try.yaml from a GitHub release, see the header}"
)


@pytest.fixture(scope="module")
def trial() -> dict:
    return yaml.safe_load((ROOT / "compose.try.yaml").read_text())


@pytest.fixture(scope="module")
def install() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


SERVICE_KEYS = {
    "try-db": {"image", "environment", "tmpfs", "healthcheck"},
    "try-app": {
        "image", "depends_on", "environment", "ports", "security_opt", "cap_drop",
        "read_only", "tmpfs", "healthcheck",
    },
    "try-seed": {
        "image", "command", "depends_on", "environment", "network_mode", "security_opt",
        "cap_drop", "read_only", "tmpfs", "restart",
    },
}


def test_service_names_are_disjoint_from_the_installation(trial, install):
    # COMPOSE_PROJECT_NAME beats `name:`, so a shared service name is all it
    # would take for the trial to recreate an installation's container.
    assert not set(trial["services"]) & set(install["services"])


def test_each_service_has_exactly_the_reviewed_keys(trial):
    assert {name: set(service) for name, service in trial["services"].items()} == SERVICE_KEYS


def test_top_level_has_exactly_the_reviewed_keys(trial):
    assert trial["name"] == "odograph-try"
    assert set(trial) == {"name", "services", "networks", "x-podman"}


def test_network_and_pod_do_not_follow_the_project_name(trial):
    # This only keeps `up` apart. podman-compose's `down` selects networks and
    # volumes by project label, so -p and a new empty directory protect an
    # installation, as the header says.
    assert trial["networks"] == {"default": {"name": "odograph-try"}}
    assert trial["x-podman"] == {"in_pod": False}


def test_no_env_file_or_interpolation_is_needed(trial):
    assert not any("env_file" in service for service in trial["services"].values())
    text = (ROOT / "compose.try.yaml").read_text()
    assert text.count("$") == text.count(TRY_IMAGE_VARIABLE) == 2


def test_app_is_published_on_loopback_only(trial):
    assert trial["services"]["try-app"]["ports"] == ["127.0.0.1:8079:8000"]


def test_app_healthcheck_survives_podman_compose_1_3_0(trial):
    # It joins a CMD list into a broken shell line, so `podman wait` for a
    # healthy try-app would never return and try-seed would never start.
    assert trial["services"]["try-app"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert trial["services"]["try-db"]["healthcheck"]["test"][0] == "CMD-SHELL"


def test_nothing_else_publishes_a_port(trial):
    for name in ("try-db", "try-seed"):
        assert "ports" not in trial["services"][name], name
        assert "expose" not in trial["services"][name], name


def test_no_service_uses_host_networking(trial):
    assert all(service.get("network_mode") != "host" for service in trial["services"].values())


def test_app_runs_in_demo_mode_without_authentication(trial):
    environment = trial["services"]["try-app"]["environment"]

    assert environment["DEV_NO_AUTH"] == "1"
    assert environment["DEMO_MODE"] == "1"
    assert len(environment["SESSION_SECRET"]) >= MIN_SESSION_SECRET_LENGTH


def test_fixed_values_say_they_are_not_secrets(trial):
    db_password = trial["services"]["try-db"]["environment"]["POSTGRES_PASSWORD"]
    environment = trial["services"]["try-app"]["environment"]

    assert "not-a-secret" in db_password
    assert "not-a-secret" in environment["SESSION_SECRET"]
    assert f"mileage:{db_password}@try-db:5432/mileage" in environment["DATABASE_URL"]


def test_database_data_is_on_tmpfs_and_nothing_uses_a_volume(trial, install):
    data_directory = install["services"]["db"]["volumes"][0].split(":", 1)[1]

    assert trial["services"]["try-db"]["tmpfs"] == [data_directory]
    assert "volumes" not in trial
    assert not any("volumes" in service for service in trial["services"].values())


def test_healthy_ordering(trial):
    assert trial["services"]["try-app"]["depends_on"]["try-db"]["condition"] == "service_healthy"
    assert trial["services"]["try-seed"]["depends_on"]["try-app"]["condition"] == "service_healthy"
    assert "healthcheck" in trial["services"]["try-db"]
    assert "healthcheck" in trial["services"]["try-app"]


def test_seed_is_one_shot_and_reaches_the_database_over_loopback(trial):
    seed = trial["services"]["try-seed"]
    script = seed["command"][-1]

    assert seed["restart"] == "no"
    assert seed["network_mode"] == "service:try-db"
    assert seed["environment"] == {"DEMO_MODE": "1"}
    assert "@127.0.0.1:5432/mileage" in script
    assert "http://try-app:8000/healthz" in script
    assert script.index("/healthz") < script.index("scripts/dev_seed.py")
    assert "--wipe --scenario week" in script


@pytest.mark.parametrize("name", ["try-app", "try-seed"])
def test_application_containers_keep_the_hardening(trial, name):
    service = trial["services"][name]

    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["tmpfs"] == ["/tmp"]


def test_images_match_the_installation_or_refuse_to_start(trial, install):
    assert trial["services"]["try-db"]["image"] == install["services"]["db"]["image"]
    assert trial["services"]["try-app"]["image"] == TRY_IMAGE_VARIABLE
    assert trial["services"]["try-seed"]["image"] == TRY_IMAGE_VARIABLE


def test_header_states_the_risk_and_the_commands():
    header = " ".join((ROOT / "compose.try.yaml").read_text().split("name: odograph-try")[0].replace("#", " ").split())

    assert "mkdir odograph-try && cd odograph-try curl -fsSLO" in header
    assert "compose.try.yaml docker compose -p odograph-try -f compose.try.yaml up" in header
    assert "no .env is in scope" in header
    assert "Never omit -p." in header
    assert "Never run this file from an install directory." in header
    assert "Never add -v or --remove-orphans without -p" in header
    assert "docker compose -p odograph-try -f compose.try.yaml down" in header
    assert "pulled images stay" in header
    assert "github.com/MeandrousShark/odograph/releases/download/vX.Y.Z/compose.try.yaml" in header
    assert "NO AUTHENTICATION" in header
    assert "never expose it beyond loopback" in header
    assert "SSH tunnel" in header


def test_image_carries_the_seed_script():
    dockerfile = (ROOT / "Dockerfile").read_text()
    ignored = (ROOT / ".dockerignore").read_text().splitlines()

    assert "COPY scripts/dev_seed.py scripts/\n" in dockerfile
    assert "!scripts/dev_seed.py" in ignored
    assert ignored.index("!scripts/dev_seed.py") > ignored.index("scripts/*")


def test_smoke_script_checks_what_a_stranger_gets():
    source = (ROOT / "scripts/try_smoke.sh").read_text()

    assert "Demo mode: synthetic data, not persistent, authentication disabled." in source
    assert 'run_compose() {\n    "${compose[@]}" -p "$PROJECT"' in source
    assert "PROJECT=odograph-try" in source
    assert "seed-final-lines-in-up-output" in source


def test_every_pull_request_runs_the_trial_on_docker():
    workflow = yaml.load(
        (ROOT / ".github/workflows/trial-smoke.yml").read_text(), Loader=yaml.BaseLoader
    )
    job = workflow["jobs"]["trial-smoke"]
    steps = {step.get("name"): step for step in job["steps"]}

    assert "pull_request" in workflow["on"]
    assert "paths" not in workflow["on"]["pull_request"]
    assert "if" not in job
    assert workflow["permissions"] == {"contents": "read"}
    assert job["env"]["ODOGRAPH_TRY_IMAGE"]
    assert "docker build" in steps["Build the application image"]["run"]
    docker = steps["Run the trial with Docker Compose"]
    assert docker["run"] == "scripts/try_smoke.sh compose.try.yaml"
    assert not any("continue-on-error" in step for step in job["steps"])
    assert "continue-on-error" not in job


def test_podman_compose_is_installed_by_hash():
    workflow = (ROOT / ".github/workflows/trial-smoke.yml").read_text()
    lock = (ROOT / ".github/podman-compose.lock").read_text().splitlines()

    assert "--require-hashes" in workflow and ".github/podman-compose.lock" in workflow
    assert lock[0].startswith("podman-compose==1.3.0 --hash=sha256:")
    assert all(" --hash=sha256:" in line for line in lock)


FAKE_DOCKER = r"""#!/usr/bin/env bash
state="$FAKE_STATE"
containers_exist() { [ -e "$state/up" ] && [ ! -e "$state/gone" ]; }
case "$1" in
compose)
    echo "$*" >> "$state/calls"
    case "$*" in
    *" up")
        touch "$state/up"
        echo "try-seed | Demo mode: synthetic data, not persistent, authentication disabled."
        echo "try-seed | http://127.0.0.1:8079"
        if [ "$FAKE_SCENARIO" = compose-dies ]; then
            exit 1
        fi
        until [ -e "$state/stop" ]; do sleep 1; done
        ;;
    *" down -v") touch "$state/stop" "$state/gone" ;;
    *" down")
        touch "$state/stop"
        [ "$FAKE_SCENARIO" = down-leaves-containers ] || touch "$state/gone"
        ;;
    esac
    ;;
ps) containers_exist && echo seed ;;
inspect)
    case "$*" in
    *State.Status*)
        if [ "$FAKE_SCENARIO" = compose-dies ]; then echo created; else echo exited; fi
        ;;
    *ExitCode*)
        if [ "$FAKE_SCENARIO" = seed-fails ]; then echo 1; else echo 0; fi
        ;;
    *) echo '{"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8079"}]}' ;;
    esac
    ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
while [ $# -gt 0 ]; do
    case "$1" in
    -o) output="$2"; shift ;;
    -w) code=200 ;;
    esac
    shift
done
if [ "$FAKE_SCENARIO" = no-banner ]; then
    echo trip-archive-row > "$output"
else
    echo "Demo mode: synthetic data, not persistent, authentication disabled. trip-archive-row" > "$output"
fi
[ -z "${code:-}" ] || printf %s "$code"
"""

FAKE_SS = r"""#!/usr/bin/env bash
if [ -e "$FAKE_STATE/up" ] && [ ! -e "$FAKE_STATE/gone" ]; then
    echo "LISTEN 0 4096 127.0.0.1:8079 0.0.0.0:*"
fi
"""


def _run_smoke(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    tools = {
        "docker": FAKE_DOCKER, "curl": FAKE_CURL, "ss": FAKE_SS,
        "sleep": "#!/bin/sh\nexec /bin/sleep 0.05\n",
    }
    for name, source in tools.items():
        tool = fake_bin / name
        tool.write_text(source)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"COMPOSE_CMD", "GITHUB_STEP_SUMMARY"}
    }
    environment.update(
        PATH=f"{fake_bin}{os.pathsep}{environment['PATH']}", FAKE_STATE=str(state),
        FAKE_SCENARIO=scenario, TRY_SMOKE_TIMEOUT="30",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/try_smoke.sh"), str(ROOT / "compose.try.yaml")],
        env=environment, capture_output=True, text=True, timeout=45,
    )
    return result, (state / "calls").read_text().splitlines()


def test_smoke_passes_a_working_trial_with_a_plain_down(tmp_path):
    result, calls = _run_smoke(tmp_path, "works")

    assert result.returncode == 0, result.stderr
    assert "ok: down left no containers, network or volumes" in result.stdout
    assert calls[-1].endswith(" down")
    assert not any(call.endswith(" down -v") for call in calls)


@pytest.mark.parametrize(("scenario", "message"), [
    ("seed-fails", "try-seed exited with 1"),
    ("no-banner", "dashboard has no banner"),
    ("compose-dies", "compose exited before try-seed finished"),
    ("down-leaves-containers", "containers remain after down"),
])
def test_smoke_fails_a_broken_trial_and_cleans_up(tmp_path, scenario, message):
    result, calls = _run_smoke(tmp_path, scenario)

    assert result.returncode == 1
    assert message in result.stderr
    assert calls[-1].endswith(" down -v")
