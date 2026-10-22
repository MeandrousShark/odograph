from __future__ import annotations

import os
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "rehearse_upgrade.sh"
SENTINEL = "sentinel-7f3a-row-value@example.invalid"
SENTINEL_COORDINATE = "37.76940,-122.48300"
OUTBOUND_PREFIXES = ("SMTP_", "GEOCODE_", "OSRM_", "OIDC_", "NTFY_URL", "NTFY_TOKEN",
                     "NTFY_USERNAME", "NTFY_PASSWORD", "ADMIN_TOKEN", "APP_URL", "EMAIL_FROM")


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content + "\n")
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# Stands in for the candidate's restore script. Its output carries a seeded
# value so a summary that copied it would fail the sentinel checks.
_FAKE_RESTORE = r"""#!/usr/bin/env bash
set -u
printf 'restore|%s|%s|%s|%s|%s\n' "$PWD" "${COMPOSE_FILE:-}" "${COMPOSE_CMD:-}" "${TMPDIR:-}" "$*" >> "$FAKE_LOG"
echo "restored row for $FAKE_SENTINEL"
if [ "${1-}" = --verify-only ]; then
    exit "${FAKE_VERIFY_EXIT:-0}"
fi
[ "${FAKE_RESTORE_EXIT:-0}" = 0 ] || exit "$FAKE_RESTORE_EXIT"
: > "$FAKE_STATE/restored"
"""


