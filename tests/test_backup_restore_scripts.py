from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_database.sh"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore_database.sh"


def _inherited_path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")


# ---------------------------------------------------------------------------
# Fake compose frontend
#
# One dispatch body, reused by every fake so behavior is configured entirely
# through env vars set per test rather than by writing bespoke scripts. Each
# invocation is logged as one "$*"-joined line (not "$@") so a single log
# entry maps to a single compose invocation, which is what the ordering/
# absence assertions below need.
# ---------------------------------------------------------------------------

_CORE_DISPATCH = r"""
log_line() {
    printf '%s\n' "$*" >> "${FAKE_LOG:?FAKE_LOG not set}"
}

cmd="${1-}"
[ "$#" -ge 1 ] && shift

case "$cmd" in
    version)
        log_line "version $*"
        exit 0
        ;;
    ps)
        log_line "ps $*"
        printf '%s\n' "${FAKE_APP_STATUS:-app  Exited (0) 3 minutes ago}"
        exit 0
        ;;
    run)
        log_line "run $*"
        case "$*" in
            *prepare-restore*) exit "${FAKE_PREPARE_RESTORE_EXIT:-0}" ;;
            *finalize-restore*) exit "${FAKE_FINALIZE_RESTORE_EXIT:-0}" ;;
            *) exit 99 ;;
        esac
        ;;
    exec)
        log_line "exec $*"
        is_runtime=0
        if [ "${1-}" = "-T" ]; then
            shift
        elif [ "${1-}" = "-i" ]; then
            shift
            is_runtime=1
        fi
        if [ "$is_runtime" -eq 1 ] && [ -n "${FAKE_MISSING_CONTAINER:-}" ] && [ "${1-}" = "$FAKE_MISSING_CONTAINER" ]; then
            exit 125
        fi
        [ "$#" -ge 1 ] && shift
        tool="${1-}"
        [ "$#" -ge 1 ] && shift
        if [ -n "${FAKE_TOOL_RAN:-}" ]; then
            : > "$FAKE_TOOL_RAN"
        fi
        case "$tool" in
            pg_dump)
                code="${FAKE_PG_DUMP_EXIT:-0}"
                if [ "$code" -ne 0 ]; then
                    exit "$code"
                fi
                if [ -n "${FAKE_ARCHIVE_FILE:-}" ]; then
                    cat -- "$FAKE_ARCHIVE_FILE"
                else
                    printf 'FAKE ARCHIVE BYTES\n'
                fi
                exit 0
                ;;
            pg_restore)
                is_list=0
                is_render=0
                for a in "$@"; do
                    if [ "$a" = "--list" ]; then
                        is_list=1
                    fi
                    if [ "$a" = "--file=-" ]; then
                        is_render=1
                    fi
                done
                if [ "$is_list" -eq 1 ]; then
                    if [ -n "${FAKE_STDIN_CAPTURE_LIST:-}" ]; then
                        cat > "$FAKE_STDIN_CAPTURE_LIST"
                    else
                        cat > /dev/null
                    fi
                    code="${FAKE_PG_RESTORE_LIST_EXIT:-0}"
                    if [ "$code" -ne 0 ]; then
                        exit "$code"
                    fi
                    printf '%s\n' "${FAKE_TOC_CONTENT:-1; 0 0 TABLE public trips mileage}"
                    exit 0
                elif [ "$is_render" -eq 1 ]; then
                    if [ -n "${FAKE_STDIN_CAPTURE_RENDER:-}" ]; then
                        cat > "$FAKE_STDIN_CAPTURE_RENDER"
                    else
                        cat > /dev/null
                    fi
                    if [ "${FAKE_PG_RESTORE_RENDER_EXIT:-0}" -ne 0 ]; then
                        exit "${FAKE_PG_RESTORE_RENDER_EXIT}"
                    fi
                    printf '%s\n' "${FAKE_RENDER_SQL:--- rendered archive SQL}"
                    exit 0
                else
                    if [ -n "${FAKE_STDIN_CAPTURE_RESTORE:-}" ]; then
                        cat > "$FAKE_STDIN_CAPTURE_RESTORE"
                    else
                        cat > /dev/null
                    fi
                    exit "${FAKE_PG_RESTORE_EXIT:-0}"
                fi
                ;;
            psql)
                is_file=0
                sql=""
                for a in "$@"; do
                    sql="$a"
                    if [ "$a" = "-f" ]; then
                        is_file=1
                    fi
                done
                if [ "$is_file" -eq 1 ]; then
                    if [ -n "${FAKE_STDIN_CAPTURE_PSQL:-}" ]; then
                        cat > "$FAKE_STDIN_CAPTURE_PSQL"
                    else
                        cat > /dev/null
                    fi
                    exit "${FAKE_PSQL_EXIT:-0}"
                fi
                case "$sql" in
                    *pg_roles*)
                        printf '%s\n' "${FAKE_BACKUP_PRIVILEGE:-full-instance}"
                        exit 0
                        ;;
                    *schema_migrations*)
                        code="${FAKE_SCHEMA_QUERY_EXIT:-0}"
                        if [ "$code" -ne 0 ]; then
                            exit "$code"
                        fi
                        printf '%s\n' "${FAKE_SCHEMA_VERSION:-0}"
                        exit 0
                        ;;
                    *pg_depend*)
                        printf '%s\n' "${FAKE_EMPTY_TARGET_RESULT:-}"
                        exit 0
                        ;;
                    "SHOW server_version")
                        code="${FAKE_SERVER_QUERY_EXIT:-0}"
                        if [ "$code" -ne 0 ]; then
                            exit "$code"
                        fi
                        printf '%s\n' "${FAKE_SERVER_VERSION:-16.4}"
                        exit 0
                        ;;
                    "SELECT postgis_lib_version()")
                        code="${FAKE_POSTGIS_QUERY_EXIT:-0}"
                        if [ "$code" -ne 0 ]; then
                            exit "$code"
                        fi
                        printf '%s\n' "${FAKE_POSTGIS_VERSION:-3.4.2}"
                        exit 0
                        ;;
                    *ANALYZE*)
                        exit "${FAKE_ANALYZE_EXIT:-0}"
                        ;;
                    *)
                        printf 'fake psql: unrecognized query: %s\n' "$sql" >&2
                        exit 1
                        ;;
                esac
                ;;
            pg_isready)
                exit "${FAKE_PG_ISREADY_EXIT:-0}"
                ;;
            *)
                printf 'fake compose: unrecognized tool: %s\n' "$tool" >&2
                exit 1
                ;;
        esac
        ;;
    *)
        printf 'fake compose: unrecognized subcommand: %s\n' "$cmd" >&2
        exit 1
        ;;
esac
""".strip("\n")


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content + "\n")
    path.chmod(0o755)
    return path


