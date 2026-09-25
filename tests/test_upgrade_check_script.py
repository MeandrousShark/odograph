from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

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


def _install_repo(
    tmp_path: Path,
    *,
    candidate_db_image: str = "db:stable",
    modern_auth: bool = False,
    no_bootstrap: bool = False,
    owned_schema: bool = False,
) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    migrations_dir = repo / "migrations"
    scripts_dir.mkdir(parents=True)
    migrations_dir.mkdir()
    source = UPGRADE_SCRIPT.read_text()
    # Fake Compose/curl never open a server. A real development site or
    # independent upgrade drill must not change these orchestration tests.
    source = source.replace(_extract_function(source, "port_in_use"), "port_in_use() { return 1; }")
    _write_executable(scripts_dir / "upgrade_check.sh", source)
    if no_bootstrap:
        bootstrap_env = "'POSTGRES_PASSWORD=fake' 'INGEST_PASSWORD=fake'"
    elif owned_schema:
        bootstrap_env = "'POSTGRES_PASSWORD=fake' 'INITIAL_ADMIN_SIGNUP=1'"
    elif modern_auth:
        bootstrap_env = "'POSTGRES_PASSWORD=fake' 'INGEST_PASSWORD=fake' 'INITIAL_ADMIN_SIGNUP=1'"
    else:
        bootstrap_env = "'POSTGRES_PASSWORD=fake' 'ADMIN_TOKEN=fake' 'INGEST_PASSWORD=fake'"
    _write_executable(
        scripts_dir / "generate_env.sh",
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' {bootstrap_env} > .env",
    )
    _write_executable(
        scripts_dir / "send_test_track.sh",
        "#!/usr/bin/env bash\n"
        "if [ \"${FAKE_SCHEMA_VERSION:-1}\" -ge 26 ]; then\n"
        "  [ \"${ODOGRAPH_TRACKING_USERNAME:-}\" = odograph_test ] || exit 97\n"
        "  [ \"${ODOGRAPH_TRACKING_SECRET:-}\" = synthetic-track-secret ] || exit 97\n"
        "  echo tracking-issued-login >> \"$FAKE_LOG\"\n"
        "fi\nexit 0",
    )
    _write_executable(
        scripts_dir / "backup_database.sh",
        "#!/usr/bin/env bash\nexit 77",
    )
    _write_executable(scripts_dir / "restore_database.sh", "#!/usr/bin/env bash\nexit 78")
    (scripts_dir / "dev_seed.py").write_text("raise SystemExit(0)\n")
    (repo / "compose.yaml").write_text(_compose_yaml("db:stable", "base"))
    (migrations_dir / "001_base.sql").write_text("SELECT 1;\n")
    if owned_schema:
        for version in range(2, 27):
            (migrations_dir / f"{version:03d}_fixture.sql").write_text("SELECT 1;\n")
        (scripts_dir / "dev_seed.py").write_text(
            "import os, pathlib\n"
            "assert pathlib.Path(os.environ['FAKE_SIGNUP_EMAIL']).read_text() == 'development@localhost.invalid'\n"
        )

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
        "printf 'child|backup|%s|%s|%s\\n' \"$PWD\" \"${COMPOSE_FILE:-}\" \"${COMPOSE_CMD:-}\" >> \"$FAKE_LOG\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --output ]; then : > \"$2\"; exit 0; fi\n"
        "  shift\n"
        "done\n"
        "exit 1",
    )
    _write_executable(
        scripts_dir / "restore_database.sh",
        "#!/usr/bin/env bash\n"
        "printf 'child|restore|%s|%s|%s\\n' \"$PWD\" \"${COMPOSE_FILE:-}\" \"${COMPOSE_CMD:-}\" >> \"$FAKE_LOG\"\n"
        "exit 0",
    )
    (repo / "compose.build.override.yml").write_text(
        "services:\n  app:\n    build: .\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    candidate_ref = _git(repo, "rev-parse", "HEAD")
    return repo, base_ref, candidate_ref


def _install_repo_with_db_service_drift(tmp_path: Path) -> tuple[Path, str, str]:
    repo, base_ref, candidate_ref = _install_repo(
        tmp_path, candidate_db_image="db:native"
    )
    compose = repo / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n",
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n"
            "      POSTGRES_USER: unexpected\n",
        )
    )
    _git(repo, "add", "compose.yaml")
    _git(repo, "commit", "-qm", "candidate db service drift")
    return repo, base_ref, _git(repo, "rev-parse", "HEAD")


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
        printf '%s\n' "${PWD##*/}" > "$FAKE_PROJECT_FILE"
        case " $original " in
            *" db "*)
                if [ -n "${FAKE_DB_EVENTS:-}" ]; then
                    case "$PWD" in
                        */cand/*)
                            printf 'candidate\n' >> "$FAKE_DB_EVENTS"
                            [ -z "${FAKE_ACTIVE_DB:-}" ] || printf 'candidate\n' > "$FAKE_ACTIVE_DB"
                            ;;
                        *)
                            printf 'base\n' >> "$FAKE_DB_EVENTS"
                            [ -z "${FAKE_ACTIVE_DB:-}" ] || printf 'base\n' > "$FAKE_ACTIVE_DB"
                            ;;
                    esac
                fi
                ;;
        esac
        case " $original " in
            *" app "|*" app")
                case "$PWD" in
                    */cand/*) printf '%s\n' candidate > "$FAKE_ACTIVE_APP" ;;
                    *) printf '%s\n' base > "$FAKE_ACTIVE_APP" ;;
                esac
                ;;
        esac
        case "$PWD" in
            */cand/*)
                case " $original " in
                    *" db "*)
                        if [ -n "${FAKE_DB_UP_EXIT:-}" ]; then
                            exit "$FAKE_DB_UP_EXIT"
                        fi
                        ;;
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
            *"exec -T app python -")
                cat > "$FAKE_ISSUANCE_SCRIPT"
                [ "${FAKE_AUTH_FAULT:-}" != issuance-failed ] || exit 96
                printf '%s\n' '[["odograph_test", "synthetic-track-secret"], ["odograph_restore", "synthetic-restore-secret"]]'
                printf 'tracking|issued\n' >> "$FAKE_LOG"
                exit 0
                ;;
            *"python -m app.application_roles verify"*) exit 0 ;;
            *"python -m app.manage_account list-accounts"*)
                printf 'ID\tLOGIN EMAIL\tROLE\tSTATUS\tEMAIL\n'
                printf '3\tother@example.invalid\tmember\tenabled\tverified\n'
                printf '7\tdevelopment@localhost.invalid\tadmin\tenabled\tunverified\n'
                exit 0
                ;;
            *"python -m app.manage_account reset-password 7")
                IFS= read -r confirmation
                [ "$confirmation" = yes ] || exit 93
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
                if [ "${FAKE_MODERN_AUTH:-}" = 1 ]; then
                    printf '%s\n' t
                else
                    case "$PWD" in */cand/*) printf '%s\n' t;; *) printf '%s\n' f;; esac
                fi
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
                if [ "${FAKE_MODERN_AUTH:-}" = 1 ]; then
                    printf '%s\n' f
                else
                    case "$PWD" in */cand/*) printf '%s\n' f;; *) printf '%s\n' t;; esac
                fi
                exit 0
                ;;
            *"to_regclass('oidc_identities') IS NOT NULL"*)
                if [ "${FAKE_MODERN_AUTH:-}" = 1 ]; then
                    printf '%s\n' t
                else
                    printf '%s\n' f
                fi
                exit 0
                ;;
            *"to_regclass('local_admin') IS NULL"*)
                if [ "${FAKE_MODERN_AUTH:-}" = 1 ]; then
                    printf '%s\n' t
                else
                    printf '%s\n' f
                fi
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
            *"count(*) FROM ingest_credentials"*) printf '%s\n' 1; exit 0 ;;
            *"find migrations"*) printf '%s\n' "${FAKE_SCHEMA_VERSION:-1}"; exit 0 ;;
            *"sh -c"*) printf '%s\n' 1; exit 0 ;;
            *"count(*) FROM trips"*) printf '%s\n' 1; exit 0 ;;
            *"count(*) FROM points"*)
                if [ -f "$FAKE_INGEST_MARKER" ]; then cat "$FAKE_INGEST_MARKER"; else printf '%s\n' 0; fi
                exit 0
                ;;
            *schema_migrations*) printf '%s\n' "${FAKE_SCHEMA_VERSION:-1}"; exit 0 ;;
            *psql*) printf '%s\n' row; exit 0 ;;
        esac
        exit 93
        ;;
    ps)
        [ "$#" -eq 2 ] && [ "$2" = -q ] || exit 98
        case "$PWD" in
            */cand/*) printf '%s\n' candidate-db-container ;;
            *) printf '%s\n' base-db-container ;;
        esac
        exit 0
        ;;
    *)
        printf 'unexpected compose invocation: %s\n' "$original" >&2
        exit 91
        ;;
esac
""".strip()


_RUNTIME_PREFLIGHT = r"""
if [ "${1-}" = ps ]; then
    case "$*" in *label=com.docker.compose.service=db*) ;; *) exit 98;; esac
    printf '%s\n' db-container
    exit 0
fi
if [ "${1-}" = volume ] && [ "${FAKE_VOLUME_FAILURE:-}" = 1 ] && [ -f "$FAKE_DB_EVENTS" ]; then
    if [ "$(wc -l < "$FAKE_DB_EVENTS")" -ge 2 ]; then
        case "${2-}" in
            ls) printf '%s_dbdata\n' "$(cat "$FAKE_PROJECT_FILE")"; exit 0;;
            rm) exit 73;;
        esac
    fi
fi
"""


def _install_frontend(bin_dir: Path, frontend: str) -> str:
    if frontend == "docker":
        _write_executable(
            bin_dir / "docker",
            "#!/usr/bin/env bash\nset -u\n"
            "if [ \"${1-}\" = compose ]; then shift\n"
            + _COMPOSE_BODY
            + "\nexit $?\nfi\n"
            "printf 'runtime|docker|%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
            + _RUNTIME_PREFLIGHT
            + "case \"${1-}\" in info) exit 0;; inspect) if [ \"$(cat \"${FAKE_ACTIVE_DB:-/dev/null}\" 2>/dev/null || true)\" = candidate ]; then printf '%s\\n' candidate-image-id >> \"${FAKE_INSPECT_EVENTS:-/dev/null}\"; printf '%s\\n' candidate-image-id; else printf '%s\\n' base-image-id >> \"${FAKE_INSPECT_EVENTS:-/dev/null}\"; printf '%s\\n' base-image-id; fi; exit 0;; volume|image) exit 0;; rmi) exit 0;; esac\n"
            "exit 92",
        )
        return "docker compose"

    _write_executable(bin_dir / "podman-compose", "#!/usr/bin/env bash\nset -u\n" + _COMPOSE_BODY)
    _write_executable(
        bin_dir / "podman",
        "#!/usr/bin/env bash\nset -u\n"
        "printf 'runtime|podman|%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
        + _RUNTIME_PREFLIGHT
        + "case \"${1-}\" in info) exit 0;; inspect) if [ \"$(cat \"${FAKE_ACTIVE_DB:-/dev/null}\" 2>/dev/null || true)\" = candidate ]; then printf '%s\\n' candidate-image-id >> \"${FAKE_INSPECT_EVENTS:-/dev/null}\"; printf '%s\\n' candidate-image-id; else printf '%s\\n' base-image-id >> \"${FAKE_INSPECT_EVENTS:-/dev/null}\"; printf '%s\\n' base-image-id; fi; exit 0;; volume|image) exit 0;; rmi) exit 0;; esac\n"
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
    db_up_failure: str = "",
    volume_failure: bool = False,
    auth_fault: str = "",
    modern_auth: bool = False,
    owned_schema: bool = False,
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
        "headerfile=\"\"\n"
        "password=\"\"\n"
        "email=\"\"\n"
        "basic=\"\"\n"
        "previous=\"\"\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$previous\" = -o ]; then outfile=\"$arg\"; fi\n"
        "  if [ \"$previous\" = -D ]; then headerfile=\"$arg\"; fi\n"
        "  if [ \"$previous\" = -u ]; then basic=\"$arg\"; fi\n"
        "  case \"$arg\" in password=*) password=\"${arg#password=}\";; esac\n"
        "  case \"$arg\" in email=*) email=\"${arg#email=}\";; esac\n"
        "  previous=\"$arg\"\n"
        "done\n"
        "if [ -n \"$outfile\" ] && [ \"$outfile\" != /dev/null ]; then\n"
        "  printf '%s\\n' '<input name=\"csrf_token\" value=\"fake-csrf\">' > \"$outfile\"\n"
        "  if [ \"$url\" = 'http://127.0.0.1:8077/login' ]; then\n"
        "    active=$(cat \"$FAKE_ACTIVE_APP\" 2>/dev/null || true)\n"
        "    if { [ \"$active\" = base ] && [ \"${FAKE_MODERN_AUTH:-}\" != 1 ]; } || { [ \"$active\" = candidate ] && [ -f \"$FAKE_LINK_MARKER\" ] "
        "&& [ \"${FAKE_AUTH_FAULT:-}\" != linked-oidc-hidden ]; } "
        "|| [ \"${FAKE_AUTH_FAULT:-}\" = candidate-unlinked-oidc ]; then\n"
        "      printf '%s\\n' '<a href=\"/login/oidc\">OIDC</a>' >> \"$outfile\"\n"
        "    fi\n"
        "  fi\n"
        "fi\n"
        "status=200\n"
        "case \"$url\" in\n"
        "  */signup)\n"
        "    case \"$args\" in *--data-urlencode*) status=303; printf '%s' \"$password\" > \"$FAKE_ORIGINAL_PASSWORD\"; printf '%s' \"$email\" > \"$FAKE_SIGNUP_EMAIL\";; esac\n"
        "    if [ -n \"$headerfile\" ]; then printf '%s\\n' 'HTTP/1.1 303 See Other' 'Location: /' > \"$headerfile\"; fi\n"
        "    ;;\n"
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
        "  */ingest)\n"
        "    if [ \"${FAKE_SCHEMA_VERSION:-1}\" -ge 26 ]; then\n"
        "      [ \"$basic\" = odograph_restore:synthetic-restore-secret ] || exit 97\n"
        "      echo ingest-issued-login >> \"$FAKE_LOG\"\n"
        "    fi\n"
        "    count=$(cat \"$FAKE_INGEST_MARKER\" 2>/dev/null || echo 0)\n"
        "    echo $((count + 1)) > \"$FAKE_INGEST_MARKER\"\n"
        "    ;;\n"
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
        "FAKE_MODERN_AUTH": "1" if modern_auth else "",
        "FAKE_SCHEMA_VERSION": "26" if owned_schema else "1",
        "FAKE_SIGNUP_EMAIL": str(tmp_path / "signup-email"),
        "FAKE_ISSUANCE_SCRIPT": str(tmp_path / "issuance.py"),
        "FAKE_DB_EVENTS": str(tmp_path / "db-events"),
        "FAKE_ACTIVE_DB": str(tmp_path / "active-db"),
        "FAKE_INSPECT_EVENTS": str(tmp_path / "inspect-events"),
        "FAKE_DB_UP_EXIT": db_up_failure,
        "FAKE_PROJECT_FILE": str(tmp_path / "project-name"),
        "FAKE_VOLUME_FAILURE": "1" if volume_failure else "",
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
    assert "http|http://127.0.0.1:8077/setup" in lines
    assert "http|http://127.0.0.1:8077/signup" not in lines
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


