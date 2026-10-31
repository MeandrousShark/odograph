"""compose.try.yaml has no authentication, so its isolation is pinned here."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MIN_SESSION_SECRET_LENGTH = 32


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
    # podman-compose's `down` removes both by project-derived name.
    assert trial["networks"] == {"default": {"name": "odograph-try"}}
    assert trial["x-podman"] == {"in_pod": False}


def test_no_env_file_or_interpolation_is_needed(trial):
    assert not any("env_file" in service for service in trial["services"].values())
    assert "$" not in (ROOT / "compose.try.yaml").read_text()


def test_app_is_published_on_loopback_only(trial):
    assert trial["services"]["try-app"]["ports"] == ["127.0.0.1:8079:8000"]


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
    assert seed["image"] == trial["services"]["try-app"]["image"]
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


def test_images_match_the_installation_file(trial, install):
    assert trial["services"]["try-app"]["image"] == install["services"]["app"]["image"]
    assert trial["services"]["try-db"]["image"] == install["services"]["db"]["image"]
    assert re.search(r":v\d+\.\d+\.\d+$", trial["services"]["try-app"]["image"])


def test_header_states_the_risk_and_the_commands():
    header = " ".join((ROOT / "compose.try.yaml").read_text().split("name: odograph-try")[0].replace("#", " ").split())

    assert "docker compose -p odograph-try -f compose.try.yaml up" in header
    assert "docker compose -p odograph-try -f compose.try.yaml down" in header
    assert "pulled images stay" in header
    assert "compose.try.yaml" in header and "curl" in header
    assert "NO AUTHENTICATION" in header
    assert "never expose it beyond loopback" in header
    assert "SSH tunnel" in header


def test_image_carries_the_seed_script():
    dockerfile = (ROOT / "Dockerfile").read_text()
    ignored = (ROOT / ".dockerignore").read_text().splitlines()

    assert "COPY scripts/dev_seed.py scripts/\n" in dockerfile
    assert "!scripts/dev_seed.py" in ignored
    assert ignored.index("!scripts/dev_seed.py") > ignored.index("scripts/*")
