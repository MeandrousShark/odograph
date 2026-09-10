from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _documented_environment_names() -> set[str]:
    text = (ROOT / "docs" / "configuration.md").read_text()
    return set(re.findall(r"`([A-Z][A-Z0-9_]*(?:<YEAR>)?)`", text))


def _application_environment_names() -> set[str]:
    text = (ROOT / "app" / "config.py").read_text()
    names = set(
        re.findall(r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]*)", text)
    )
    names.update(
        re.findall(r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]*)", text)
    )
    names.update(re.findall(r"_f\(\s*[\"']([A-Z][A-Z0-9_]*)", text))
    return names


def test_configuration_reference_covers_every_runtime_and_compose_setting():
    documented = _documented_environment_names()
    required = _application_environment_names() | {
        "POSTGRES_PASSWORD",
        "OSRM_DATASET",
        "MILEAGE_RATE_<YEAR>",
        "ADMIN_TOKEN",
    }

    assert required - documented == set()


def test_env_example_is_only_the_runnable_baseline():
    active = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    }

    assert active == {
        "POSTGRES_PASSWORD",
        "INGEST_PASSWORD",
        "SESSION_SECRET",
        "INITIAL_ADMIN_SIGNUP",
        "DISPLAY_TZ",
        "FORWARDED_ALLOW_IPS",
    }


def test_quickstart_separates_critical_path_from_post_install_operations():
    # Keep the first-use path short and ordered. Optional operations belong in
    # their own sections so an operator can reach a signed-in instance without
    # making backup or release-policy decisions first.
    readme = (ROOT / "README.md").read_text()
    critical = readme.split("## Quick start", 1)[1].split("## Connect OwnTracks", 1)[0]
    clone = critical.index("git clone --branch vX.Y.Z --depth 1")
    generate_env = critical.index("scripts/generate_env.sh")
    signup_disabled = critical.index("INITIAL_ADMIN_SIGNUP=0")
    start = critical.index("docker compose up -d")
    create_admin = critical.index("python -m app.manage_account create-admin")
    https = critical.index("## Set up HTTPS")

    assert clone < generate_env < signup_disabled < start < create_admin
    assert "git checkout vX.Y.Z" not in critical
    assert "docker compose up -d" in critical
    assert "podman-compose up -d" in critical
    assert "pull app" not in critical
    assert "/signup" in critical
    assert "ADMIN_TOKEN" not in critical
    assert "backup" not in critical.lower()
    assert "restore" not in critical.lower()
    assert "floating tag" not in critical.lower()
    assert "latest tag" not in critical.lower()
    assert create_admin < https

    post_install = readme.split("## Connect OwnTracks", 1)[1].split(
        "## Password recovery", 1
    )[0]
    assert "Account Settings" in post_install
    assert "docs/owntracks.md" in post_install
    assert "backup_database.sh" in post_install
    assert "docs/security.md#hardening-checklist" in post_install
    assert "docs/configuration.md#external-services" in post_install


def test_compose_preserves_state_and_waits_for_healthy_database():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    db = compose["services"]["db"]
    app = compose["services"]["app"]

    assert db["volumes"] == ["dbdata:/var/lib/postgresql/data"]
    assert "dbdata" in compose["volumes"]
    assert db["restart"] == "unless-stopped"
    assert app["restart"] == "unless-stopped"
    assert app["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert app["ports"] == ["127.0.0.1:8077:8000"]


def test_operator_guides_cover_both_supported_reverse_proxy_examples():
    guide = (ROOT / "docs" / "reverse-proxy.md").read_text()

    assert "## Caddy" in guide
    assert "reverse_proxy 127.0.0.1:8077" in guide
    assert "## nginx" in guide
    assert "proxy_pass http://127.0.0.1:8077" in guide
    assert "proxy_set_header X-Forwarded-Proto $scheme" in guide
