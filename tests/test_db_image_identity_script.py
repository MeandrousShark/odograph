"""Tests for scripts/db_image_identity.sh.

A fake runtime returns fixture facts, so the parsing and refusal rules run
without containers. One test probes the real Compose database image when a
runtime already has it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "db_image_identity.sh"
IMAGE = "registry.example.test/postgis@sha256:" + "a" * 64

DEBIAN_FACTS = {
    "postgres_version": "postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+2)",
    "postgis_default_version": "default_version = '3.6.4'",
    "postgis_library": "/usr/lib/postgresql/16/lib/postgis-3.so",
    "geos_library": "libgeos.so.3.11.1",
    "proj_library": "libproj.so.25.9.8.1",
    "icu_library": "libicuuc.so.72.1",
    "glibc_version": "glibc 2.36",
}
DEBIAN_IDENTITY = (
    "postgresql=16.15 postgis=3.6.4 geos=3.11.1 proj=9.8.1 glibc=2.36 icu=72.1"
)

_FAKE_RUNTIME = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$FAKE_ARGS"
cat "$FAKE_FACTS"
exit "${FAKE_EXIT:-0}"
"""


def _run(tmp_path: Path, facts: dict[str, str] | None, *args: str, exit_code: int = 0):
    runtime = tmp_path / "runtime"
    runtime.write_text(_FAKE_RUNTIME)
    runtime.chmod(0o755)
    facts_file = tmp_path / "facts"
    facts_file.write_text("".join(f"{key}={value}\n" for key, value in (facts or {}).items()))
    args_file = tmp_path / "args"
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "CONTAINER_RUNTIME": str(runtime),
            "FAKE_FACTS": str(facts_file),
            "FAKE_ARGS": str(args_file),
            "FAKE_EXIT": str(exit_code),
        },
    )
    runtime_args = args_file.read_text().splitlines() if args_file.exists() else None
    return result, runtime_args


def test_debian_facts_give_the_canonical_identity(tmp_path):
    result, runtime_args = _run(tmp_path, DEBIAN_FACTS, IMAGE)

    assert result.returncode == 0, result.stderr
    assert result.stdout == DEBIAN_IDENTITY + "\n"
    assert runtime_args[:8] == [
        "run", "--rm", "--network", "none", "--read-only",
        "--entrypoint", "/bin/sh", IMAGE,
    ]
    assert runtime_args[8] == "-c"
    # No volume, environment or port reaches the probe container.
    assert not {"-v", "--volume", "--mount", "-e", "--env", "-p", "--publish"} & set(runtime_args[:8])


def test_package_revisions_are_not_part_of_the_identity(tmp_path):
    rebuilt = {
        **DEBIAN_FACTS,
        "postgres_version": "postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+3)",
        "postgis_default_version": "default_version='3.6.4'  ",
        "postgis_library": "/usr/local/lib/postgresql/postgis-3.so",
    }

    result, _ = _run(tmp_path, rebuilt, IMAGE)

    assert result.returncode == 0, result.stderr
    assert result.stdout == DEBIAN_IDENTITY + "\n"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("postgres_version", "postgres (PostgreSQL) 16.16 (Debian 16.16-1.pgdg12+1)", "postgresql=16.16"),
        ("postgres_version", "postgres (PostgreSQL) 17.2", "postgresql=17.2"),
        ("postgis_default_version", "default_version = '3.6.5'", "postgis=3.6.5"),
        ("geos_library", "libgeos.so.3.12.2", "geos=3.12.2"),
        ("geos_library", "libgeos-3.9.1.so", "geos=3.9.1"),
        ("proj_library", "libproj.so.25.9.8.2", "proj=9.8.2"),
        ("icu_library", "libicuuc.so.76.1", "icu=76.1"),
        ("glibc_version", "glibc 2.41", "glibc=2.41"),
    ],
)
def test_each_component_version_changes_the_identity(tmp_path, key, value, expected):
    result, _ = _run(tmp_path, {**DEBIAN_FACTS, key: value}, IMAGE)

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout.split()
    assert result.stdout != DEBIAN_IDENTITY + "\n"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("postgres_version", "postgres (PostgreSQL) 17beta1", "PostgreSQL version"),
        ("postgis_default_version", "default_version = '3.6'", "PostGIS version"),
        ("postgis_default_version", "default_version = '3.6.4dev'", "PostGIS version"),
        ("postgis_library", "", "names no installed PostGIS library"),
        ("geos_library", "", "GEOS version"),
        ("geos_library", "libgeos_c.so.1.17.1", "GEOS version"),
        ("proj_library", "libproj.so.25", "PROJ version"),
        ("proj_library", "", "PROJ version"),
        ("icu_library", "libicuuc.so.72", "ICU version"),
        ("icu_library", "", "ICU version"),
        ("glibc_version", "", "glibc version"),
        ("glibc_version", "musl 1.2.5", "glibc version"),
    ],
)
def test_an_undeterminable_component_fails_closed(tmp_path, key, value, message):
    result, _ = _run(tmp_path, {**DEBIAN_FACTS, key: value}, IMAGE)

    assert result.returncode != 0
    assert result.stdout == ""
    assert message in result.stderr
    assert IMAGE in result.stderr


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        ({}, "not a PostgreSQL image"),
        ({"postgres_version": DEBIAN_FACTS["postgres_version"]}, "not a PostGIS image"),
        (
            {
                "postgres_version": DEBIAN_FACTS["postgres_version"],
                "postgis_default_version": DEBIAN_FACTS["postgis_default_version"],
                "postgis_library": "",
            },
            "not a PostGIS image",
        ),
    ],
)
def test_a_non_postgis_image_is_refused(tmp_path, facts, message):
    result, _ = _run(tmp_path, facts, IMAGE)

    assert result.returncode != 0
    assert result.stdout == ""
    assert message in result.stderr


def test_a_failed_container_run_fails_closed(tmp_path):
    result, _ = _run(tmp_path, DEBIAN_FACTS, IMAGE, exit_code=125)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "could not run the image" in result.stderr


@pytest.mark.parametrize("args", [(), (IMAGE, IMAGE), ("--pull",), ("",)])
def test_usage_errors_never_run_a_container(tmp_path, args):
    result, runtime_args = _run(tmp_path, DEBIAN_FACTS, *args)

    assert result.returncode != 0
    assert "usage: scripts/db_image_identity.sh IMAGE" in result.stderr
    assert runtime_args is None


def test_script_passes_shellcheck():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed")

    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout
    assert os.access(SCRIPT, os.X_OK)


def _runtime_with_image(image: str) -> str | None:
    candidates = [os.environ["CONTAINER_RUNTIME"]] if os.environ.get("CONTAINER_RUNTIME") else ["docker", "podman"]
    for runtime in candidates:
        if not shutil.which(runtime):
            continue
        present = subprocess.run(
            [runtime, "image", "inspect", image], capture_output=True, timeout=20
        )
        if present.returncode == 0:
            return runtime
    return None


def test_the_compose_database_image_reports_its_identity():
    image = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["db"]["image"]
    runtime = _runtime_with_image(image)
    if runtime is None:
        pytest.skip("no container runtime has the Compose database image locally")

    result = subprocess.run(
        ["bash", str(SCRIPT), image],
        capture_output=True,
        text=True,
        timeout=50,
        env={**os.environ, "CONTAINER_RUNTIME": runtime},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == DEBIAN_IDENTITY + "\n"