def test_modern_auth_uses_signup_and_modern_rollback_contract(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path, modern_auth=True)

    result, log_path, _, scratch_root = _run(
        repo,
        ["--base", base_ref, "--candidate", candidate_ref],
        tmp_path,
        fail_up=False,
        modern_auth=True,
    )

    assert result.returncode == 0, result.stderr
    assert "capability-gated /signup flow" in result.stdout
    assert "modern accounts schema" in result.stdout
    http_lines = log_path.read_text().splitlines()
    assert "http|http://127.0.0.1:8077/signup" in http_lines
    assert not any(line.endswith("/setup") for line in http_lines)
    log_text = log_path.read_text()
    assert "to_regclass('accounts') IS NOT NULL" in log_text
    assert "to_regclass('oidc_identities') IS NOT NULL" in log_text
    assert "to_regclass('local_admin') IS NULL" in log_text
    assert "count(*) FROM local_admin" not in log_text
    assert list(scratch_root.glob("upgrade_check.*")) == []


def test_missing_bootstrap_capability_fails_before_http_bootstrap(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path, no_bootstrap=True)

    result, log_path, _, scratch_root = _run(
        repo,
        ["--base", base_ref, "--candidate", candidate_ref],
        tmp_path,
        fail_up=False,
    )

    assert result.returncode != 0
    assert "no scriptable administrator bootstrap" in result.stderr
    http_lines = log_path.read_text().splitlines()
    assert not any(line.endswith("/setup") for line in http_lines)
    assert not any(line.endswith("/signup") for line in http_lines)
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


