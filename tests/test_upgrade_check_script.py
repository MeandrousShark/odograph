from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
UPGRADE_SCRIPT = REPO_ROOT / "scripts" / "upgrade_check.sh"


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content + "\n")
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _compose_yaml(db_image: str, app_marker: str) -> str:
    return f"""services:
  db:
    image: {db_image}
    environment:
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
  app:
    build: .
    environment:
      APP_MARKER: {app_marker}
"""


def _install_repo(tmp_path: Path, *, candidate_db_image: str = "db:stable") -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    migrations_dir = repo / "migrations"
    scripts_dir.mkdir(parents=True)
    migrations_dir.mkdir()
    _write_executable(scripts_dir / "upgrade_check.sh", UPGRADE_SCRIPT.read_text())
    _write_executable(
        scripts_dir / "generate_env.sh",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'POSTGRES_PASSWORD=fake' 'ADMIN_TOKEN=fake' "
        "'INGEST_PASSWORD=fake' > .env",
    )
    _write_executable(scripts_dir / "send_test_track.sh", "#!/usr/bin/env bash\nexit 0")
    _write_executable(
        scripts_dir / "backup_database.sh",
        "#!/usr/bin/env bash\nexit 77",
    )
    _write_executable(scripts_dir / "restore_database.sh", "#!/usr/bin/env bash\nexit 78")
    (scripts_dir / "dev_seed.py").write_text("raise SystemExit(0)\n")
    (repo / "compose.yaml").write_text(_compose_yaml("db:stable", "base"))
    (migrations_dir / "001_base.sql").write_text("SELECT 1;\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Upgrade Test")
    _git(repo, "config", "user.email", "upgrade@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_ref = _git(repo, "rev-parse", "HEAD")

    (repo / "compose.yaml").write_text(_compose_yaml(candidate_db_image, "candidate"))
    _write_executable(
        scripts_dir / "backup_database.sh",
        "#!/usr/bin/env bash\nset -u\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then : > \"$2\"; exit 0; fi\n"
        "  shift\n"
        "done\n"
        "exit 1",
    )
    _write_executable(scripts_dir / "restore_database.sh", "#!/usr/bin/env bash\nexit 0")
    (repo / "compose.build.override.yml").write_text(
        "services:\n  app:\n    build: .\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    candidate_ref = _git(repo, "rev-parse", "HEAD")
    return repo, base_ref, candidate_ref


_COMPOSE_BODY = r"""
original="$*"
printf 'compose|%s|%s\n' "$PWD" "$original" >> "$FAKE_LOG"

while [ "${1-}" = "-f" ]; do
    [ "$#" -ge 2 ] || exit 90
    if [ "$2" = "compose.candidate-image.override.yml" ]; then
        cp "$2" "$FAKE_OVERRIDE_CAPTURE"
    fi
    shift 2
done

case "${1-}" in
    config)
        cat compose.yaml
        ;;
    up)
        case " $original " in
            *" app "|*" app")
                case "$PWD" in
                    */cand/*) printf '%s\n' candidate > "$FAKE_ACTIVE_APP" ;;
                    *) printf '%s\n' base > "$FAKE_ACTIVE_APP" ;;
                esac
                ;;
        esac
        exit "${FAKE_UP_EXIT:-42}"
        ;;
    down|stop)
        ;;
    exec)
        case "$original" in
            *pg_isready*) exit 0 ;;
            *"python -m app.manage_account reset-password"*)
                IFS= read -r password
                IFS= read -r confirmation
                [ "$password" = "$confirmation" ] || exit 94
                printf '%s' "$password" > "$FAKE_ROTATED_PASSWORD"
                printf 'auth|password-reset|candidate\n' >> "$FAKE_LOG"
                exit 0
                ;;
            *"INSERT INTO oidc_identities"*)
                : > "$FAKE_LINK_MARKER"
                printf 'auth|oidc-link|candidate\n' >> "$FAKE_LOG"
                exit 0
                ;;
            *"to_regclass('accounts') IS NOT NULL"*)
                case "$PWD" in */cand/*) printf '%s\n' t;; *) printf '%s\n' f;; esac
                exit 0
                ;;
            *"to_regclass('accounts') IS NULL"*)
                if [ "${FAKE_AUTH_FAULT:-}" = rollback-accounts-present ]; then
                    printf '%s\n' f
                else
                    case "$PWD" in */cand/*) printf '%s\n' f;; *) printf '%s\n' t;; esac
                fi
                exit 0
                ;;
            *"to_regclass('oidc_identities') IS NULL"*)
                case "$PWD" in */cand/*) printf '%s\n' f;; *) printf '%s\n' t;; esac
                exit 0
                ;;
            *"count(*) FROM accounts WHERE email"*) printf '%s\n' 1; exit 0 ;;
            *"count(*) FROM oidc_identities oi JOIN accounts"*)
                if [ -f "$FAKE_LINK_MARKER" ]; then printf '%s\n' 1; else printf '%s\n' 0; fi
                exit 0
                ;;
            *"count(*) FROM oidc_identities"*)
                if [ -f "$FAKE_LINK_MARKER" ]; then printf '%s\n' 1; else printf '%s\n' 0; fi
                exit 0
                ;;
            *"count(*) FROM local_admin WHERE id = 1"*) printf '%s\n' 1; exit 0 ;;
            *"sh -c"*) printf '%s\n' 1; exit 0 ;;
            *"count(*) FROM trips"*) printf '%s\n' 1; exit 0 ;;
            *"count(*) FROM points"*)
                if [ -f "$FAKE_INGEST_MARKER" ]; then printf '%s\n' 1; else printf '%s\n' 0; fi
                exit 0
                ;;
            *schema_migrations*) printf '%s\n' 1; exit 0 ;;
            *psql*) printf '%s\n' row; exit 0 ;;
        esac
        exit 93
        ;;
    *)
        printf 'unexpected compose invocation: %s\n' "$original" >&2
        exit 91
        ;;
esac
""".strip()


def _install_frontend(bin_dir: Path, frontend: str) -> str:
    if frontend == "docker":
        _write_executable(
            bin_dir / "docker",
            "#!/usr/bin/env bash\nset -u\n"
            "if [ \"${1-}\" = compose ]; then shift\n"
            + _COMPOSE_BODY
            + "\nexit $?\nfi\n"
            "printf 'runtime|docker|%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
            "case \"${1-}\" in info) exit 0;; volume|image) exit 0;; rmi) exit 0;; esac\n"
            "exit 92",
        )
        return "docker compose"

    _write_executable(bin_dir / "podman-compose", "#!/usr/bin/env bash\nset -u\n" + _COMPOSE_BODY)
    _write_executable(
        bin_dir / "podman",
        "#!/usr/bin/env bash\nset -u\n"
        "printf 'runtime|podman|%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
        "case \"${1-}\" in info) exit 0;; volume|image) exit 0;; rmi) exit 0;; esac\n"
        "exit 92",
    )
    return "podman-compose"


def _run(
    repo: Path,
    args: list[str],
    tmp_path: Path,
    *,
    frontend: str = "podman",
    fail_up: bool = True,
    auth_fault: str = "",
) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    scratch_root = tmp_path / "scratch"
    bin_dir.mkdir(exist_ok=True)
    scratch_root.mkdir(exist_ok=True)
    log_path = tmp_path / "fake.log"
    override_capture = tmp_path / "candidate-image.override.yml"
    compose_cmd = _install_frontend(bin_dir, frontend)
    _write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\nset -u\n"
        "args=\"$*\"\n"
        "url=\"${!#}\"\n"
        "printf 'http|%s\\n' \"$url\" >> \"$FAKE_LOG\"\n"
        "outfile=\"\"\n"
        "password=\"\"\n"
        "previous=\"\"\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$previous\" = -o ]; then outfile=\"$arg\"; fi\n"
        "  case \"$arg\" in password=*) password=\"${arg#password=}\";; esac\n"
        "  previous=\"$arg\"\n"
        "done\n"
        "if [ -n \"$outfile\" ] && [ \"$outfile\" != /dev/null ]; then\n"
        "  printf '%s\\n' '<input name=\"csrf_token\" value=\"fake-csrf\">' > \"$outfile\"\n"
        "  if [ \"$url\" = 'http://127.0.0.1:8077/login' ]; then\n"
        "    active=$(cat \"$FAKE_ACTIVE_APP\" 2>/dev/null || true)\n"
        "    if [ \"$active\" = base ] || { [ -f \"$FAKE_LINK_MARKER\" ] "
        "&& [ \"${FAKE_AUTH_FAULT:-}\" != linked-oidc-hidden ]; } "
        "|| [ \"${FAKE_AUTH_FAULT:-}\" = candidate-unlinked-oidc ]; then\n"
        "      printf '%s\\n' '<a href=\"/login/oidc\">OIDC</a>' >> \"$outfile\"\n"
        "    fi\n"
        "  fi\n"
        "fi\n"
        "status=200\n"
        "case \"$url\" in\n"
        "  */login/local)\n"
        "    active=$(cat \"$FAKE_ACTIVE_APP\")\n"
        "    original=$(cat \"$FAKE_ORIGINAL_PASSWORD\")\n"
        "    rotated=$(cat \"$FAKE_ROTATED_PASSWORD\" 2>/dev/null || true)\n"
        "    valid=$original\n"
        "    if [ \"$active\" = candidate ] && [ -n \"$rotated\" ]; then valid=$rotated; fi\n"
        "    if [ \"$password\" = \"$valid\" ]; then status=303; else status=401; fi\n"
        "    if [ \"${FAKE_AUTH_FAULT:-}\" = accept-old-after-rotation ] "
        "&& [ \"$active\" = candidate ] && [ \"$password\" = \"$original\" ]; then status=303; fi\n"
        "    if [ \"${FAKE_AUTH_FAULT:-}\" = accept-rotated-after-rollback ] "
        "&& [ \"$active\" = base ] && [ -n \"$rotated\" ] && [ \"$password\" = \"$rotated\" ]; then status=303; fi\n"
        "    ;;\n"
        "  */setup)\n"
        "    case \"$args\" in *--data-urlencode*) status=303; printf '%s' \"$password\" > \"$FAKE_ORIGINAL_PASSWORD\";; esac\n"
        "    ;;\n"
        "  */ingest) : > \"$FAKE_INGEST_MARKER\";;\n"
        "esac\n"
        "case \"$args\" in *'-w %'*|*\"-w %\"*) printf '%s' \"$status\";; esac",
    )
    _write_executable(
        bin_dir / "python3",
        "#!/usr/bin/env bash\nset -u\n"
        "if [ \"${1-}\" = - ]; then\n"
        "  program=$(mktemp)\n"
        "  trap 'rm -f \"$program\"' EXIT\n"
        "  cat > \"$program\"\n"
        "  shift\n"
        "  if grep -q 's.bind' \"$program\"; then printf '%s\\n' 55432; exit 0; fi\n"
        f"  {sys.executable!r} \"$program\" \"$@\"\n"
        "  status=$?\n"
        "  rm -f \"$program\"\n"
        "  trap - EXIT\n"
        "  exit \"$status\"\n"
        "fi\n"
        "if [ \"${1-}\" = -c ]; then\n"
        "  case \"${2-}\" in\n"
        "    *secrets.token_hex*) printf '%s\\n' \"$FAKE_OIDC_SECRET\"; exit 0;;\n"
        "    *secrets.token_urlsafe*)\n"
        "      if [ -f \"$FAKE_ORIGINAL_PASSWORD\" ]; then\n"
        "        printf '%s\\n' \"$FAKE_NEW_PASSWORD\"\n"
        "      else\n"
        "        printf '%s\\n' \"$FAKE_OLD_PASSWORD\"\n"
        "      fi\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        f"exec {sys.executable!r} \"$@\"",
    )
    oidc_secret = "b" * 64
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "COMPOSE_CMD": compose_cmd,
        "FAKE_LOG": str(log_path),
        "FAKE_OVERRIDE_CAPTURE": str(override_capture),
        "FAKE_INGEST_MARKER": str(tmp_path / "ingested"),
        "FAKE_ACTIVE_APP": str(tmp_path / "active-app"),
        "FAKE_ORIGINAL_PASSWORD": str(tmp_path / "original-password"),
        "FAKE_ROTATED_PASSWORD": str(tmp_path / "rotated-password"),
        "FAKE_LINK_MARKER": str(tmp_path / "linked-identity"),
        "FAKE_OLD_PASSWORD": "original-password-contract",
        "FAKE_NEW_PASSWORD": "rotated-password-contract",
        "FAKE_OIDC_SECRET": oidc_secret,
        "FAKE_AUTH_FAULT": auth_fault,
        "FAKE_UP_EXIT": "42" if fail_up else "0",
        "TMPDIR": str(scratch_root),
    }
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "upgrade_check.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, log_path, override_capture, scratch_root


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--base", "HEAD"], "--base and --candidate are both required"),
        (["--candidate", "HEAD"], "--base and --candidate are both required"),
        (
            ["--base", "HEAD", "--candidate-image", "image:v1"],
            "--base and --candidate are both required",
        ),
    ],
)
def test_argument_contract_requires_both_source_refs(
    tmp_path, args, message
):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = _write_executable(scripts_dir / "upgrade_check.sh", UPGRADE_SCRIPT.read_text())

    result = subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, timeout=10
    )

    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize(
    "image",
    [
        "registry.example.test/odograph",
        "registry.example.test/odograph:latest",
        "registry.example.test/odograph:LATEST",
        "registry.example.test:5000/odograph",
        "registry.example.test/odograph@sha256:1234abcd",
    ],
)
def test_candidate_image_rejects_mutable_or_incomplete_references(tmp_path, image):
    script = _write_executable(tmp_path / "upgrade_check.sh", UPGRADE_SCRIPT.read_text())

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--base",
            "base",
            "--candidate",
            "candidate",
            "--candidate-image",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "explicit non-latest tag or a full sha256 digest" in result.stderr


