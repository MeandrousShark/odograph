from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_COMPOSE = ROOT / "compose.yaml"
BUILD_OVERRIDE = ROOT / "compose.build.override.yml"
ENV_EXAMPLE = ROOT / ".env.example"
RELEASE_IMAGE = "ghcr.io/meandrousshark/odograph:v0.6.0"


def load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


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


def test_env_example_ships_osrm_dataset_commented_out():
    # An uncommented default here would silently point a new operator at
    # the maintainer's own region; commenting it out lets compose.yaml's
    # `OSRM_DATASET:?` guard fire for anyone who starts the `osrm` profile
    # before running scripts/provision_osrm.sh.
    lines = ENV_EXAMPLE.read_text().splitlines()
    dataset_lines = [line for line in lines if "OSRM_DATASET" in line]

    assert dataset_lines, "expected an OSRM_DATASET line in .env.example"
    assert all(line.startswith("#") for line in dataset_lines)
    assert not any(line.strip() == "OSRM_DATASET=washington-latest.osrm" for line in lines)