def test_env_value_reports_missing_key_and_file(tmp_path):
    source = UPGRADE_SCRIPT.read_text()
    helper = tmp_path / "env-value.sh"
    helper.write_text(
        "#!/usr/bin/env bash\nset -o pipefail\n"
        f"{_extract_function(source, 'env_value')}\n"
        'env_value "$1" MISSING_KEY\n'
    )
    helper.chmod(0o755)
    env_file = tmp_path / "base.env"
    env_file.write_text("POSTGRES_PASSWORD=present\n")

    result = subprocess.run(
        [str(helper), str(env_file)], capture_output=True, text=True, timeout=10
    )

    assert result.returncode != 0
    assert "MISSING_KEY" in result.stderr
    assert str(env_file) in result.stderr


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


@pytest.mark.parametrize(
    "version,override,success,ingested",
    [
        (25, {}, True, False),
        (26, {}, True, True),
        (26, {"VERIFY_EXIT": "7"}, False, False),
        (26, {"CREDENTIAL_COUNT": "0"}, False, False),
        (26, {"INGEST_EXIT": "7"}, False, True),
        (26, {"SKIP_POINT": "1"}, False, True),
    ],
)
def test_ownership_upgrade_checks_contract_and_existing_tracker(
    tmp_path, version, override, success, ingested,
):
    source = UPGRADE_SCRIPT.read_text()
    functions = "\n\n".join(
        _extract_function(source, name)
        for name in ("step_pass", "step_fail", "verify_ownership_upgrade")
    )
    log = tmp_path / "checks.log"
    count = tmp_path / "points"
    count.write_text("1\n")
    script = tmp_path / "ownership-check.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'ADMIN_EMAIL=development@localhost.invalid\n'
        'BASE_SCHEMA_VERSION=25\n'
        'INGEST_USERNAME=owntracks\n'
        'POSTRESTORE_DEVICE=existing-phone\n'
        'INGEST_PASSWORD=synthetic\n'
        'compose_dir() { printf "%s\\n" "$*" >> "$CHECK_LOG"; return "${VERIFY_EXIT:-0}"; }\n'
        'db_query() { printf "%s\\n" "${CREDENTIAL_COUNT:-1}"; }\n'
        'count_points() { cat "$POINTS"; }\n'
        'ingest_one_point() {\n'
        '  echo ingest >> "$CHECK_LOG"\n'
        '  if [ "${SKIP_POINT:-0}" != 1 ]; then echo 2 > "$POINTS"; fi\n'
        '  return "${INGEST_EXIT:-0}"\n'
        '}\n'
        f'{functions}\nverify_ownership_upgrade candidate "$1"\n'
    )
    script.chmod(0o755)
    result = subprocess.run(
        [str(script), str(version)], capture_output=True, text=True, timeout=10,
        env={**os.environ, "CHECK_LOG": str(log), "POINTS": str(count), **override},
    )

    assert (result.returncode == 0) is success, result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert ("ingest" in calls) is ingested
    if version < 26:
        assert calls == []
    else:
        assert calls[0] == "candidate exec -T app python -m app.application_roles verify"


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
    assert f"'{project}-app:*'/'{project}_app:*' (docker)," in result.stdout
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