def _install_repo(tmp_path: Path, *, build_override: bool = True) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    _write_executable(scripts_dir / "rehearse_upgrade.sh", SCRIPT.read_text())
    _write_executable(scripts_dir / "generate_env.sh", (REPO_ROOT / "scripts" / "generate_env.sh").read_text())
    shutil.copy(REPO_ROOT / ".env.example", repo / ".env.example")
    _write_executable(scripts_dir / "restore_database.sh", _FAKE_RESTORE)
    (repo / "compose.yaml").write_text("services:\n  db:\n    image: db:candidate\n  app:\n    image: app:pinned\n")
    if build_override:
        (repo / "compose.build.override.yml").write_text("services:\n  app:\n    build: .\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Rehearsal Test")
    _git(repo, "config", "user.email", "rehearsal@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    return repo, _git(repo, "rev-parse", "HEAD")


_COMPOSE_BODY = r"""
original="$*"
printf 'compose|%s|%s\n' "$PWD" "$original" >> "$FAKE_LOG"
while [ "${1-}" = "-f" ]; do
    [ "$#" -ge 2 ] || exit 90
    [ -f "$2" ] || exit 89
    shift 2
done

after_state() { [ -f "$FAKE_STATE/app-stopped" ]; }
lines_of() { printf '%b' "$1" | sed '/^$/d'; }

case "${1-}" in
    config)
        cp compose.rehearsal.override.yml "$FAKE_STATE/override.yml"
        port="$(sed -n 's/.*"127\.0\.0\.1:\([0-9]*\):8000".*/\1/p' compose.rehearsal.override.yml)"
        printf 'networks:\n  default:\n'
        if grep -q 'internal: true' compose.rehearsal.override.yml && [ -z "${FAKE_RENDER_NOT_INTERNAL:-}" ]; then
            printf '    internal: true\n'
        else
            printf '    internal: false\n'
        fi
        printf 'services:\n  app:\n    image: app:candidate\n    ports:\n'
        [ -z "${FAKE_RENDER_EXTRA_PORT:-}" ] || printf '    - 127.0.0.1:8077:8000\n'
        printf '    - 127.0.0.1:%s:8000\n' "$port"
        printf "    restart: 'no'\n"
        printf "  db:\n    image: db:candidate\n    restart: 'no'\n"
        ;;
    pull|build)
        [ "$1" != build ] || : > "$FAKE_STATE/built"
        ;;
    up)
        cp .env "$FAKE_STATE/env-at-up"
        case " $original " in *" db "*) : > "$FAKE_STATE/db-started" ;; esac
        case " $original " in
            *" app "|*" app")
                : > "$FAKE_STATE/app-started"
                exit "${FAKE_APP_UP_EXIT:-0}"
                ;;
        esac
        ;;
    stop)
        case " $original " in *" app"*) : > "$FAKE_STATE/app-stopped" ;; esac
        ;;
    down)
        : > "$FAKE_STATE/down"
        ;;
    run)
        case "$original" in
            *"python -c"*"application_roles import TABLES"*)
                printf '%s\n' "${@: -1}" > "$FAKE_STATE/contract-arg"
                echo "compose frontend noise"
                for table in $FAKE_TABLES; do echo "REHEARSAL table $table"; done
                lines_of "${FAKE_DROPS:-}" | while IFS= read -r drop; do echo "REHEARSAL drop $drop"; done
                echo "REHEARSAL latest ${FAKE_LATEST:-45}"
                exit 0
                ;;
        esac
        exit 93
        ;;
    exec)
        case "$original" in
            *"db pg_isready"*) exit 0 ;;
            *"app python3 -c"*healthz*)
                [ "${FAKE_HEALTH:-ok}" = ok ] || exit 1
                exit 0
                ;;
            *"app python -m app.application_roles verify"*)
                echo "verify output for $FAKE_SENTINEL"
                exit "${FAKE_VERIFY_ROLE_EXIT:-0}"
                ;;
            *"to_regclass('public.schema_migrations')"*) echo t; exit 0 ;;
            *"max(version)"*)
                if after_state; then echo "${FAKE_SCHEMA_AFTER:-45}"; else echo "${FAKE_SCHEMA_BEFORE:-25}"; fi
                exit 0
                ;;
            *query_to_xml*)
                if after_state; then lines_of "$FAKE_COUNTS_AFTER"; else lines_of "$FAKE_COUNTS_BEFORE"; fi
                exit 0
                ;;
            *"SELECT count(*) FROM pg_class"*"'r','p','v','m','f'"*)
                if after_state; then lines_of "$FAKE_RELATIONS_AFTER" | wc -l; else lines_of "$FAKE_RELATIONS_BEFORE" | wc -l; fi
                exit 0
                ;;
            *"SELECT count(*) FROM pg_class"*"'r','p')"*)
                if after_state; then lines_of "$FAKE_COUNTS_AFTER" | wc -l; else lines_of "$FAKE_COUNTS_BEFORE" | wc -l; fi
                exit 0
                ;;
            *"SELECT c.relname FROM pg_class"*)
                if after_state; then lines_of "$FAKE_RELATIONS_AFTER"; else lines_of "$FAKE_RELATIONS_BEFORE"; fi
                exit 0
                ;;
            *psql*)
                # An unexpected query gets a seeded value, never a quiet zero.
                echo "$FAKE_SENTINEL"
                exit 0
                ;;
        esac
        exit 93
        ;;
    *)
        printf 'unexpected compose invocation: %s\n' "$original" >&2
        exit 91
        ;;
esac
""".strip()


_RUNTIME_BODY = r"""
printf 'runtime|%s\n' "$*" >> "$FAKE_LOG"
project() { cat "$FAKE_STATE/project" 2>/dev/null || true; }
case "${1-}" in
    info) exit 0 ;;
    ps)
        case "$*" in
            *label=com.docker.compose.project=*)
                all="$*"
                p="${all##*label=com.docker.compose.project=}"
                p="${p%% *}"
                printf '%s\n' "$p" > "$FAKE_STATE/project"
                ;;
        esac
        case "$*" in
            *service=app*) [ ! -f "$FAKE_STATE/app-started" ] || echo app-container ;;
            *service=db*) [ ! -f "$FAKE_STATE/db-started" ] || echo db-container ;;
            *)
                if [ "${FAKE_EXISTING:-}" = container ]; then echo existing-container; fi
                ;;
        esac
        exit 0
        ;;
    inspect)
        case "$*" in
            *State.Status*) echo "${FAKE_APP_STATE:-running}" ;;
            *State.ExitCode*) echo 3 ;;
            *Image*app-container*) echo sha256:candidate-app-image ;;
            *Image*db-container*) echo sha256:candidate-db-image ;;
            *) exit 94 ;;
        esac
        exit 0
        ;;
    logs)
        cat "$FAKE_APP_LOGS"
        exit 0
        ;;
    volume)
        case "${2-}" in
            ls)
                echo other_dbdata
                if [ "${FAKE_EXISTING:-}" = volume ] || { [ -f "$FAKE_STATE/db-started" ] && [ ! -f "$FAKE_STATE/volume-removed" ]; }; then
                    echo "$(project)_dbdata"
                fi
                ;;
            rm) : > "$FAKE_STATE/volume-removed" ;;
        esac
        exit 0
        ;;
    network)
        case "${2-}" in
            ls)
                echo other_default
                if [ -f "$FAKE_STATE/db-started" ] && [ ! -f "$FAKE_STATE/down" ]; then echo "$(project)_default"; fi
                ;;
        esac
        exit 0
        ;;
    image)
        echo ghcr.io/example/odograph:v9.9.9
        [ ! -f "$FAKE_STATE/built" ] || echo "localhost/$(project)_app:dev"
        exit 0
        ;;
    rmi|rm) exit 0 ;;
esac
exit 92
""".strip()


def _install_frontend(bin_dir: Path, frontend: str) -> str:
    if frontend == "docker":
        _write_executable(
            bin_dir / "docker",
            "#!/usr/bin/env bash\nset -u\n"
            "if [ \"${1-}\" = compose ]; then shift\n" + _COMPOSE_BODY + "\nexit $?\nfi\n"
            + _RUNTIME_BODY,
        )
        return "docker compose"
    _write_executable(bin_dir / "podman-compose", "#!/usr/bin/env bash\nset -u\n" + _COMPOSE_BODY)
    _write_executable(bin_dir / "podman", "#!/usr/bin/env bash\nset -u\n" + _RUNTIME_BODY)
    return "podman-compose"


DEFAULT_LOGS = "\n".join([
    f"2026-10-13T10:00:00.000000000Z app INFO starting for {SENTINEL}",
    "2026-10-13T10:00:01.000000000Z app.db INFO applying migration 026_account_ownership.sql",
    "2026-10-13T10:00:13.500000000Z app.db INFO applying migration 027_device_labels.sql",
    f"2026-10-13T10:00:14.250000000Z app INFO point {SENTINEL_COORDINATE} for {SENTINEL}",
]) + "\n"
DEFAULT_TABLES = "accounts points schema_migrations trips"


class Run:
    def __init__(self, result, log_path: Path, state: Path, scratch_root: Path):
        self.result = result
        self.state = state
        self.scratch_root = scratch_root
        self.lines = log_path.read_text().splitlines() if log_path.exists() else []

    @property
    def compose_lines(self) -> list[str]:
        return [line for line in self.lines if line.startswith("compose|")]

    @property
    def runtime_lines(self) -> list[str]:
        return [line for line in self.lines if line.startswith("runtime|")]

    @property
    def summary_path(self) -> Path:
        matches = sorted(self.scratch_root.glob("odorehearse*-summary.txt"))
        assert len(matches) == 1, matches
        return matches[0]

    @property
    def summary(self) -> str:
        return self.summary_path.read_text()

    @property
    def project(self) -> str:
        return (self.state / "project").read_text().strip()


def _archive(tmp_path: Path, *, sidecar: bool = True) -> Path:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(exist_ok=True)
    archive = archive_dir / "pre-upgrade.dump"
    archive.write_bytes(b"fake archive " + SENTINEL.encode())
    if sidecar:
        (archive_dir / "pre-upgrade.dump.sha256").write_text("0" * 64 + "  pre-upgrade.dump\n")
    return archive


def _run(
    tmp_path: Path,
    extra_args: list[str] | None = None,
    *,
    frontend: str = "podman",
    archive: Path | None = None,
    candidate: str | None = None,
    build_override: bool = True,
    logs: str = DEFAULT_LOGS,
    **fake: str,
) -> Run:
    if not (tmp_path / "repo").exists():
        _install_repo(tmp_path, build_override=build_override)
    repo = tmp_path / "repo"
    commit = _git(repo, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    scratch_root = tmp_path / "scratch"
    for directory in (bin_dir, state, scratch_root):
        directory.mkdir(exist_ok=True)
    compose_cmd = _install_frontend(bin_dir, frontend)
    _write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n"
        "printf 'http|%s\\n' \"${!#}\" >> \"$FAKE_LOG\"\n"
        "printf '%s' 200",
    )
    log_path = tmp_path / "fake.log"
    app_logs = tmp_path / "app.log"
    app_logs.write_text(logs)
    if archive is None:
        archive = _archive(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "COMPOSE_CMD": compose_cmd,
        "TMPDIR": str(scratch_root),
        "FAKE_LOG": str(log_path),
        "FAKE_STATE": str(state),
        "FAKE_APP_LOGS": str(app_logs),
        "FAKE_SENTINEL": SENTINEL,
        "FAKE_TABLES": DEFAULT_TABLES,
        "FAKE_RELATIONS_BEFORE": "accounts\\npoints\\nschema_migrations\\ntrips\\n",
        "FAKE_RELATIONS_AFTER": "accounts\\npoints\\nschema_migrations\\ntrips\\n",
        "FAKE_COUNTS_BEFORE": "accounts\\t1\\npoints\\t1200\\nschema_migrations\\t25\\ntrips\\t40\\n",
        "FAKE_COUNTS_AFTER": "accounts\\t1\\npoints\\t1200\\nschema_migrations\\t45\\ntrips\\t40\\n",
        **fake,
    }
    for key in ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "POSTGRES_PASSWORD"):
        env.pop(key, None)
    args = ["--archive", str(archive), "--candidate", candidate or commit, *(extra_args or [])]
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "rehearse_upgrade.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return Run(result, log_path, state, scratch_root)