def install_single_word_fake(bin_dir: Path, name: str = "fake-compose") -> Path:
    """A one-word compose frontend, e.g. podman-compose or a COMPOSE_CMD override target."""
    return _write_executable(
        bin_dir / name,
        "#!/usr/bin/env bash\nset -u\n" + _CORE_DISPATCH,
    )


def install_docker_fake(bin_dir: Path) -> Path:
    """docker's "compose" is a subcommand, so shift it before the shared dispatch."""
    return _write_executable(
        bin_dir / "docker",
        '#!/usr/bin/env bash\nset -u\nif [ "${1-}" = "compose" ]; then shift; fi\n' + _CORE_DISPATCH,
    )


def install_podman_compose_fake(bin_dir: Path) -> Path:
    return install_single_word_fake(bin_dir, "podman-compose")


def install_always_fail_fake(bin_dir: Path, name: str) -> Path:
    """A decoy that errors loudly if ever invoked, to prove it was bypassed."""
    return _write_executable(
        bin_dir / name,
        f'#!/usr/bin/env bash\necho "error: {name} decoy should never be invoked: $*" >&2\nexit 99\n',
    )


def install_scripts(tmp_path: Path) -> tuple[Path, Path]:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    backup_path = _write_executable(scripts_dir / "backup_database.sh", BACKUP_SCRIPT.read_text())
    restore_path = _write_executable(scripts_dir / "restore_database.sh", RESTORE_SCRIPT.read_text())
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    return backup_path, restore_path


def base_env(
    bin_dir: Path,
    log_path: Path,
    compose_cmd: str | None = None,
    system_path: str | None = None,
    **overrides: str,
) -> dict:
    path = system_path if system_path is not None else _inherited_path()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{path}",
        "FAKE_LOG": str(log_path),
    }
    if compose_cmd is not None:
        env["COMPOSE_CMD"] = compose_cmd
    env.update(overrides)
    return env