def test_database_image_migration_rejects_non_image_db_service_drift(tmp_path):
    repo, base_ref, candidate_ref = _install_repo_with_db_service_drift(tmp_path)

    result, log_path, _, scratch_root = _run(
        repo,
        [
            "--base",
            base_ref,
            "--candidate",
            candidate_ref,
            "--database-image-migration",
        ],
        tmp_path,
        fail_up=False,
    )

    assert result.returncode != 0
    assert "database-image-migration" in result.stderr
    assert "db" in result.stderr.lower()
    assert "image" in result.stderr.lower()
    log_text = log_path.read_text() if log_path.exists() else ""
    assert not any(" up " in f" {line} " for line in log_text.splitlines())
    assert list(scratch_root.glob("upgrade_check.*")) == []


@pytest.mark.parametrize("frontend", ["docker", "podman"])
def test_database_image_migration_rebuilds_db_and_rolls_back_to_original_image(
    tmp_path, frontend,
):
    repo, base_ref, candidate_ref = _install_repo(
        tmp_path, candidate_db_image="db:native"
    )

    result, log_path, _, scratch_root = _run(
        repo,
        [
            "--base",
            base_ref,
            "--candidate",
            candidate_ref,
            "--database-image-migration",
        ],
        tmp_path,
        fail_up=False,
        frontend=frontend,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "db-events").read_text().splitlines() == [
        "base",
        "base",
        "candidate",
        "base",
    ]
    inspect_events = (tmp_path / "inspect-events").read_text().splitlines()
    assert "base-image-id" in inspect_events
    assert "candidate-image-id" in inspect_events
    assert inspect_events.index("base-image-id") < inspect_events.index("candidate-image-id")
    assert inspect_events.index("candidate-image-id") < len(inspect_events) - 1
    assert inspect_events[-1] == "base-image-id"

    compose_lines = log_path.read_text().splitlines()
    candidate_db_up = [
        index
        for index, line in enumerate(compose_lines)
        if "/cand/" in line and " up " in f" {line} " and " db" in line
    ]
    assert len(candidate_db_up) == 1
    assert any(
        index < candidate_db_up[0]
        and "/base/" in line
        and " down" in f" {line} "
        for index, line in enumerate(compose_lines)
    )
    assert any(
        index > candidate_db_up[0]
        and "/base/" in line
        and " up " in f" {line} "
        and " db" in line
        for index, line in enumerate(compose_lines)
    )
    assert "step 7" in result.stdout
    assert "step 8" in result.stdout
    assert list(scratch_root.glob("upgrade_check.*")) == []