def _env_keys(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _script_array(name: str) -> list[str]:
    match = re.search(rf"^{name}=\(\n(.*?)^\)$", SCRIPT.read_text(), re.M | re.S)
    assert match, name
    return match.group(1).split()


# --- argument contract -----------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "--archive and --candidate are both required"),
        (["--archive", "x.dump"], "--archive and --candidate are both required"),
        (["--candidate", "HEAD"], "--archive and --candidate are both required"),
        (["--archive"], "--archive requires a value"),
        (["--archive", "x", "--candidate", "HEAD", "--bogus"], "unrecognized argument: --bogus"),
        (["--archive", "x", "--candidate", "HEAD", "--health-timeout", "0"], "positive whole number"),
        (["--archive", "x", "--candidate", "HEAD", "--health-timeout", "soon"], "positive whole number"),
    ],
)
def test_argument_contract(tmp_path, args, message):
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=10, cwd=tmp_path
    )

    assert result.returncode != 0
    assert message in result.stderr


def test_help_lists_every_option(tmp_path):
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10)

    assert result.returncode == 0
    for option in ("--archive PATH", "--candidate REF", "--candidate-image IMAGE", "--legacy-env FILE",
                   "--health-timeout SECONDS", "--output PATH", "--keep"):
        assert option in result.stderr


