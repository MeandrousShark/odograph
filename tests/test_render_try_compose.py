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


def test_try_is_the_default_mode(tmp_path):
    default = _render(tmp_path)
    explicit = tmp_path / "explicit.yaml"
    result = _run("--mode", "try", "--digest", DIGEST, "--output", str(explicit))

    assert result.returncode == 0, result.stderr
    assert explicit.read_bytes() == default.read_bytes()


def _run_install(*args: str) -> subprocess.CompletedProcess[str]:
    return _run("--mode", "install", *args)


def _render_install(tmp_path: Path) -> Path:
    output = tmp_path / "compose.yaml"
    result = _run_install("--digest", DIGEST, "--source", str(ROOT / "compose.yaml"), "--output", str(output))
    assert result.returncode == 0, result.stderr
    return output


def test_install_render_changes_only_the_app_image_block(tmp_path):
    source = (ROOT / "compose.yaml").read_text().splitlines(keepends=True)
    rendered = _render_install(tmp_path).read_text().splitlines(keepends=True)
    removed = [line for line in source if line not in rendered]
    added = [line for line in rendered if line not in source]

    tag_line = next(line for line in source if line.startswith(f"    image: {IMAGE}:v"))
    assert [line for line in removed if not line.lstrip().startswith("#")] == [
        tag_line, "    pull_policy: never\n",
    ]
    assert all(line.startswith("    # ") for line in removed[:-2])
    assert added[-1] == f"    image: {IMAGE}@{DIGEST}\n"
    assert all(line.startswith("    # ") for line in added[:-1])
    comment = "".join(line.removeprefix("    # ").rstrip("\n") + " " for line in added[:-1])
    assert "published release digest" in comment
    assert "does not check its signature" in comment
    assert "scripts/verify_image.sh" in comment
    assert len(rendered) == len(source) - len(removed) + len(added)


def test_install_render_keeps_every_variable_and_the_other_services(tmp_path):
    text = _render_install(tmp_path).read_text()
    source_text = (ROOT / "compose.yaml").read_text()
    rendered = yaml.safe_load(text)
    source = yaml.safe_load(source_text)

    assert re.findall(r"\$\{[^}]*\}", text) == re.findall(r"\$\{[^}]*\}", source_text)
    assert "${POSTGRES_PASSWORD:?set in .env}" in text
    for key in ("volumes", "networks"):
        assert rendered[key] == source[key]
    for service in ("db", "osrm"):
        assert rendered["services"][service] == source["services"][service]
    app = dict(source["services"]["app"], image=f"{IMAGE}@{DIGEST}")
    del app["pull_policy"]
    assert rendered["services"]["app"] == app


def test_install_verify_accepts_the_rendered_file(tmp_path):
    output = _render_install(tmp_path)

    assert _run_install("--digest", DIGEST, "--verify", str(output)).returncode == 0


def test_install_mode_defaults_to_the_repository_compose(tmp_path):
    output = tmp_path / "default.yaml"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/render_try_compose.py"), "--mode", "install",
         "--image", IMAGE, "--digest", DIGEST, "--output", str(output)],
        capture_output=True, text=True, cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == _render_install(tmp_path).read_bytes()


def test_install_verify_rejects_another_digest(tmp_path):
    result = _run_install("--digest", OTHER_DIGEST, "--verify", str(_render_install(tmp_path)))

    assert result.returncode != 0
    assert result.stderr.startswith("install compose error:")
    assert "differs from the render" in result.stderr


def test_install_verify_rejects_the_unrendered_file():
    result = _run_install("--digest", DIGEST, "--verify", str(ROOT / "compose.yaml"))

    assert result.returncode != 0
    assert "still never pulls" in result.stderr


@pytest.mark.parametrize("digest", [
    "", "sha256:abc", "v1.2.3", "sha256:" + "A" * 64, DIGEST + "x", DIGEST + "\n", " " + DIGEST,
])
def test_install_render_rejects_anything_but_a_full_digest(tmp_path, digest):
    output = tmp_path / "rendered.yaml"
    result = _run_install("--digest", digest, "--source", str(ROOT / "compose.yaml"), "--output", str(output))

    assert result.returncode != 0
    assert not output.exists()


INSTALL_WRONG_FILES = {
    "variable expanded": lambda text: text.replace(
        "${POSTGRES_PASSWORD:?set in .env}", "secret", 1
    ),
    "forwarded ips fixed": lambda text: text.replace("${FORWARDED_ALLOW_IPS}", "'*'"),
    "port on every interface": lambda text: text.replace("127.0.0.1:8077:8000", "8077:8000"),
    "database on a tag": lambda text: re.sub(
        r"odograph-postgis@sha256:[0-9a-f]{64}", "odograph-postgis:16", text
    ),
    "pull policy back": lambda text: text.replace(
        f"    image: {IMAGE}@{DIGEST}\n", f"    image: {IMAGE}@{DIGEST}\n    pull_policy: never\n"
    ),
    "app on a tag": lambda text: text.replace(f"{IMAGE}@{DIGEST}", f"{IMAGE}:v1.2.3"),
    "comment changed": lambda text: text.replace("does not check its signature", "checks its signature"),
    "trailing newline": lambda text: text + "\n",
}


@pytest.mark.parametrize("name", INSTALL_WRONG_FILES)
def test_install_verify_rejects_a_wrong_file(tmp_path, name):
    output = _render_install(tmp_path)
    original = output.read_text()
    wrong = INSTALL_WRONG_FILES[name](original)
    assert wrong != original
    output.write_text(wrong)

    result = _run_install("--digest", DIGEST, "--verify", str(output))

    assert result.returncode != 0
    assert result.stderr.startswith("install compose error:")


@pytest.mark.parametrize(("change", "message"), [
    (lambda text: text.replace("never builds and never pulls", "never pulls"), "app image block"),
    (lambda text: text.replace("    pull_policy: never\n", ""), "app image block"),
    (lambda text: re.sub(r"(odograph):v[0-9.]+\n", r"\1@sha256:" + "c" * 64 + "\n", text),
     "app image block"),
    (lambda text: text.replace("  app:\n", "  web:\n").replace(
        "  osrm:\n", "  app:\n    image: example.test/odograph:v9\n\n  osrm:\n"
    ), "not under app"),
])
def test_install_render_refuses_an_unexpected_source(tmp_path, change, message):
    source = tmp_path / "source.yaml"
    original = (ROOT / "compose.yaml").read_text()
    changed = change(original)
    assert changed != original
    source.write_text(changed)

    result = _run_install("--digest", DIGEST, "--source", str(source), "--output", str(tmp_path / "o"))

    assert result.returncode != 0
    assert message in result.stderr
    assert not (tmp_path / "o").exists()