def test_database_migration_rejects_unchanged_image_before_startup(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path)
    result, log, _, _ = _run(
        repo, ["--base", base_ref, "--candidate", candidate_ref, "--database-image-migration"],
        tmp_path, fail_up=False,
    )
    assert result.returncode != 0
    assert "requires a changed db image" in result.stderr
    assert " up " not in log.read_text()


def test_database_migration_stops_if_old_volume_cannot_be_removed(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path, candidate_db_image="db:native")
    result, log, _, _ = _run(
        repo, ["--base", base_ref, "--candidate", candidate_ref, "--database-image-migration"],
        tmp_path, fail_up=False, volume_failure=True,
    )
    assert result.returncode != 0
    assert "refusing to reuse" in result.stderr
    assert not any("/cand/" in line and " up " in line for line in log.read_text().splitlines())


def test_database_image_migration_fails_closed_when_candidate_db_cannot_start(
    tmp_path,
):
    repo, base_ref, candidate_ref = _install_repo(
        tmp_path, candidate_db_image="db:native"
    )

    result, log_path, _, scratch_root = _run(
        repo,
        [
            "--base",
            base_ref,
            "--candidate",
            candidate_ref,
            "--database-image-migration",
        ],
        tmp_path,
        fail_up=False,
        db_up_failure="73",
    )

    assert result.returncode != 0
    assert "step 7" in result.stderr
    assert "candidate" in result.stderr.lower()
    assert (tmp_path / "db-events").read_text().splitlines() == [
        "base",
        "base",
        "candidate",
    ]
    assert not any(
        "/cand/" in line and " up " in f" {line} " and " app" in line
        for line in log_path.read_text().splitlines()
    )
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


