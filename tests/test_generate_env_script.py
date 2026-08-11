from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _stage_generator(tmp_path: Path, example: str) -> Path:
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "generate_env.sh"
    script.write_bytes((REPO_ROOT / "scripts" / "generate_env.sh").read_bytes())
    (tmp_path / ".env.example").write_text(example)
    return script


def _isolated_tool_path(tmp_path: Path, backend: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("awk", "cat", "chmod", "dirname"):
        target = shutil.which(command)
        assert target is not None
        (bin_dir / command).symlink_to(target)

    if backend == "openssl":
        (bin_dir / "openssl").write_text(
            "#!/bin/sh\n"
            "case \"$2\" in\n"
            "  -hex) printf '%064d\\n' 0 ;;\n"
            "  -base64) printf '%043d\\n' 1 ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        (bin_dir / "openssl").chmod(0o755)
    else:
        (bin_dir / "python3").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *token_hex*) printf '%064d\\n' 2 ;;\n"
            "  *token_urlsafe*) printf '%043d\\n' 3 ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        (bin_dir / "python3").chmod(0o755)
    return bin_dir


def test_generate_env_uses_uri_safe_256_bit_postgres_password(tmp_path):
    script = _stage_generator(
        tmp_path,
        "POSTGRES_PASSWORD=\n"
        "INGEST_PASSWORD=\n"
        "SESSION_SECRET=\n"
        "INITIAL_ADMIN_SIGNUP=1\n"
        "DISPLAY_TZ=UTC\n"
        "FORWARDED_ALLOW_IPS=*\n",
    )

    subprocess.run(
        ["bash", str(script)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    values = dict(
        line.split("=", 1)
        for line in (tmp_path / ".env").read_text().splitlines()
        if "=" in line
    )
    assert re.fullmatch(r"[0-9a-f]{64}", values["POSTGRES_PASSWORD"])
    database_url = f"postgresql://mileage:{values['POSTGRES_PASSWORD']}@db:5432/mileage"
    assert urlsplit(database_url).password == values["POSTGRES_PASSWORD"]


def test_generate_env_refuses_to_overwrite_existing_file(tmp_path):
    script = _stage_generator(tmp_path, "POSTGRES_PASSWORD=\n")
    (tmp_path / ".env").write_text("keep=this\n")

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (tmp_path / ".env").read_text() == "keep=this\n"


def test_generate_env_generates_secrets_for_exactly_the_empty_env_example_vars(
    tmp_path,
):
    # Parsing the real .env.example (not a synthetic fixture) is the point:
    # the defect this guards against is .env.example and generate_env.sh
    # drifting apart, which a hand-written fixture can't expose because it
    # would just encode whichever set the test author already expects.
    env_example_text = (REPO_ROOT / ".env.example").read_text()
    generated_vars = re.findall(
        r"^([A-Za-z_][A-Za-z0-9_]*)=$", env_example_text, re.MULTILINE
    )
    assert generated_vars

    script = _stage_generator(tmp_path, env_example_text)

    result = subprocess.run(
        ["bash", str(script)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    generated_set = set(generated_vars)
    example_lines = env_example_text.splitlines()
    env_lines = (tmp_path / ".env").read_text().splitlines()
    assert len(env_lines) == len(example_lines)

    for example_line, env_line in zip(example_lines, env_lines):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=", example_line)
        if match and match.group(1) in generated_set:
            assert re.fullmatch(rf"{match.group(1)}=.+", env_line)
        else:
            assert env_line == example_line

    summary_block = result.stdout.split("for:\n", 1)[1].split("\n\n", 1)[0]
    assert summary_block.splitlines() == [f"  {var}" for var in generated_vars]


def test_generated_baseline_is_complete_private_and_contains_no_admin_token(tmp_path):
    script = _stage_generator(tmp_path, (REPO_ROOT / ".env.example").read_text())

    result = subprocess.run(
        ["bash", str(script)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    env_file = tmp_path / ".env"
    active = dict(
        line.split("=", 1)
        for line in env_file.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert set(active) == {
        "POSTGRES_PASSWORD",
        "INGEST_PASSWORD",
        "SESSION_SECRET",
        "INITIAL_ADMIN_SIGNUP",
        "DISPLAY_TZ",
        "FORWARDED_ALLOW_IPS",
    }
    assert all(active[name] for name in active)
    assert active["INITIAL_ADMIN_SIGNUP"] == "1"
    assert "ADMIN_TOKEN" not in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "Optional services remain disabled and unset" in result.stdout
    assert "docs/configuration.md" in result.stdout
    assert "left commented out" not in result.stdout
    for secret_name in ("POSTGRES_PASSWORD", "INGEST_PASSWORD", "SESSION_SECRET"):
        assert active[secret_name] not in result.stdout
        assert active[secret_name] not in result.stderr


@pytest.mark.parametrize("backend", ["openssl", "python"])
def test_generate_env_contract_covers_each_secret_backend(tmp_path, backend):
    script = _stage_generator(
        tmp_path,
        "POSTGRES_PASSWORD=\nINGEST_PASSWORD=\nSESSION_SECRET=\n",
    )
    bin_dir = _isolated_tool_path(tmp_path, backend)

    result = subprocess.run(
        ["/bin/bash", str(script)],
        check=True,
        cwd=tmp_path,
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
    )

    values = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines()
    )
    assert re.fullmatch(r"[0-9a-f]{64}", values["POSTGRES_PASSWORD"])
    assert values["INGEST_PASSWORD"]
    assert values["SESSION_SECRET"]
    assert values["POSTGRES_PASSWORD"] not in result.stdout