def test_missing_archive_or_sidecar_is_refused_before_any_container_work(tmp_path):
    missing = _run(tmp_path, archive=tmp_path / "absent.dump")
    assert missing.result.returncode != 0
    assert "archive not found" in missing.result.stderr
    assert missing.compose_lines == [] and missing.runtime_lines == []

    (tmp_path / "other").mkdir()
    no_sidecar = _run(tmp_path, archive=_archive(tmp_path / "other", sidecar=False))
    assert no_sidecar.result.returncode != 0
    assert "checksum sidecar not found" in no_sidecar.result.stderr
    assert no_sidecar.compose_lines == [] and no_sidecar.runtime_lines == []


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "summary.txt"
    output.write_text("previous summary\n")

    run = _run(tmp_path, ["--output", str(output)])

    assert run.result.returncode != 0
    assert "already exists" in run.result.stderr
    assert output.read_text() == "previous summary\n"
    assert run.compose_lines == []


def test_unknown_candidate_ref_is_refused(tmp_path):
    run = _run(tmp_path, candidate="no-such-ref")

    assert run.result.returncode != 0
    assert "does not resolve to a commit" in run.result.stderr
    assert run.compose_lines == []


@pytest.mark.parametrize(
    "image",
    [
        "registry.example.test/odograph",
        "registry.example.test/odograph:latest",
        "registry.example.test/odograph:LATEST",
        "registry.example.test/odograph:v1",
        "registry.example.test/odograph:v0.11",
        "registry.example.test/odograph:1.2.3",
        "registry.example.test/odograph:V1.2.3",
        "registry.example.test/odograph:v1.2.3.4",
        "registry.example.test:5000/odograph",
        "registry.example.test/odograph@sha256:1234abcd",
    ],
)
def test_candidate_image_rejects_mutable_or_incomplete_references(tmp_path, image):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--archive", "x", "--candidate", "HEAD", "--candidate-image", image],
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode != 0
    assert "exact release tag (vX.Y.Z) or a full sha256 digest" in result.stderr


@pytest.mark.parametrize(
    "image",
    ["registry.example.test/odograph@sha256:" + "a" * 64, "registry.example.test/odograph:v0.12.0"],
)
def test_candidate_image_is_pulled_and_used_without_build(tmp_path, image):
    run = _run(tmp_path, ["--candidate-image", image])

    assert run.result.returncode == 0, run.result.stderr
    assert any(line.endswith(" pull app") for line in run.compose_lines)
    assert not any(" build " in f"{line} " for line in run.compose_lines)
    assert all("-f compose.candidate-image.override.yml" in line for line in run.compose_lines)
    assert f"candidate app image: {image}" in run.summary
    # Never removed: only this project's own build repository is.
    assert not any(line.startswith("runtime|rmi") and "ghcr.io" in line for line in run.runtime_lines)


# --- isolation ---------------------------------------------------------------


@pytest.mark.parametrize("frontend", ["podman", "docker"])
def test_every_compose_call_uses_the_internal_network_override(tmp_path, frontend):
    run = _run(tmp_path, frontend=frontend)

    assert run.result.returncode == 0, run.result.stderr
    assert run.compose_lines
    for line in run.compose_lines:
        assert "-f compose.yaml -f compose.build.override.yml -f compose.rehearsal.override.yml " in line
        assert f"/{run.project}|" in line
    override = (run.state / "override.yml").read_text()
    assert "networks:\n  default:\n    internal: true\n" in override
    assert "ports: !override\n" in override
    assert "8077" not in override
    assert override.count("restart: \"no\"") == 2
    port = re.search(r'"127\.0\.0\.1:(\d+):8000"', override).group(1)
    assert port != "8077"
    assert f"health port: 127.0.0.1:{port} " in run.summary
    restore_lines = [line for line in run.lines if line.startswith("restore|")]
    assert len(restore_lines) == 2
    for line in restore_lines:
        _, cwd, compose_file, compose_cmd, tmpdir, _ = line.split("|", 5)
        assert compose_file == "compose.yaml:compose.build.override.yml:compose.rehearsal.override.yml"
        assert cwd.endswith("/" + run.project)
        assert tmpdir.startswith(str(run.scratch_root))


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ({"FAKE_RENDER_NOT_INTERNAL": "1"}, "network default is not internal"),
        ({"FAKE_RENDER_EXTRA_PORT": "1"}, "app must publish exactly one port"),
    ],
)
def test_rendered_config_must_be_isolated_before_anything_starts(tmp_path, fault, message):
    run = _run(tmp_path, **fault)

    assert run.result.returncode != 0
    assert message in run.result.stderr
    assert not any(" up " in f"{line} " for line in run.compose_lines)
    assert "stopped at: step 3: rendered Compose configuration is not isolated as required" in run.summary