def test_published_base_image_is_used_without_build_for_restore_and_rollback(tmp_path):
    repo, base_ref, candidate_ref = _install_repo(tmp_path, modern_auth=True)
    result, log_path, _, _ = _run(
        repo, ["--base", base_ref, "--candidate", candidate_ref,
               "--base-image", "registry.example.test/odograph@sha256:" + "a" * 64],
        tmp_path, fail_up=False, modern_auth=True,
    )
    assert result.returncode == 0, result.stderr
    base_up = [line for line in log_path.read_text().splitlines()
               if line.startswith("compose|") and "/base/" in line and " up " in line]
    assert base_up
    assert all("compose.base-image.override.yml" in line for line in base_up)
    assert all("--build" not in line for line in base_up)
    assert any("--no-build app" in line for line in base_up)


@pytest.mark.parametrize("frontend", ["docker", "podman"])
@pytest.mark.parametrize("published", [False, True])
def test_backup_restore_children_use_the_parent_compose_file_set(tmp_path, frontend, published):
    repo, base_ref, candidate_ref = _install_repo(tmp_path, candidate_db_image="db:native")
    args = ["--base", base_ref, "--candidate", candidate_ref, "--database-image-migration"]
    if published:
        args += ["--base-image", "registry.example.test/base:v1",
                 "--candidate-image", "registry.example.test/candidate:v2"]
    result, log, _, _ = _run(repo, args, tmp_path, frontend=frontend, fail_up=False)
    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    config = {line.split("|")[1]: re.findall(r"-f (\S+)", line)
              for line in lines if line.startswith("compose|") and line.endswith(" config")}
    children = [line.split("|") for line in lines if line.startswith("child|")]
    assert len(children) == 7
    assert {"/base/" in child[2] for child in children} == {False, True}
    for _, _, directory, files, command in children:
        assert files.split(":") == config[directory]
        assert command == ("docker compose" if frontend == "docker" else "podman-compose")


