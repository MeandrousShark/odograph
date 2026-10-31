"""The release asset must name the published manifest by digest and nothing else."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/meandrousshark/odograph"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/render_try_compose.py"), "--image", IMAGE, *args],
        capture_output=True, text=True,
    )


def _render(tmp_path: Path) -> Path:
    output = tmp_path / "rendered.yaml"
    result = _run("--digest", DIGEST, "--source", str(ROOT / "compose.try.yaml"), "--output", str(output))
    assert result.returncode == 0, result.stderr
    return output


def test_rendered_file_pins_both_services_to_the_digest(tmp_path):
    rendered = yaml.safe_load(_render(tmp_path).read_text())
    source = yaml.safe_load((ROOT / "compose.try.yaml").read_text())

    assert rendered["services"]["try-app"]["image"] == f"{IMAGE}@{DIGEST}"
    assert rendered["services"]["try-seed"]["image"] == f"{IMAGE}@{DIGEST}"
    assert rendered["services"]["try-db"] == source["services"]["try-db"]


def test_rendered_file_has_no_variable_or_tag(tmp_path):
    text = _render(tmp_path).read_text()

    assert "ODOGRAPH_TRY_IMAGE" not in text
    assert "$" not in text
    assert f"{IMAGE}:" not in text


def test_verify_accepts_the_rendered_file(tmp_path):
    output = _render(tmp_path)

    assert _run("--digest", DIGEST, "--verify", str(output)).returncode == 0


def test_verify_rejects_another_digest(tmp_path):
    result = _run("--digest", OTHER_DIGEST, "--verify", str(_render(tmp_path)))

    assert result.returncode != 0
    assert "expected" in result.stderr


def test_verify_rejects_the_unrendered_file():
    result = _run("--digest", DIGEST, "--verify", str(ROOT / "compose.try.yaml"))

    assert result.returncode != 0
    assert "still contains a variable" in result.stderr


def test_verify_rejects_a_tag(tmp_path):
    output = _render(tmp_path)
    output.write_text(output.read_text().replace(f"{IMAGE}@{DIGEST}", f"{IMAGE}:v1.2.3", 1))

    assert _run("--digest", DIGEST, "--verify", str(output)).returncode != 0


@pytest.mark.parametrize("digest", ["", "sha256:abc", "v1.2.3", "sha256:" + "A" * 64])
def test_render_rejects_anything_but_a_full_digest(tmp_path, digest):
    output = tmp_path / "rendered.yaml"
    result = _run("--digest", digest, "--source", str(ROOT / "compose.try.yaml"), "--output", str(output))

    assert result.returncode != 0
    assert not output.exists()


def test_render_refuses_a_source_without_the_variable(tmp_path):
    source = tmp_path / "compose.try.yaml"
    shutil.copy(ROOT / "compose.try.yaml", source)
    source.write_text(source.read_text().split("  try-seed:")[0])

    assert _run("--digest", DIGEST, "--source", str(source), "--output", str(tmp_path / "o")).returncode != 0