def run_script(script: Path, args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def read_log(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text().splitlines() if line]


def exec_lines(log_path: Path) -> list[str]:
    return [line for line in read_log(log_path) if line.startswith("exec ")]


def mode_bits(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)[2:].zfill(3)


def make_archive_source(tmp_path: Path, content: bytes, name: str = "fake_pg_dump_output.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


FAKE_ARCHIVE_BYTES = bytes(range(256)) * 4  # binary, includes NUL, to prove -T fidelity


def write_archive_with_sidecar(dest_dir: Path, name: str, content: bytes) -> tuple[Path, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / name
    archive.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    sidecar = dest_dir / f"{name}.sha256"
    sidecar.write_text(f"{digest}  {name}\n")
    return archive, sidecar


def assert_successful_backup(archive: Path, expected_bytes: bytes) -> dict:
    sidecar = archive.with_name(archive.name + ".sha256")
    manifest = archive.with_name(archive.name + ".manifest")
    assert archive.is_file()
    assert sidecar.is_file()
    assert manifest.is_file()

    assert archive.read_bytes() == expected_bytes
    assert mode_bits(archive) == "600"
    assert mode_bits(sidecar) == "600"
    assert mode_bits(manifest) == "600"

    hash_field, name_field = sidecar.read_text().strip().split("  ", 1)
    assert hash_field == hashlib.sha256(expected_bytes).hexdigest()
    assert name_field == archive.name

    manifest_data = dict(
        line.split("=", 1) for line in manifest.read_text().splitlines() if "=" in line
    )
    assert set(manifest_data.keys()) == {
        "created_utc",
        "archive",
        "schema_version",
        "backup_scope",
        "postgres_version",
        "postgis_version",
        "source_ref",
    }
    assert manifest_data["archive"] == archive.name
    return manifest_data


# ---------------------------------------------------------------------------
# Compose autodetection / COMPOSE_CMD override
# ---------------------------------------------------------------------------


def test_backup_autodetects_docker_compose(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_docker_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(bin_dir, log_path, compose_cmd=None, FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    match = re.search(r"backups/mileage-\d{8}T\d{6}Z\.dump", result.stdout)
    assert match
    assert_successful_backup(tmp_path / match.group(0), FAKE_ARCHIVE_BYTES)

    log = read_log(log_path)
    assert any(line.startswith("version") for line in log)
    assert any("pg_dump" in line for line in log)


def test_backup_falls_back_to_podman_compose_when_docker_compose_unavailable(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_always_fail_fake(bin_dir, "docker")
    install_podman_compose_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(
        bin_dir, log_path, compose_cmd=None,
        FAKE_ARCHIVE_FILE=str(archive_src),
    )
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    match = re.search(r"backups/mileage-\d{8}T\d{6}Z\.dump", result.stdout)
    assert match
    assert_successful_backup(tmp_path / match.group(0), FAKE_ARCHIVE_BYTES)

    log = read_log(log_path)
    assert not any(line.startswith("version") for line in log)
    assert any("pg_dump" in line for line in log)


def test_backup_compose_cmd_override_bypasses_autodetection(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Decoys that would fail loudly if invoked, proving the override wins
    # even when both real command forms are also present on PATH.
    install_always_fail_fake(bin_dir, "docker")
    install_always_fail_fake(bin_dir, "podman-compose")
    override = install_single_word_fake(bin_dir, "fake-compose")
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(bin_dir, log_path, compose_cmd=str(override), FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    match = re.search(r"backups/mileage-\d{8}T\d{6}Z\.dump", result.stdout)
    assert match
    assert_successful_backup(tmp_path / match.group(0), FAKE_ARCHIVE_BYTES)


def test_restore_verify_only_uses_compose_cmd_override(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_always_fail_fake(bin_dir, "docker")
    install_always_fail_fake(bin_dir, "podman-compose")
    override = install_single_word_fake(bin_dir, "fake-compose")
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = base_env(bin_dir, log_path, compose_cmd=str(override))
    result = run_script(restore_script, ["--verify-only", str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Direct container runtime / CONTAINER_RUNTIME override
# ---------------------------------------------------------------------------


def test_backup_container_autodetects_podman_without_compose_frontend(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    (tmp_path / "compose.yaml").unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_single_word_fake(bin_dir, "podman")
    install_always_fail_fake(bin_dir, "docker")
    install_always_fail_fake(bin_dir, "podman-compose")
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)
    archive = tmp_path / "backups" / "container-podman.dump"

    env = base_env(
        bin_dir,
        log_path,
        FAKE_ARCHIVE_FILE=str(archive_src),
        FAKE_SCHEMA_VERSION="21",
        FAKE_SERVER_VERSION="16.4.1",
        FAKE_POSTGIS_VERSION="3.4.3",
    )
    result = run_script(
        backup_script,
        ["--container", "db", "--output", str(archive)],
        tmp_path,
        env,
    )

    assert result.returncode == 0, result.stderr
    manifest_data = assert_successful_backup(archive, FAKE_ARCHIVE_BYTES)
    assert manifest_data["schema_version"] == "21"
    assert manifest_data["postgres_version"] == "16.4.1"
    assert manifest_data["postgis_version"] == "3.4.3"

    log = read_log(log_path)
    assert not any(line.startswith("version") for line in log)
    execs = exec_lines(log_path)
    assert execs
    for line in execs:
        assert line.startswith("exec -i db ")
        assert " -T " not in f" {line} "


def test_backup_container_uses_docker_runtime_override(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    (tmp_path / "compose.yaml").unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_docker_fake(bin_dir)
    install_always_fail_fake(bin_dir, "podman")
    install_always_fail_fake(bin_dir, "podman-compose")
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)
    archive = tmp_path / "backups" / "container-docker.dump"

    env = base_env(
        bin_dir,
        log_path,
        FAKE_ARCHIVE_FILE=str(archive_src),
        CONTAINER_RUNTIME="docker",
    )
    result = run_script(
        backup_script,
        ["--container", "db", "--output", str(archive)],
        tmp_path,
        env,
    )

    assert result.returncode == 0, result.stderr
    assert_successful_backup(archive, FAKE_ARCHIVE_BYTES)
    log = read_log(log_path)
    assert not any(line.startswith("version") for line in log)
    execs = exec_lines(log_path)
    assert execs
    assert all(line.startswith("exec -i db ") for line in execs)


@pytest.mark.parametrize("preexisting_member", ["archive", "sidecar", "manifest"])
def test_backup_container_refuses_to_overwrite_existing_member(tmp_path, preexisting_member):
    backup_script, _ = install_scripts(tmp_path)
    (tmp_path / "compose.yaml").unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_single_word_fake(bin_dir, "podman")
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    out_dir = tmp_path / "existing_backups"
    out_dir.mkdir()
    archive = out_dir / "mileage-existing.dump"
    sidecar = archive.with_name(archive.name + ".sha256")
    manifest = archive.with_name(archive.name + ".manifest")
    members = {"archive": archive, "sidecar": sidecar, "manifest": manifest}
    members[preexisting_member].write_text("PREEXISTING")

    env = base_env(bin_dir, log_path, FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(
        backup_script,
        ["--container", "db", "--output", str(archive)],
        tmp_path,
        env,
    )

    assert result.returncode != 0
    assert members[preexisting_member].read_text() == "PREEXISTING"
    for name, path in members.items():
        if name != preexisting_member:
            assert not path.exists()
    assert read_log(log_path) == []


@pytest.mark.parametrize(
    ("failure_var", "failure_code"),
    [
        ("FAKE_PG_DUMP_EXIT", "3"),
        ("FAKE_PG_RESTORE_LIST_EXIT", "5"),
        ("FAKE_SERVER_QUERY_EXIT", "7"),
        ("FAKE_POSTGIS_QUERY_EXIT", "8"),
    ],
)
def test_backup_container_failure_cleans_up_all_artifacts(
    tmp_path, failure_var, failure_code
):
    backup_script, _ = install_scripts(tmp_path)
    (tmp_path / "compose.yaml").unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_single_word_fake(bin_dir, "podman")
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)
    archive = tmp_path / "backups" / "container-failure.dump"

    env = base_env(
        bin_dir,
        log_path,
        FAKE_ARCHIVE_FILE=str(archive_src),
        **{failure_var: failure_code},
    )
    result = run_script(
        backup_script,
        ["--container", "db", "--output", str(archive)],
        tmp_path,
        env,
    )

    assert result.returncode != 0
    _assert_no_backup_artifacts(archive.parent)


def test_backup_container_missing_container_fails_before_tool_and_cleans_up(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    (tmp_path / "compose.yaml").unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_single_word_fake(bin_dir, "podman")
    log_path = tmp_path / "fake.log"
    tool_marker = tmp_path / "tool-ran"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)
    archive = tmp_path / "backups" / "missing-container.dump"

    env = base_env(
        bin_dir,
        log_path,
        FAKE_ARCHIVE_FILE=str(archive_src),
        FAKE_MISSING_CONTAINER="missing-db",
        FAKE_TOOL_RAN=str(tool_marker),
    )
    result = run_script(
        backup_script,
        ["--container", "missing-db", "--output", str(archive)],
        tmp_path,
        env,
    )

    assert result.returncode != 0
    assert not tool_marker.exists()
    log = read_log(log_path)
    assert len(log) == 1
    assert log[0].startswith("exec -i missing-db psql -X ")
    _assert_no_backup_artifacts(archive.parent)


def test_backup_container_missing_runtime_fails_before_creating_output(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    (tmp_path / "compose.yaml").unlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to("/bin/bash")
    log_path = tmp_path / "fake.log"
    archive = tmp_path / "backups" / "missing-runtime.dump"

    env = base_env(bin_dir, log_path, system_path="")
    result = run_script(
        backup_script,
        ["--container", "db", "--output", str(archive)],
        tmp_path,
        env,
    )

    assert result.returncode != 0
    assert "neither 'podman' nor 'docker' found" in result.stderr
    assert not archive.parent.exists()


# ---------------------------------------------------------------------------
# Default naming, permissions
# ---------------------------------------------------------------------------


def test_backup_default_naming_and_directory_permissions(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(bin_dir, log_path, str(fake), FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    backups_dir = tmp_path / "backups"
    assert mode_bits(backups_dir) == "700"

    matches = list(backups_dir.glob("mileage-*.dump"))
    assert len(matches) == 1
    assert re.fullmatch(r"mileage-\d{8}T\d{6}Z\.dump", matches[0].name)
    assert_successful_backup(matches[0], FAKE_ARCHIVE_BYTES)


def test_backup_bare_filename_output_skips_directory_chmod(tmp_path):
    # Deliberate deviation: chmod 700 on "." would lock the whole install
    # checkout, not just the backup, so a bare --output filename skips it.
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    mode_before = mode_bits(tmp_path)
    env = base_env(bin_dir, log_path, str(fake), FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, ["--output", "bare.dump"], tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert mode_bits(tmp_path) == mode_before
    assert_successful_backup(tmp_path / "bare.dump", FAKE_ARCHIVE_BYTES)


# ---------------------------------------------------------------------------
# Overwrite refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preexisting_member", ["archive", "sidecar", "manifest"])
def test_backup_refuses_to_overwrite_existing_member(tmp_path, preexisting_member):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    out_dir = tmp_path / "existing_backups"
    out_dir.mkdir()
    archive = out_dir / "mileage-20260101T000000Z.dump"
    sidecar = archive.with_name(archive.name + ".sha256")
    manifest = archive.with_name(archive.name + ".manifest")
    members = {"archive": archive, "sidecar": sidecar, "manifest": manifest}
    members[preexisting_member].write_text("PREEXISTING")

    env = base_env(bin_dir, log_path, str(fake), FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, ["--output", str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert members[preexisting_member].read_text() == "PREEXISTING"
    for name, path in members.items():
        if name != preexisting_member:
            assert not path.exists()
    assert read_log(log_path) == []  # refused before any compose invocation


# ---------------------------------------------------------------------------
# pg_dump / pg_restore --list failure, atomic publication
# ---------------------------------------------------------------------------


def _assert_no_backup_artifacts(out_dir: Path):
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


def test_backup_pg_dump_failure_publishes_nothing(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"

    env = base_env(bin_dir, log_path, str(fake), FAKE_PG_DUMP_EXIT="3")
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode != 0
    _assert_no_backup_artifacts(tmp_path / "backups")


def test_backup_pg_dump_excludes_postgis_reference_schemas(tmp_path):
    # These three schemas are provisioned by the postgis/postgis image's own
    # init scripts on every fresh database, so pg_dump's bare CREATE SCHEMA
    # entries for them collide on restore; excluding them at dump time is
    # the fix. Pinned as a denylist of exactly these three, not a
    # public-only allowlist, so a future application schema stays included.
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(bin_dir, log_path, str(fake), FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    execs = exec_lines(log_path)
    dump_calls = [line for line in execs if "pg_dump" in line]
    assert len(dump_calls) == 1
    assert dump_calls[0] == (
        "exec -T db pg_dump -U mileage -d mileage --format=custom "
        "--exclude-schema=tiger --exclude-schema=tiger_data --exclude-schema=topology"
    )


def test_backup_pg_restore_list_validation_failure_publishes_nothing(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(
        bin_dir, log_path, str(fake),
        FAKE_ARCHIVE_FILE=str(archive_src), FAKE_PG_RESTORE_LIST_EXIT="5",
    )
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode != 0
    backups_dir = tmp_path / "backups"
    # Directory may exist (created before the dump ran) but must contain no
    # archive, sidecar, manifest, or leftover temp file.
    if backups_dir.exists():
        assert list(backups_dir.iterdir()) == []
    matches = list(tmp_path.glob("**/mileage-*.dump"))
    assert matches == []


# ---------------------------------------------------------------------------
# Success content: sidecar, manifest, byte-exact archive
# ---------------------------------------------------------------------------


def test_backup_success_content_is_exact(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(
        bin_dir, log_path, str(fake),
        FAKE_ARCHIVE_FILE=str(archive_src),
        FAKE_SCHEMA_VERSION="9",
        FAKE_SERVER_VERSION="16.4",
        FAKE_POSTGIS_VERSION="3.4.2",
    )
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    match = re.search(r"backups/mileage-\d{8}T\d{6}Z\.dump", result.stdout)
    assert match
    manifest_data = assert_successful_backup(tmp_path / match.group(0), FAKE_ARCHIVE_BYTES)

    assert manifest_data["schema_version"] == "9"
    assert manifest_data["postgres_version"] == "16.4"
    assert manifest_data["postgis_version"] == "3.4.2"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest_data["created_utc"])

    manifest_text = (tmp_path / (match.group(0) + ".manifest")).read_text()
    assert "PASSWORD" not in manifest_text.upper()


# ---------------------------------------------------------------------------
# Missing schema_migrations records schema_version=0
# ---------------------------------------------------------------------------


def test_backup_missing_schema_migrations_records_zero(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(
        bin_dir, log_path, str(fake),
        FAKE_ARCHIVE_FILE=str(archive_src), FAKE_SCHEMA_QUERY_EXIT="1",
    )
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    match = re.search(r"backups/mileage-\d{8}T\d{6}Z\.dump", result.stdout)
    assert match
    manifest_data = assert_successful_backup(tmp_path / match.group(0), FAKE_ARCHIVE_BYTES)
    assert manifest_data["schema_version"] == "0"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_backup_never_emits_secrets(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=env-file-secret-xyz\nINGEST_PASSWORD=other-secret\n")

    env = base_env(
        bin_dir, log_path, str(fake),
        FAKE_ARCHIVE_FILE=str(archive_src),
        POSTGRES_PASSWORD="super-secret-password-123",
        FAKE_SECRET_VALUE="another-fake-secret-456",
    )
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "super-secret-password-123" not in combined
    assert "another-fake-secret-456" not in combined
    assert "env-file-secret-xyz" not in combined
    assert "other-secret" not in combined


# ---------------------------------------------------------------------------
# Every exec invocation uses -T
# ---------------------------------------------------------------------------


def test_backup_every_exec_invocation_uses_dash_T(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = install_single_word_fake(bin_dir)
    log_path = tmp_path / "fake.log"
    archive_src = make_archive_source(tmp_path, FAKE_ARCHIVE_BYTES)

    env = base_env(bin_dir, log_path, str(fake), FAKE_ARCHIVE_FILE=str(archive_src))
    result = run_script(backup_script, [], tmp_path, env)

    assert result.returncode == 0, result.stderr
    execs = exec_lines(log_path)
    assert execs
    for line in execs:
        assert " -T " in f" {line} "


# ===========================================================================
# Restore script
# ===========================================================================


def restore_env(bin_dir: Path, log_path: Path, **overrides: str) -> dict:
    fake = install_single_word_fake(bin_dir)
    return base_env(bin_dir, log_path, str(fake), **overrides)


def test_backup_refuses_account_scoped_identity_before_dump(tmp_path):
    backup_script, _ = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive = tmp_path / "backups" / "restricted.dump"
    env = restore_env(bin_dir, log_path, FAKE_BACKUP_PRIVILEGE="refused")

    result = run_script(backup_script, ["--output", str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "full-instance" in result.stderr
    assert not any("pg_dump" in line for line in read_log(log_path))
    _assert_no_backup_artifacts(archive.parent)


OWNED_TOC = "1; 0 0 SCHEMA - odograph_service mileage\n2; 0 0 TABLE public trips mileage"


def test_restore_owned_archive_rebuilds_roles_around_transaction(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "owned.dump", FAKE_ARCHIVE_BYTES)
    env = restore_env(bin_dir, log_path, FAKE_TOC_CONTENT=OWNED_TOC, FAKE_SCHEMA_VERSION="26")

    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    log = read_log(log_path)
    ordered = [
        "pg_restore --file=-",
        "run --rm --no-deps -T app python -m app.application_roles prepare-restore",
        "psql -X --single-transaction",
        "run --rm --no-deps -T app python -m app.application_roles finalize-restore",
        "ANALYZE;",
    ]
    positions = [next(i for i, line in enumerate(log) if marker in line) for marker in ordered]
    assert positions == sorted(positions)
    assert "Restored schema version: 26" in result.stdout


@pytest.mark.parametrize("stage", ["prepare", "finalize"])
def test_restore_owned_archive_security_failure_stops_before_next_stage(tmp_path, stage):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "owned.dump", FAKE_ARCHIVE_BYTES)
    env = restore_env(
        bin_dir, log_path, FAKE_TOC_CONTENT=OWNED_TOC,
        **{f"FAKE_{stage.upper()}_RESTORE_EXIT": "7"},
    )

    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode != 0
    log = read_log(log_path)
    assert not any("ANALYZE" in line for line in log)
    if stage == "prepare":
        assert "no archive SQL was applied" in result.stderr
        assert not any("--single-transaction" in line for line in log)
        assert not any("finalize-restore" in line for line in log)
    else:
        assert "data restored" in result.stderr
        assert "Keep the app stopped" in result.stderr
        assert any("--single-transaction" in line for line in log)


def test_restore_owned_archive_verify_only_never_prepares_roles(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "owned.dump", FAKE_ARCHIVE_BYTES)
    env = restore_env(bin_dir, log_path, FAKE_TOC_CONTENT=OWNED_TOC)

    result = run_script(restore_script, ["--verify-only", str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("run ") for line in read_log(log_path))


# ---------------------------------------------------------------------------
# --verify-only
# ---------------------------------------------------------------------------


def test_restore_verify_only_valid_archive_passes_without_dash_d(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path)
    result = run_script(restore_script, ["--verify-only", str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    execs = exec_lines(log_path)
    assert execs
    for line in execs:
        tokens = line.split()
        assert "-d" not in tokens
    assert any("pg_restore" in line and "--list" in line for line in execs)


def test_restore_verify_only_tampered_archive_fails_before_any_compose_call(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)
    # Flip one byte after the sidecar was computed.
    data = bytearray(archive.read_bytes())
    data[0] ^= 0xFF
    archive.write_bytes(bytes(data))

    env = restore_env(bin_dir, log_path)
    result = run_script(restore_script, ["--verify-only", str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "checksum" in (result.stdout + result.stderr).lower()
    assert read_log(log_path) == []


def test_restore_verify_only_missing_sidecar_fails_clearly(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive = tmp_path / "d" / "mileage-x.dump"
    archive.parent.mkdir()
    archive.write_bytes(FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path)
    result = run_script(restore_script, ["--verify-only", str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "sha256" in result.stderr or "sidecar" in result.stderr
    assert read_log(log_path) == []


# ---------------------------------------------------------------------------
# --skip-checksum semantics
# ---------------------------------------------------------------------------


def test_restore_skip_checksum_with_verify_only_is_rejected(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path)
    result = run_script(restore_script, ["--skip-checksum", "--verify-only", str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "not valid" in result.stderr
    assert read_log(log_path) == []


def test_restore_skip_checksum_alone_skips_checksum_but_validates_structure(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive = tmp_path / "d" / "mileage-x.dump"
    archive.parent.mkdir()
    archive.write_bytes(FAKE_ARCHIVE_BYTES)
    # Deliberately no sidecar: if checksum were checked, this would fail
    # with a "sidecar not found" error instead of proceeding.

    env = restore_env(bin_dir, log_path, FAKE_SCHEMA_VERSION="4")
    result = run_script(restore_script, ["--skip-checksum", str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "skip-checksum" in result.stderr
    execs = exec_lines(log_path)
    assert any("pg_restore" in line and "--list" in line for line in execs)
    assert any(
        "psql" in line and "--single-transaction" in line
        for line in execs
    )


# ---------------------------------------------------------------------------
# Corrupt archive refused before any pg_restore -d call
# ---------------------------------------------------------------------------


def test_restore_corrupt_archive_refused_before_pg_restore_dash_d(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path, FAKE_PG_RESTORE_LIST_EXIT="7")
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode != 0
    execs = exec_lines(log_path)
    assert any("pg_restore" in line and "--list" in line for line in execs)
    assert not any("pg_restore" in line and "-d" in line.split() for line in execs)


# ---------------------------------------------------------------------------
# Running-app refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_status", ["app   Up 5 minutes", "app  running (healthy)"])
def test_restore_refuses_when_app_is_running(tmp_path, app_status):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path, FAKE_APP_STATUS=app_status)
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "app service appears to be running" in result.stderr
    execs = exec_lines(log_path)
    pg_isready_calls = [line for line in execs if "pg_isready" in line]
    assert pg_isready_calls == [
        "exec -T db pg_isready -h 127.0.0.1 -p 5432 -U mileage -d mileage"
    ]
    assert any(line.startswith("ps ") for line in read_log(log_path))
    assert not any("psql" in line for line in execs)
    assert not any("pg_restore" in line for line in execs)


# ---------------------------------------------------------------------------
# Non-empty-target refusal / extension exemption
# ---------------------------------------------------------------------------


def test_restore_refuses_non_empty_target(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path, FAKE_EMPTY_TARGET_RESULT="schema_migrations")
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "schema_migrations" in result.stderr
    assert "fresh" in result.stderr.lower()
    execs = exec_lines(log_path)
    assert not any("pg_restore" in line and "--single-transaction" in line for line in execs)


def test_restore_proceeds_when_target_is_empty(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path)  # FAKE_EMPTY_TARGET_RESULT unset => empty
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    execs = exec_lines(log_path)
    assert any(
        "psql" in line and "--single-transaction" in line
        for line in execs
    )


def test_restore_empty_target_sql_exempts_extension_owned_relations():
    text = RESTORE_SCRIPT.read_text()
    assert "deptype = 'e'" in text


# ---------------------------------------------------------------------------
# Successful restore: exact flags, byte-exact stdin, ANALYZE,
# reported schema version, manifest mismatch warning without failing
# ---------------------------------------------------------------------------


def test_restore_success_exact_flags_and_manifest_mismatch_warning(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)
    manifest = archive.with_name(archive.name + ".manifest")
    manifest.write_text("schema_version=8\narchive=mileage-x.dump\n")
    render_capture = tmp_path / "render_stdin_capture.bin"
    psql_capture = tmp_path / "psql_stdin_capture.sql"

    env = restore_env(
        bin_dir, log_path,
        FAKE_SCHEMA_VERSION="7",
        FAKE_STDIN_CAPTURE_RENDER=str(render_capture),
        FAKE_STDIN_CAPTURE_PSQL=str(psql_capture),
    )
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr

    execs = exec_lines(log_path)
    render_calls = [line for line in execs if "pg_restore" in line and "--file=-" in line]
    assert len(render_calls) == 1
    assert render_calls[0] == (
        "exec -T db pg_restore --file=- --no-owner --no-privileges"
    )

    assert render_capture.read_bytes() == FAKE_ARCHIVE_BYTES
    assert psql_capture.read_text() == "-- rendered archive SQL\n"

    transaction_calls = [
        line for line in execs
        if "psql" in line and "--single-transaction" in line
    ]
    assert transaction_calls == [
        (
            "exec -T db psql -X --single-transaction -v ON_ERROR_STOP=1 "
            "-U mileage -d mileage -f -"
        )
    ]

    analyze_calls = [line for line in execs if "ANALYZE" in line]
    assert len(analyze_calls) == 1
    assert "-v ON_ERROR_STOP=1" in analyze_calls[0]
    assert execs.index(analyze_calls[0]) > execs.index(transaction_calls[0])

    assert "Restored schema version: 7" in result.stdout
    assert "manifest schema_version (8) does not match the restored database's schema_version (7)" in result.stderr


def test_restore_missing_schema_migrations_after_restore_is_warning_not_failure(tmp_path):
    # Deliberate deviation: the restore already committed by this point, so
    # a missing ledger is reported, not treated as a rollback-worthy failure.
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path, FAKE_SCHEMA_QUERY_EXIT="1")
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert "WARNING: schema_migrations is missing after restore" in result.stderr
    assert "Restored schema version: 0" in result.stdout


def test_restore_render_failure_never_executes_sql(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)
    temp_dir = tmp_path / "restore-temp"
    temp_dir.mkdir()

    env = restore_env(
        bin_dir,
        log_path,
        FAKE_PG_RESTORE_RENDER_EXIT="7",
        TMPDIR=str(temp_dir),
    )
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "no SQL was sent" in result.stderr
    execs = exec_lines(log_path)
    assert any("pg_restore" in line and "--list" in line for line in execs)
    assert any("pg_restore" in line and "--file=-" in line for line in execs)
    assert not any("psql" in line and "--single-transaction" in line for line in execs)
    assert list(temp_dir.iterdir()) == []


def test_restore_transaction_failure_reports_rollback(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path, FAKE_PSQL_EXIT="7")
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode != 0
    assert "rolled back" in result.stderr
    execs = exec_lines(log_path)
    assert any("psql" in line and "--single-transaction" in line for line in execs)
    assert not any("ANALYZE" in line for line in execs)
    assert not any("schema_migrations" in line for line in execs)


def test_restore_bootstraps_only_missing_legacy_extension_schemas(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)
    psql_capture = tmp_path / "psql_stdin_capture.sql"

    toc = "\n".join(
        [
            "10; 0 0 EXTENSION - postgis_tiger_geocoder postgres",
            "11; 0 0 EXTENSION - postgis_topology postgres",
            "12; 2615 0 SCHEMA - tiger postgres",
        ]
    )
    env = restore_env(
        bin_dir,
        log_path,
        FAKE_TOC_CONTENT=toc,
        FAKE_STDIN_CAPTURE_PSQL=str(psql_capture),
    )
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    sql = psql_capture.read_text()
    assert "CREATE SCHEMA IF NOT EXISTS tiger;" not in sql
    assert "CREATE SCHEMA IF NOT EXISTS tiger_data;" in sql
    assert "CREATE SCHEMA IF NOT EXISTS topology;" in sql


def test_restore_does_not_bootstrap_schemas_present_in_archive(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)
    psql_capture = tmp_path / "psql_stdin_capture.sql"

    toc = "\n".join(
        [
            "10; 0 0 EXTENSION - postgis_tiger_geocoder postgres",
            "11; 0 0 EXTENSION - postgis_topology postgres",
            "12; 2615 0 SCHEMA - tiger postgres",
            "13; 2615 0 SCHEMA - tiger_data postgres",
            "14; 2615 0 SCHEMA - topology postgres",
        ]
    )
    env = restore_env(
        bin_dir,
        log_path,
        FAKE_TOC_CONTENT=toc,
        FAKE_STDIN_CAPTURE_PSQL=str(psql_capture),
    )
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    sql = psql_capture.read_text()
    assert "CREATE SCHEMA IF NOT EXISTS" not in sql


def test_restore_native_archive_without_legacy_extensions_has_no_bootstrap(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)
    psql_capture = tmp_path / "psql_stdin_capture.sql"

    env = restore_env(
        bin_dir,
        log_path,
        FAKE_TOC_CONTENT="10; 0 0 TABLE public trips mileage",
        FAKE_STDIN_CAPTURE_PSQL=str(psql_capture),
    )
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    sql = psql_capture.read_text()
    assert "CREATE SCHEMA IF NOT EXISTS" not in sql
    assert "-- rendered archive SQL" in sql


def test_restore_every_exec_invocation_uses_dash_T(tmp_path):
    _, restore_script = install_scripts(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "fake.log"
    archive, _ = write_archive_with_sidecar(tmp_path / "d", "mileage-x.dump", FAKE_ARCHIVE_BYTES)

    env = restore_env(bin_dir, log_path)
    result = run_script(restore_script, [str(archive)], tmp_path, env)

    assert result.returncode == 0, result.stderr
    execs = exec_lines(log_path)
    assert execs
    for line in execs:
        assert " -T " in f" {line} "


# ---------------------------------------------------------------------------
# Static safety: no destructive tokens in either script
# ---------------------------------------------------------------------------


FORBIDDEN_TOKENS = ["--clean", "--create", "dropdb", "createdb", "down -v", "volume rm", "rm -rf"]


@pytest.mark.parametrize("script", [BACKUP_SCRIPT, RESTORE_SCRIPT])
def test_script_contains_no_destructive_tokens(script):
    text = script.read_text()
    for token in FORBIDDEN_TOKENS:
        assert token not in text, f"forbidden token {token!r} found in {script}"