def test_generated_environment_holds_no_outbound_credentials(tmp_path):
    run = _run(tmp_path)

    assert run.result.returncode == 0, run.result.stderr
    env = _env_keys((run.state / "env-at-up").read_text())
    non_empty = {key for key, value in env.items() if value.strip()}
    assert non_empty <= set(_script_array("SYNTHETIC_ENV_KEYS"))
    assert {"POSTGRES_PASSWORD", "INGEST_PASSWORD", "SESSION_SECRET"} <= non_empty
    for key in env:
        assert not key.startswith(OUTBOUND_PREFIXES), key
    assert "environment: synthetic\n" in run.summary


def test_generated_environment_with_an_outbound_key_is_refused(tmp_path):
    repo, _ = _install_repo(tmp_path)
    example = repo / ".env.example"
    example.write_text(example.read_text() + "SMTP_HOST=smtp.example.test\n")
    _git(repo, "commit", "-qam", "outbound default")

    run = _run(tmp_path)

    assert run.result.returncode != 0
    assert "outside the rehearsal allowlist: SMTP_HOST" in run.result.stderr
    assert "smtp.example.test" not in run.result.stderr + run.result.stdout
    assert not any(" up " in f"{line} " for line in run.compose_lines)


# --- legacy environment ----------------------------------------------------


def test_legacy_env_copies_only_allowlisted_keys_by_name(tmp_path):
    legacy = tmp_path / "legacy.env"
    legacy.write_text(
        "# legacy preferences\n"
        "DISPLAY_TZ=Europe/Paris\n"
        f"EMAIL_TO={SENTINEL}\n"
        "export MILEAGE_RATE_2024=0.67\n"
        "SMTP_HOST=\n"
        "OIDC_CLIENT_SECRET=''\n"
    )

    run = _run(tmp_path, ["--legacy-env", str(legacy)])

    assert run.result.returncode == 0, run.result.stderr
    env_text = (run.state / "env-at-up").read_text()
    env = _env_keys(env_text)
    assert env["DISPLAY_TZ"] == "Europe/Paris"
    assert env_text.count("DISPLAY_TZ=") == 1
    assert env["EMAIL_TO"] == SENTINEL
    assert env["MILEAGE_RATE_2024"] == "0.67"
    assert "SMTP_HOST" not in env and "OIDC_CLIENT_SECRET" not in env
    assert ("environment: synthetic plus allowlisted legacy keys: "
            "DISPLAY_TZ EMAIL_TO MILEAGE_RATE_2024\n") in run.summary
    assert SENTINEL not in run.summary
    assert "Europe/Paris" not in run.summary


@pytest.mark.parametrize(
    ("content", "message", "named"),
    [
        (f"DISPLAY_TZ=UTC\nSMTP_PASSWORD={SENTINEL}\nOIDC_CLIENT_SECRET={SENTINEL}x\n",
         "outside the rehearsal allowlist: SMTP_PASSWORD OIDC_CLIENT_SECRET", True),
        (f"INGEST_PASSWORD={SENTINEL}\n", "outside the rehearsal allowlist: INGEST_PASSWORD", True),
        (f"NTFY_URL={SENTINEL}\n", "outside the rehearsal allowlist: NTFY_URL", True),
        (f"POSTGRES_PASSWORD={SENTINEL}\n", "outside the rehearsal allowlist: POSTGRES_PASSWORD", True),
        (f"EMAIL_TO={SENTINEL}\nEMAIL_TO={SENTINEL}\n", "sets EMAIL_TO more than once", False),
        (f"EMAIL_TO=\"{SENTINEL}\n", "unterminated quoted value", False),
        (f"not an assignment {SENTINEL}\n", "line 1 is not a KEY=VALUE assignment", False),
    ],
)
def test_legacy_env_refuses_without_echoing_values(tmp_path, content, message, named):
    legacy = tmp_path / "legacy.env"
    legacy.write_text(content)

    run = _run(tmp_path, ["--legacy-env", str(legacy)])

    assert run.result.returncode != 0
    assert message in run.result.stderr
    assert SENTINEL not in run.result.stderr + run.result.stdout
    assert run.compose_lines == [] and run.runtime_lines == []
    if named:
        assert "MILEAGE_RATE_<YEAR>" in run.result.stderr


