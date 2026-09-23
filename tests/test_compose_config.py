import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_COMPOSE = ROOT / "compose.yaml"
BUILD_OVERRIDE = ROOT / "compose.build.override.yml"
ENV_EXAMPLE = ROOT / ".env.example"
GENERATE_ENV = ROOT / "scripts" / "generate_env.sh"
RELEASE_IMAGE = "ghcr.io/meandrousshark/odograph:v0.11.2"

REQUIRED_VARIABLE_GUARD = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")

# Both supported Compose frontends, as the operator-facing docs name them.
COMPOSE_FRONTENDS = {
    "docker compose": ["docker", "compose"],
    "podman-compose": ["podman-compose"],
}


def load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def stage_install(tmp_path: Path) -> Path:
    """A throwaway copy of the files a clean install starts from, with .env generated."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "generate_env.sh").write_bytes(GENERATE_ENV.read_bytes())
    (tmp_path / ".env.example").write_bytes(ENV_EXAMPLE.read_bytes())
    (tmp_path / "compose.yaml").write_bytes(CANONICAL_COMPOSE.read_bytes())

    subprocess.run(
        ["bash", str(tmp_path / "scripts" / "generate_env.sh")],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return tmp_path


def clean_install_env() -> dict:
    # Every variable compose.yaml reads must come from the generated .env,
    # never from whatever the developer running the tests happens to export.
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSRM_")}
    env["COMPOSE_PROJECT_NAME"] = "composeconfigtest"
    return env


def available_frontend(argv: list[str]) -> bool:
    if shutil.which(argv[0]) is None:
        return False
    probe = subprocess.run([*argv, "version"], capture_output=True, text=True)
    return probe.returncode == 0


def test_canonical_compose_pins_the_release_image_without_a_build():
    app = load_compose(CANONICAL_COMPOSE)["services"]["app"]

    assert app["image"] == RELEASE_IMAGE
    assert "build" not in app


def test_contributor_override_restores_a_project_scoped_source_build():
    app = load_compose(BUILD_OVERRIDE)["services"]["app"]

    assert app == {
        "build": ".",
        "image": "${COMPOSE_PROJECT_NAME}_app:dev",
    }


def test_canonical_compose_keeps_podman_compatible_default_network():
    compose = load_compose(CANONICAL_COMPOSE)

    assert compose["networks"] == {"default": None}


def test_app_service_drops_privileges():
    app = load_compose(CANONICAL_COMPOSE)["services"]["app"]

    assert app["security_opt"] == ["no-new-privileges:true"]
    assert app["cap_drop"] == ["ALL"]


def test_app_service_runs_read_only_with_a_tmp_tmpfs():
    app = load_compose(CANONICAL_COMPOSE)["services"]["app"]

    assert app["read_only"] is True
    assert app["tmpfs"] == ["/tmp"]


def test_env_example_does_not_enable_an_osrm_dataset():
    # An uncommented default would silently point a new operator at a region.
    # The concise baseline omits optional settings and the complete reference
    # names the variable for operators who deliberately enable the profile.
    lines = ENV_EXAMPLE.read_text().splitlines()

    assert not any(line.startswith("OSRM_DATASET=") for line in lines)
    assert "`OSRM_DATASET`" in (ROOT / "docs" / "configuration.md").read_text()


def test_no_required_variable_guard_depends_on_a_variable_a_clean_install_lacks(tmp_path):
    # Compose interpolates every service in the file before it applies
    # profile filtering, so a `:?` guard on an opt-in service's variable
    # refuses to render the file at all for operators who never enable that
    # service. A clean install could not run `up -d`, `ps`, or any of the
    # backup/restore scripts until this invariant held.
    generated = stage_install(tmp_path) / ".env"
    values = dict(
        line.split("=", 1)
        for line in generated.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )

    guarded = set(REQUIRED_VARIABLE_GUARD.findall(CANONICAL_COMPOSE.read_text()))

    unsatisfied = sorted(name for name in guarded if not values.get(name))
    assert not unsatisfied, (
        f"compose.yaml requires {unsatisfied} but a freshly generated .env leaves "
        "them unset or commented out"
    )


def test_osrm_command_defers_its_dataset_check_to_the_container_shell():
    # `$$` is what keeps the guard testable at run time: a single `$` would
    # have Compose substitute the value while rendering the file. List form
    # is what keeps podman-compose from shell-splitting the script into
    # words before it substitutes anything.
    osrm = load_compose(CANONICAL_COMPOSE)["services"]["osrm"]

    assert isinstance(osrm["command"], list)
    script = osrm["command"][-1]
    assert "$${OSRM_DATASET}" in script
    assert "${OSRM_DATASET}" not in script.replace("$${OSRM_DATASET}", "")
    assert osrm["environment"]["OSRM_DATASET"] == "${OSRM_DATASET:-}"


@pytest.mark.parametrize("name,argv", sorted(COMPOSE_FRONTENDS.items()))
def test_clean_install_renders_only_the_baseline_services(tmp_path, name, argv):
    if not available_frontend(argv):
        pytest.skip(f"{name} is not installed")

    install = stage_install(tmp_path)
    result = subprocess.run(
        [*argv, "config", "--services"],
        cwd=install,
        env=clean_install_env(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.split()) == ["app", "db"]