def test_help_documents_candidate_tree_and_stable_db_constraint(tmp_path):
    script = _write_executable(tmp_path / "upgrade_check.sh", UPGRADE_SCRIPT.read_text())

    result = subprocess.run(
        ["bash", str(script), "--help"], capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0
    assert "--candidate-image IMAGE" in result.stderr
    assert "candidate tree supplies Compose and operational scripts" in result.stderr
    assert "explicit non-latest" in result.stderr
    assert "rendered db" in result.stderr
    assert "service must be identical" in result.stderr


@pytest.mark.parametrize("frontend", ["docker", "podman"])
def test_candidate_image_uses_app_only_override_without_build_and_tears_down(
    tmp_path, frontend
):
    repo, base_ref, candidate_ref = _install_repo(tmp_path)
    image = "registry.example.test/odograph@sha256:" + "a" * 64

    result, log_path, override_capture, scratch_root = _run(
        repo,
        [
            "--base",
            base_ref,
            "--candidate",
            candidate_ref,
            "--candidate-image",
            image,
        ],
        tmp_path,
        frontend=frontend,
        fail_up=False,
    )

    assert result.returncode == 0, result.stderr
    assert override_capture.read_text() == (
        "services:\n"
        "  app:\n"
        f"    image: '{image}'\n"
        "    pull_policy: always\n"
    )
    assert "db:" not in override_capture.read_text()

    lines = log_path.read_text().splitlines()
    config_lines = [line for line in lines if line.startswith("compose|") and line.endswith(" config")]
    assert len(config_lines) == 2
    assert "compose.candidate-image.override.yml" not in config_lines[0]
    assert "compose.candidate-image.override.yml" in config_lines[1]
    up_lines = [line for line in lines if " up " in f" {line} "]
    candidate_up = [line for line in up_lines if "compose.candidate-image.override.yml" in line]
    assert len(candidate_up) == 1
    assert candidate_up[0].endswith(" up -d --no-build app")
    assert "--build" not in candidate_up[0]
    assert any(line.endswith(" down") for line in lines)
    assert "auth|oidc-link|candidate" in lines
    assert "auth|password-reset|candidate" in lines
    assert any(
        "python -m app.manage_account reset-password" in line for line in lines
    )
    assert any("INSERT INTO oidc_identities" in line for line in lines)
    assert not any(line == "http|http://127.0.0.1:8077/login/oidc" for line in lines)
    assert "original local password works" in result.stdout
    assert "candidate schema and offered for sign-in" in result.stdout
    assert "the new password works and the original fails" in result.stdout
    assert "rotated password and candidate-only auth state absent" in result.stdout
    combined_output = result.stdout + result.stderr + log_path.read_text()
    for secret in (
        "original-password-contract",
        "rotated-password-contract",
        "b" * 64,
    ):
        assert secret not in combined_output
    assert list(scratch_root.glob("upgrade_check.*")) == []


def test_candidate_image_accepts_explicit_non_latest_tag(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path)

    result, _, override_capture, _ = _run(
        repo,
        [
            "--base",
            base_ref,
            "--candidate",
            candidate_ref,
            "--candidate-image",
            "registry.example.test:5000/odograph:v0.6.0-rc.1",
        ],
        tmp_path,
    )

    assert result.returncode == 42
    assert "v0.6.0-rc.1" in override_capture.read_text()


def test_source_candidate_allows_app_drift_and_uses_build_override(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path)

    result, log_path, _, scratch_root = _run(
        repo, ["--base", base_ref, "--candidate", candidate_ref], tmp_path
    )

    assert result.returncode == 42
    assert "db service definitions differ" not in result.stderr
    config_lines = [
        line
        for line in log_path.read_text().splitlines()
        if line.startswith("compose|") and line.endswith(" config")
    ]
    assert len(config_lines) == 2
    assert "compose.build.override.yml" not in config_lines[0]
    assert "compose.build.override.yml" in config_lines[1]
    assert list(scratch_root.glob("upgrade_check.*")) == []


def _extract_function(source: str, name: str) -> str:
    marker = f"{name}() {{"
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    end = brace_start
    for end, char in enumerate(source[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
    return source[start : end + 1]


def _manifest_comparison_harness(tmp_path: Path) -> Path:
    # step7_upgrade's manifest comparison is the one under test here (the
    # bug: it compared schema_version, which necessarily changes across a
    # migration-running upgrade). Standing up the full drill's containers
    # just to exercise this comparison would be impractical, so the exact
    # functions it relies on are pulled out of the real script and driven
    # directly against fixture manifests instead.
    source = UPGRADE_SCRIPT.read_text()
    functions = "\n\n".join(
        _extract_function(source, name)
        for name in (
            "step_pass",
            "step_fail",
            "assert_manifests_equal",
            "strip_schema_version_section",
            "assert_data_manifests_equal",
        )
    )
    script = tmp_path / "compare.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'SCRATCH="$1"\n'
        'A="$2"\n'
        'B="$3"\n'
        'DESC="$4"\n\n'
        f"{functions}\n\n"
        'assert_data_manifests_equal "$A" "$B" "$DESC"\n'
    )
    script.chmod(0o755)
    return script


def test_data_manifest_comparison_ignores_schema_version_drift(tmp_path):
    script = _manifest_comparison_harness(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    common_data = "== administrator ==\nadmin@example.test\n== vehicles ==\ncar\n"
    manifest_a = tmp_path / "manifest-a.txt"
    manifest_b = tmp_path / "manifest-b.txt"
    manifest_a.write_text("== schema_version ==\n18\n" + common_data)
    manifest_b.write_text("== schema_version ==\n19\n" + common_data)

    result = subprocess.run(
        ["bash", str(script), str(scratch), str(manifest_a), str(manifest_b), "schema-only-diff"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "[PASS] schema-only-diff" in result.stdout


def test_data_manifest_comparison_still_fails_on_data_drift(tmp_path):
    script = _manifest_comparison_harness(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    manifest_a = tmp_path / "manifest-a.txt"
    manifest_b = tmp_path / "manifest-b.txt"
    manifest_a.write_text("== schema_version ==\n18\n== vehicles ==\ncar\n")
    manifest_b.write_text("== schema_version ==\n19\n== vehicles ==\ntruck\n")

    result = subprocess.run(
        ["bash", str(script), str(scratch), str(manifest_a), str(manifest_b), "data-diff"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "manifest mismatch (data-diff)" in result.stderr
    assert "-car" in result.stderr
    assert "+truck" in result.stderr


def test_keep_teardown_message_reports_project_patterns_without_unbound_variable(
    tmp_path,
):
    repo, base_ref, candidate_ref = _install_repo(tmp_path)

    result, _, _, scratch_root = _run(
        repo,
        ["--base", base_ref, "--candidate", candidate_ref, "--keep"],
        tmp_path,
    )

    assert "unbound variable" not in result.stderr
    assert "--keep given: leaving containers/volumes/scratch in place" in result.stdout

    match = re.search(r"^  project:\s+(\S+)$", result.stdout, re.MULTILINE)
    assert match, result.stdout
    project = match.group(1)
    assert f"'{project}-*'/'{project}_*' (docker)," in result.stdout
    assert list(scratch_root.glob("upgrade_check.*")) != []


def test_source_candidate_refuses_db_service_drift_before_up(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(
        tmp_path, candidate_db_image="db:changed"
    )

    result, log_path, _, scratch_root = _run(
        repo, ["--base", base_ref, "--candidate", candidate_ref], tmp_path
    )

    assert result.returncode != 0
    assert "rendered db service definitions differ" in result.stderr
    assert "release-specific operations migration plan" in result.stderr
    assert not any(" up " in f" {line} " for line in log_path.read_text().splitlines())
    assert any(line.endswith(" down") for line in log_path.read_text().splitlines())
    assert list(scratch_root.glob("upgrade_check.*")) == []


@pytest.mark.parametrize(
    ("auth_fault", "message"),
    [
        (
            "candidate-unlinked-oidc",
            "configured but unlinked OIDC was incorrectly offered",
        ),
        (
            "accept-old-after-rotation",
            "original local password still signed in after rotation",
        ),
        (
            "linked-oidc-hidden",
            "stored OIDC identity was not offered as a candidate sign-in option",
        ),
        (
            "accept-rotated-after-rollback",
            "candidate-rotated password incorrectly worked after rollback",
        ),
        (
            "rollback-accounts-present",
            "candidate-only accounts relation survived rollback",
        ),
    ],
)
def test_mixed_auth_drill_fails_closed_on_transition_drift(
    tmp_path, auth_fault, message
):
    repo, base_ref, candidate_ref = _install_repo(tmp_path)

    result, log_path, _, scratch_root = _run(
        repo,
        ["--base", base_ref, "--candidate", candidate_ref],
        tmp_path,
        fail_up=False,
        auth_fault=auth_fault,
    )

    assert result.returncode != 0
    assert message in result.stderr
    combined_output = result.stdout + result.stderr + log_path.read_text()
    assert "original-password-contract" not in combined_output
    assert "rotated-password-contract" not in combined_output
    assert "b" * 64 not in combined_output
    assert list(scratch_root.glob("upgrade_check.*")) == []
