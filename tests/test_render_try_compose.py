"""The release asset must name the published manifest by digest and nothing else."""
from __future__ import annotations

import re
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


@pytest.mark.parametrize("digest", [
    "", "sha256:abc", "v1.2.3", "sha256:" + "A" * 64, DIGEST + "x", DIGEST + "\n", " " + DIGEST,
])
def test_render_rejects_anything_but_a_full_digest(tmp_path, digest):
    output = tmp_path / "rendered.yaml"
    result = _run("--digest", digest, "--source", str(ROOT / "compose.try.yaml"), "--output", str(output))

    assert result.returncode != 0
    assert not output.exists()


PINNED_LINE = f'    image: "{IMAGE}@{DIGEST}"\n'
# Each keeps both correct image lines, which a line scan accepted.
WRONG_FILES = {
    "port on every interface": lambda text: text.replace(
        "127.0.0.1:8079:8000", "0.0.0.0:8079:8000"
    ),
    "database on a tag": lambda text: re.sub(
        r"odograph-postgis@sha256:[0-9a-f]{64}", "odograph-postgis:16", text
    ),
    "truncated after the seed image": lambda text: text[
        :text.index(PINNED_LINE, text.index("try-seed:\n")) + len(PINNED_LINE)
    ],
    "networks removed": lambda text: text[:text.index("networks:\n")],
    "duplicate image key": lambda text: text.replace(
        PINNED_LINE, f"{PINNED_LINE}    image: {IMAGE}:v1.2.3\n"
    ),
    "decoy block": lambda text: f"x-decoy:\n  try-app:\n    image: {IMAGE}@{DIGEST}\n" + text.replace(
        f"{IMAGE}@{DIGEST}", f"{IMAGE}:v1.2.3"
    ),
    "trailing newline": lambda text: text + "\n",
    "comment changed": lambda text: text.replace("NO AUTHENTICATION", "no authentication"),
}


@pytest.mark.parametrize("name", WRONG_FILES)
def test_verify_rejects_a_wrong_file(tmp_path, name):
    output = _render(tmp_path)
    original = output.read_text()
    wrong = WRONG_FILES[name](original)
    assert wrong != original
    output.write_text(wrong)

    result = _run("--digest", DIGEST, "--verify", str(output))

    assert result.returncode != 0
    assert "differs from the render" in result.stderr


def test_verify_compares_bytes_not_translated_text(tmp_path):
    output = _render(tmp_path)
    output.write_bytes(output.read_bytes().replace(b"\n", b"\r\n"))

    assert _run("--digest", DIGEST, "--verify", str(output)).returncode != 0


def test_verify_compares_against_the_given_source(tmp_path):
    output = _render(tmp_path)
    source = tmp_path / "compose.try.yaml"
    source.write_text((ROOT / "compose.try.yaml").read_text().replace("interval: 2s", "interval: 9s"))

    result = _run("--digest", DIGEST, "--source", str(source), "--verify", str(output))

    assert result.returncode != 0
    assert "differs from the render" in result.stderr


def test_verify_refuses_a_missing_source(tmp_path):
    result = _run(
        "--digest", DIGEST, "--source", str(tmp_path / "absent.yaml"), "--verify", str(_render(tmp_path))
    )

    assert result.returncode != 0
    assert result.stderr.startswith("try compose error:")


def test_render_refuses_a_source_without_the_variable(tmp_path):
    source = tmp_path / "compose.try.yaml"
    shutil.copy(ROOT / "compose.try.yaml", source)
    source.write_text(source.read_text().split("  try-seed:")[0])

    assert _run("--digest", DIGEST, "--source", str(source), "--output", str(tmp_path / "o")).returncode != 0