@pytest.mark.parametrize("issuance_fails", [False, True])
def test_schema26_base_uses_the_seed_account_and_issued_device_logins(tmp_path, issuance_fails):
    repo, base_ref, candidate_ref = _install_repo(tmp_path, modern_auth=True, owned_schema=True)
    result, log, _, scratch = _run(
        repo, ["--base", base_ref, "--candidate", candidate_ref], tmp_path,
        fail_up=False, modern_auth=True, owned_schema=True,
        auth_fault="issuance-failed" if issuance_fails else "",
    )
    assert (result.returncode != 0) is issuance_fails, result.stderr
    assert (tmp_path / "signup-email").read_text() == "development@localhost.invalid"
    lines = log.read_text().splitlines()
    if issuance_fails:
        assert "could not issue synthetic tracking credentials" in result.stderr
        assert not any(line.startswith("child|") for line in lines)
        assert "tracking-issued-login" not in lines
    else:
        assert "tracking-issued-login" in lines
        assert lines.count("ingest-issued-login") == 2
        assert any("c.kind = 'device'" in line and "odograph_restore" in line for line in lines)
        assert "all drill steps passed" in result.stdout
    for secret in ("synthetic-track-secret", "synthetic-restore-secret"):
        assert secret not in result.stdout + result.stderr + log.read_text()
    assert list(scratch.glob("upgrade_check.*")) == []


