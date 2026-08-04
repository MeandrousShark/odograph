from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_generate_env_uses_uri_safe_256_bit_postgres_password(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "generate_env.sh").write_bytes(
        (REPO_ROOT / "scripts" / "generate_env.sh").read_bytes()
    )
    (tmp_path / ".env.example").write_text(
        "POSTGRES_PASSWORD=\n"
        "INGEST_PASSWORD=\n"
        "SESSION_SECRET=\n"
        "ADMIN_TOKEN=\n"
        "DISPLAY_TZ=UTC\n"
        "FORWARDED_ALLOW_IPS=*\n"
    )

    subprocess.run(
        ["bash", str(tmp_path / "scripts" / "generate_env.sh")],
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
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "generate_env.sh").write_bytes(
        (REPO_ROOT / "scripts" / "generate_env.sh").read_bytes()
    )
    (tmp_path / ".env.example").write_text("POSTGRES_PASSWORD=\n")
    (tmp_path / ".env").write_text("keep=this\n")

    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "generate_env.sh")],
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

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "generate_env.sh").write_bytes(
        (REPO_ROOT / "scripts" / "generate_env.sh").read_bytes()
    )
    (tmp_path / ".env.example").write_text(env_example_text)

    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "generate_env.sh")],
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