_PREFERENCE_SAMPLES = {
    "DISPLAY_TZ": ("Europe/Paris",),
    "NTFY_TOPIC": ("rehearsal-topic",),
    "NUDGE_WEEKLY_HOUR": ("3",),
    "ODOMETER_REMINDER": ("1", "0"),
    "ODOMETER_REMINDER_HOUR": ("4",),
    "EMAIL_TO": ("someone@example.invalid",),
    "EMAIL_WEEKLY_NUDGE": ("1", "0"),
    "EMAIL_MONTHLY_SUMMARY": ("0", "1"),
    "EMAIL_FILING_REMINDER": ("0", "1"),
    "EMAIL_ODOMETER_REMINDER": ("1", "0"),
    "EMAIL_DIGEST_HOUR": ("5",),
    "EMAIL_FILING_REMINDER_MMDD": ("02-02",),
}


def test_legacy_preference_allowlist_matches_the_imported_preference_columns(monkeypatch):
    from app.account_settings import CONFIG_PREFERENCE_COLUMNS
    from app.config import Config

    keys = _script_array("LEGACY_ENV_PREFERENCE_KEYS")
    assert len(keys) == len(set(keys))
    assert set(keys) == set(_PREFERENCE_SAMPLES), "add a sample value for each new allowlisted key"
    for key in [*keys, "NTFY_URL"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://rehearsal@localhost/rehearsal")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    baseline = Config.from_env()

    mapped = {}
    for key in keys:
        changed = set()
        for value in _PREFERENCE_SAMPLES[key]:
            monkeypatch.setenv(key, value)
            config = Config.from_env()
            changed |= {column for column in CONFIG_PREFERENCE_COLUMNS
                        if getattr(config, column) != getattr(baseline, column)}
            monkeypatch.delenv(key)
        assert len(changed) == 1, (key, changed)
        mapped[key] = changed.pop()

    assert sorted(mapped.values()) == sorted(CONFIG_PREFERENCE_COLUMNS)


def test_legacy_rate_pattern_matches_the_imported_rate_overrides():
    from app.ownership import legacy_rate_overrides

    pattern = re.search(r"^LEGACY_ENV_RATE_PATTERN='([^']+)'$", SCRIPT.read_text(), re.M).group(1)
    assert re.fullmatch(pattern, "MILEAGE_RATE_2024")
    assert legacy_rate_overrides({"MILEAGE_RATE_2024": "0.67"}) == {2024: Decimal("0.67")}
    # Keys outside the pattern import nothing, so refusing them loses nothing.
    for key in ("MILEAGE_RATE_", "MILEAGE_RATE_X", "MILEAGE_RATES_2024"):
        assert not re.fullmatch(pattern, key)
        assert legacy_rate_overrides({key: "0.67"}) == {}
    assert "if not key.startswith(\"MILEAGE_RATE_\")" in Path(
        REPO_ROOT / "app" / "ownership.py").read_text()


# --- relation set, counts and timings ---------------------------------------


def test_unknown_relations_are_reported_and_startup_is_still_attempted(tmp_path):
    run = _run(
        tmp_path,
        FAKE_RELATIONS_BEFORE="accounts\\napp_settings\\noperator_notes\\npoints\\nschema_migrations\\ntrips\\n",
        FAKE_RELATIONS_AFTER="accounts\\noperator_notes\\npoints\\nschema_migrations\\ntrips\\n",
        FAKE_DROPS="026 app_settings\\n",
        FAKE_HEALTH="never",
        FAKE_APP_STATE="exited",
    )

    assert run.result.returncode == 1
    assert "startup will refuse: unknown public relations: operator_notes" in run.result.stderr
    summary = run.summary
    assert "result: FAIL\n" in summary
    assert "FAIL: startup will refuse: unknown public relations: operator_notes\n" in summary
    assert "before startup, removed by a pending migration: app_settings (migration 026)\n" in summary
    assert "after startup, unknown: operator_notes\n" in summary
    assert any(line.endswith(" up -d --no-build app") for line in run.compose_lines)
    assert "startup: failed (app exited with code 3)\n" in summary
    assert (run.state / "contract-arg").read_text().strip() == "25"


def test_relation_comparison_matches_the_startup_contract_check():
    source = SCRIPT.read_text()
    contract = (REPO_ROOT / "app" / "application_roles.py").read_text()
    assert "c.relkind IN ('r','p','v','m','f')" in source
    assert "c.relkind IN ('r','p','v','m','f') " in contract
    assert "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')" in source
    assert "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')" in contract


def test_successful_rehearsal_reports_counts_timings_and_identities(tmp_path):
    run = _run(tmp_path)

    assert run.result.returncode == 0, run.result.stderr
    summary = run.summary
    assert "result: PASS\n" in summary
    assert "schema before: 25\n" in summary
    assert "schema after: 45\n" in summary
    assert "candidate latest migration: 45\n" in summary
    assert "startup: healthy\n" in summary
    assert "role contract verify: pass\n" in summary
    assert "candidate app image: source build of the candidate commit\n" in summary
    assert "candidate app image id: sha256:candidate-app-image\n" in summary
    assert "database image: db:candidate\n" in summary
    assert "database image id: sha256:candidate-db-image\n" in summary
    assert "loopback reachable: yes" in summary
    assert "  026_account_ownership.sql 12.500\n" in summary
    assert "  027_device_labels.sql 0.750\n" in summary
    assert re.search(r"^  points +1200 +1200 +0$", summary, re.M)
    assert re.search(r"^  schema_migrations +25 +45 +\+20$", summary, re.M)
    assert "ingest_credentials grows by one by design" in summary
    # Counts after are taken with the app stopped.
    stop = next(i for i, line in enumerate(run.compose_lines) if line.endswith(" stop app"))
    assert any("query_to_xml" in line for line in run.compose_lines[stop:])


@pytest.mark.parametrize(
    ("logs", "expected"),
    [
        (
            "2026-10-13T06:00:00.123456789-04:00 app.db INFO applying migration 041_geocode.sql\n"
            "2026-10-13T06:01:40.623456789-04:00 app.db INFO applying migration 042_storage.sql\n"
            "2026-10-13T06:01:41.000000000-04:00 uvicorn INFO Application startup complete.\n",
            ["041_geocode.sql 100.500", "042_storage.sql 0.377"],
        ),
        (
            "app-1  | 2026-10-13T10:00:00Z app.db INFO applying migration 040_backfill.sql\n"
            "app-1  | 2026-10-13T10:30:00.5Z app INFO done\n",
            ["040_backfill.sql 1800.500"],
        ),
        (
            "2026-10-13T10:00:00Z app.db INFO applying migration 026_account_ownership.sql\n"
            "2026-10-13T10:00:02Z app INFO intermediate step\n"
            "2026-10-13T10:00:09.25Z app.db INFO applying migration 027_device_labels.sql\n"
            "2026-10-13T10:00:10Z app INFO later step\n"
            "2026-10-13T10:00:12Z app INFO Application startup complete.\n",
            ["026_account_ownership.sql 9.250", "027_device_labels.sql 0.750"],
        ),
        (
            "2026-10-13T10:00:00Z app.db INFO applying migration 044_trip_warning_acks.sql\n",
            ["044_trip_warning_acks.sql unknown"],
        ),
        ("2026-10-13T10:00:00Z app INFO nothing pending\n", []),
    ],
)
def test_migration_timings_are_parsed_from_timestamped_logs(tmp_path, logs, expected):
    run = _run(tmp_path, logs=logs)

    assert run.result.returncode == 0, run.result.stderr
    section = run.summary.split("migrations (seconds, from container log timestamps):\n", 1)[1]
    section = section.split("\n\n", 1)[0].splitlines()
    assert section == ([f"  {line}" for line in expected] or ["  none recorded"])


def test_role_contract_failure_is_a_failed_rehearsal(tmp_path):
    run = _run(tmp_path, FAKE_VERIFY_ROLE_EXIT="1")

    assert run.result.returncode == 1
    assert "role contract verify: fail\n" in run.summary
    assert "FAIL: python -m app.application_roles verify failed" in run.summary


def test_startup_timeout_stops_the_app_and_still_counts(tmp_path):
    run = _run(tmp_path, ["--health-timeout", "1"], FAKE_HEALTH="never", REHEARSE_POLL_INTERVAL="0.2")

    assert run.result.returncode == 1
    assert "startup: timed out after 1s\n" in run.summary
    assert "role contract verify: not run (startup did not succeed)\n" in run.summary
    assert any(line.endswith(" stop app") for line in run.compose_lines)
    assert re.search(r"^  trips +40 +40 +0$", run.summary, re.M)


def test_query_failure_is_reported_never_a_silent_zero(tmp_path):
    run = _run(tmp_path, FAKE_COUNTS_BEFORE="accounts\\tnot-a-number\\n")

    assert run.result.returncode == 1
    assert "stopped at: step 5: could not count restored rows" in run.summary
    assert not any(line.endswith(" up -d --no-build app") for line in run.compose_lines)


# --- verification, cleanup and refusal ---------------------------------------


def test_archive_verification_failure_refuses_restore(tmp_path):
    run = _run(tmp_path, FAKE_VERIFY_EXIT="1")

    assert run.result.returncode == 1
    restore_lines = [line for line in run.lines if line.startswith("restore|")]
    assert len(restore_lines) == 1 and restore_lines[0].endswith("--verify-only " + str(tmp_path / "archive" / "pre-upgrade.dump"))
    assert "stopped at: step 4: the candidate restore_database.sh --verify-only rejected the archive" in run.summary
    assert any(line.endswith(" down") for line in run.compose_lines)


def _assert_cleaned(run: Run) -> None:
    assert any(line.endswith(" down") for line in run.compose_lines)
    assert f"runtime|volume rm {run.project}_dbdata" in run.runtime_lines
    assert not any("other_" in line for line in run.runtime_lines if " rm " in f"{line} ")
    assert [path.name for path in run.scratch_root.iterdir()] == [run.summary_path.name]


def test_cleanup_on_success_removes_only_task_resources(tmp_path):
    run = _run(tmp_path)

    assert run.result.returncode == 0, run.result.stderr
    _assert_cleaned(run)
    assert f"runtime|rmi localhost/{run.project}_app:dev" in run.runtime_lines
    assert not any("rmi ghcr.io" in line for line in run.runtime_lines)
    assert f"Summary written to: {run.summary_path}" in run.result.stdout
    assert oct(run.summary_path.stat().st_mode & 0o777) == "0o600"


def test_cleanup_on_failure_still_writes_the_summary(tmp_path):
    run = _run(tmp_path, FAKE_RESTORE_EXIT="1")

    assert run.result.returncode == 1
    _assert_cleaned(run)
    assert "result: FAIL\n" in run.summary
    assert "stopped at: step 4: restore into the fresh candidate volume failed" in run.summary


def test_keep_leaves_resources_and_names_the_raw_logs(tmp_path):
    run = _run(tmp_path, ["--keep"])

    assert run.result.returncode == 0, run.result.stderr
    assert not any(line.endswith(" down") for line in run.compose_lines)
    assert not any(" rm " in f"{line} " for line in run.runtime_lines)
    logs_dir = re.search(r"^raw container logs: (.+)$", run.summary, re.M).group(1)
    assert "WARNING: raw logs may contain personal data" in run.summary
    assert (Path(logs_dir) / "app.log").read_text() == DEFAULT_LOGS
    assert oct(Path(logs_dir).stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize("existing", ["container", "volume"])
def test_existing_project_is_refused_without_touching_anything(tmp_path, existing):
    run = _run(tmp_path, FAKE_EXISTING=existing)

    assert run.result.returncode != 0
    assert "refusing to run" in run.result.stderr
    assert run.compose_lines == []
    assert not any(word in line for line in run.runtime_lines for word in (" rm ", "rmi", "down"))
    assert not list(run.scratch_root.iterdir())


def test_explicit_output_path_receives_the_summary(tmp_path):
    output = tmp_path / "out" / "rehearsal.txt"
    output.parent.mkdir()

    run = _run(tmp_path, ["--output", str(output)])

    assert run.result.returncode == 0, run.result.stderr
    assert output.read_text().startswith("Odograph upgrade rehearsal summary\n")
    assert not list(run.scratch_root.iterdir())


# --- data values never reach the summary --------------------------------------


@pytest.mark.parametrize("fault", [{}, {"FAKE_RESTORE_EXIT": "1"}, {"FAKE_APP_STATE": "exited", "FAKE_HEALTH": "never"}])
def test_summary_never_contains_seeded_values(tmp_path, fault):
    legacy = tmp_path / "legacy.env"
    legacy.write_text(f"EMAIL_TO={SENTINEL}\nNTFY_TOPIC={SENTINEL}topic\n")

    run = _run(tmp_path, ["--legacy-env", str(legacy)], **fault)

    summary = run.summary
    assert SENTINEL not in summary
    assert SENTINEL_COORDINATE not in summary
    assert "app INFO" not in summary
    assert str(tmp_path / "archive") not in summary
    assert "legacy.env" not in summary


def test_tracked_shell_scripts_are_executable():
    # The documented commands run scripts directly, so git must record +x.
    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts/*.sh"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert listing
    not_executable = [line.split("\t", 1)[1] for line in listing if not line.startswith("100755 ")]
    assert not not_executable, f"run git update-index --chmod=+x on: {not_executable}"


def test_candidate_tree_stays_readable_for_the_image_user(tmp_path):
    # The image runs as a non-root user and copies sources with their modes.
    run = _run(tmp_path, ["--keep"])
    assert run.result.returncode == 0, run.result.stderr
    tracked = _git(tmp_path / "repo", "ls-files").splitlines()
    cand_dirs = [path for path in run.scratch_root.glob("rehearse_upgrade.*/odorehearse*") if path.is_dir()]
    assert len(cand_dirs) == 1
    for name in tracked:
        path = cand_dirs[0] / name
        assert path.stat().st_mode & 0o004, f"{name} is not world-readable"
    assert run.summary_path.stat().st_mode & 0o077 == 0