@pytest.mark.parametrize("count,enabled", [(1, True), (2, True), (1, False), (0, True)])
def test_tracking_issuance_refuses_any_other_account_state(monkeypatch, capsys, count, enabled):
    from app import account_context, accounts, application_roles, tracking

    program = _extract_function(UPGRADE_SCRIPT.read_text(), "issue_synthetic_tracking_credentials")
    program = program.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    control, runtime, scoped = object(), object(), object()
    created = []

    class Connection:
        async def execute(self, statement):
            assert statement == "SELECT count(*) FROM accounts"
            return self

        async def fetchone(self):
            return (count,)

    @asynccontextmanager
    async def pools(url):
        assert url == "synthetic-database-url"
        yield SimpleNamespace(control=control, runtime=runtime)

    @asynccontextmanager
    async def control_connection(pool):
        assert pool is control
        yield Connection()

    async def lookup(conn, email):
        assert email == "development@localhost.invalid"
        return {"id": 41, "is_enabled": enabled, "auth_version": 3} if count else None

    @asynccontextmanager
    async def connection():
        yield scoped

    def account_pool(pool, principal):
        assert pool is runtime
        assert principal == account_context.AccountPrincipal(41, True, 3)
        return SimpleNamespace(connection=connection)

    async def create_device(conn, label):
        assert conn is scoped
        created.append(label)
        return SimpleNamespace(username="odograph_" + label, secret="synthetic-secret")

    monkeypatch.setenv("DATABASE_URL", "synthetic-database-url")
    monkeypatch.setattr(application_roles, "application_role_pools", pools)
    monkeypatch.setattr(account_context, "control_connection", control_connection)
    monkeypatch.setattr(account_context, "AccountPool", account_pool)
    monkeypatch.setattr(accounts, "get_account_by_email", lookup)
    monkeypatch.setattr(tracking, "create_device", create_device)
    if count == 1 and enabled:
        exec(compile(program, "drill-tracking-issuance", "exec"), {})
        assert created == ["test", "postrestore-check"]
        assert "odograph_test" in capsys.readouterr().out
    else:
        with pytest.raises(RuntimeError, match="sole synthetic development account"):
            exec(compile(program, "drill-tracking-issuance", "exec"), {})
        assert created == []
        assert capsys.readouterr().out == ""


def test_occupied_port_probe_keeps_failure_diagnostics_visible(tmp_path):
    helper = _extract_function(UPGRADE_SCRIPT.read_text(), "port_in_use")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        script = _write_executable(
            tmp_path / "probe.sh",
            f"#!/usr/bin/env bash\nHEALTH_PORT={listener.getsockname()[1]}\n{helper}\n"
            'if port_in_use; then echo "port is occupied" >&2; exit 9; fi\nexit 0',
        )
        result = subprocess.run([str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 9
    assert result.stderr == "port is occupied\n"


@pytest.mark.parametrize("frontend", ["docker compose", "podman-compose"])
def test_image_cleanup_removes_exact_task_tags_only(tmp_path, frontend):
    source = UPGRADE_SCRIPT.read_text()
    functions = "\n".join(_extract_function(source, name)
                          for name in ("runtime_cmd", "remove_stamp_images"))
    project = "mtdrill12345"
    own_images = [f"localhost/{project}_app:dev", f"{project}-app:latest",
                  f"{project}_app:dev", f"localhost/{project}-app:old"]
    images = tmp_path / "images"
    images.write_text("\n".join(own_images + [
        own_images[0], f"localhost/{project}_app:<none>",
        f"localhost/{project}6_app:dev", f"{project}-app-other:dev",
        "ghcr.io/example/odograph:v1", "<none>:<none>",
    ]) + "\n")
    log = tmp_path / "runtime.log"
    binary = "docker" if frontend == "docker compose" else "podman"
    _write_executable(
        tmp_path / binary,
        "#!/usr/bin/env bash\nset -eu\n"
        'printf "%s\\n" "$*" >> "$RUNTIME_LOG"\n'
        'case "$1" in\n'
        '  image) [ "$*" = "image ls --format {{.Repository}}:{{.Tag}}" ]; cat "$IMAGES" ;;\n'
        '  rmi) [ "$#" = 2 ] ;;\n'
        '  *) exit 99 ;;\nesac',
    )
    script = _write_executable(
        tmp_path / "cleanup.sh",
        f"#!/usr/bin/env bash\nset -euo pipefail\nPROJECT={project}\n"
        f"compose_cmd='{frontend}'\n{functions}\nremove_stamp_images",
    )
    result = subprocess.run(
        [str(script)], capture_output=True, text=True, timeout=10,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "RUNTIME_LOG": str(log), "IMAGES": str(images)},
    )
    assert result.returncode == 0, result.stderr
    removed = [line.removeprefix("rmi ") for line in log.read_text().splitlines()
               if line.startswith("rmi ")]
    assert removed == sorted(own_images)
