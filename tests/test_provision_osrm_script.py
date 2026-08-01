from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVISION_SCRIPT = REPO_ROOT / "scripts" / "provision_osrm.sh"

# Resolved once against this process's own PATH, not looked up per
# invocation: test_neither_frontend_available_is_rejected deliberately gives
# the script's own environment no directories at all, so a bare "bash" would
# be unresolvable there even though the interpreter itself has nothing to do
# with the frontends the script is looking for.
BASH = shutil.which("bash") or "bash"


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content + "\n")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Fake compose frontend. Both `docker compose` and `podman-compose` are
# reached through the exact same argument shape from the script
# (`--profile osrm run --rm --no-deps ...`), so one dispatch body serves
# both fakes; only the wrapper that shifts off a leading "compose" differs.
# Each invocation is logged as one "$*"-joined line so assertions can check
# exact call count and ordering.
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
    --profile)
        profile="${1-}"; [ "$#" -ge 1 ] && shift
        sub="${1-}"; [ "$#" -ge 1 ] && shift
        log_line "--profile $profile $sub $*"
        if [ "$sub" != "run" ]; then
            echo "unexpected compose subcommand: $sub $*" >&2
            exit 91
        fi
        full="$*"
        case "$full" in
            *"df -Pk /data"*)
                printf '%s\n' "${FAKE_DF_HEADER:-Filesystem     1024-blocks    Used Available Capacity Mounted on}"
                printf '%s\n' "${FAKE_DF_LINE:-overlay          102400000 1000000 100000000        1% /data}"
                exit "${FAKE_DF_EXIT:-0}"
                ;;
            *"osrm-extract"*)
                exit "${FAKE_EXTRACT_EXIT:-0}"
                ;;
            *"osrm-partition"*)
                exit "${FAKE_PARTITION_EXIT:-0}"
                ;;
            *"osrm-customize"*)
                exit "${FAKE_CUSTOMIZE_EXIT:-0}"
                ;;
            *"sh -c"*)
                exit "${FAKE_PREPARE_EXIT:-0}"
                ;;
            *)
                exit 0
                ;;
        esac
        ;;
    *)
        echo "unexpected compose invocation: $cmd $*" >&2
        exit 92
        ;;
esac
""".strip()


def install_docker_fake(bin_dir: Path) -> Path:
    return _write_executable(
        bin_dir / "docker",
        '#!/usr/bin/env bash\nset -u\nif [ "${1-}" = "compose" ]; then shift; fi\n' + _CORE_DISPATCH,
    )


def install_podman_compose_fake(bin_dir: Path) -> Path:
    return _write_executable(bin_dir / "podman-compose", "#!/usr/bin/env bash\nset -u\n" + _CORE_DISPATCH)


def install_curl_fake(bin_dir: Path) -> Path:
    return _write_executable(
        bin_dir / "curl",
        r"""#!/usr/bin/env bash
set -u
log_line() {
    printf '%s\n' "curl $*" >> "${FAKE_LOG:?FAKE_LOG not set}"
}
log_line "$*"
outfile=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-o" ]; then outfile="$arg"; fi
    prev="$arg"
done
code="${FAKE_CURL_EXIT:-0}"
if [ "$code" -eq 0 ]; then
    if [ -n "$outfile" ]; then
        printf '%s' "${FAKE_CURL_BODY:-fake-pbf-bytes}" > "$outfile"
    fi
    exit 0
fi
exit "$code"
""",
    )


def install_low_disk_df_fake(bin_dir: Path) -> Path:
    """Shadows the real `df` for the host-side preflight check only."""
    return _write_executable(
        bin_dir / "df",
        r"""#!/usr/bin/env bash
printf '%s\n' 'Filesystem     1024-blocks   Used Available Capacity Mounted on'
printf '%s\n' '/dev/disk1      100000000 99900000       100        1% /'
""",
    )


def _install_script(tmp_path: Path) -> Path:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = _write_executable(scripts_dir / "provision_osrm.sh", PROVISION_SCRIPT.read_text())
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    return script


def _inherited_path() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")


def base_env(bin_dir: Path, log_path: Path, compose_cmd: str | None = None, **overrides: str) -> dict:
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{_inherited_path()}",
        "FAKE_LOG": str(log_path),
    }
    if compose_cmd is not None:
        env["COMPOSE_CMD"] = compose_cmd
    env.update(overrides)
    return env


def run_script(script: Path, args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(script), *args],
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


FLORIDA_URL = "https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf"


def test_help_documents_usage_and_reprovisioning_warning(tmp_path):
    script = _write_executable(tmp_path / "provision_osrm.sh", PROVISION_SCRIPT.read_text())

    result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, timeout=10)

    assert result.returncode == 0
    assert "scripts/provision_osrm.sh <geofabrik-extract-url>" in result.stderr
    assert "REPLACES the current contents" in result.stderr
    assert "OSRM_DATASET" in result.stderr


@pytest.mark.parametrize("args", [[], ["a", "b"]])
def test_wrong_argument_count_is_rejected(tmp_path, args):
    script = _write_executable(tmp_path / "provision_osrm.sh", PROVISION_SCRIPT.read_text())

    result = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, timeout=10)

    assert result.returncode != 0
    assert "expected exactly one" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "ftp://download.geofabrik.de/florida-latest.osm.pbf",
        "https://download.geofabrik.de/florida-latest.zip",
        "https://download.geofabrik.de/florida-latest.osm.bz2",
    ],
)
def test_bad_url_is_rejected_before_any_network_or_compose_call(tmp_path, url):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(script, [url], tmp_path, base_env(bin_dir, log_path, compose_cmd="docker compose"))

    assert result.returncode != 0
    assert "bad URL" in result.stderr
    assert read_log(log_path) == []


def test_missing_compose_yaml_is_rejected(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = _write_executable(scripts_dir / "provision_osrm.sh", PROVISION_SCRIPT.read_text())
    # Deliberately no compose.yaml written at tmp_path.

    result = subprocess.run(
        ["bash", str(script), FLORIDA_URL], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )

    assert result.returncode != 0
    assert "compose.yaml not found" in result.stderr


def test_neither_frontend_available_is_rejected(tmp_path):
    script = _install_script(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()  # empty: neither docker nor podman-compose present

    # This test asserts the no-frontend-found path, so PATH must contain
    # nothing but the empty bin_dir -- any real directory may carry a docker
    # or podman-compose (CI runners ship Docker even in base system paths),
    # which would pass detection and send the script on to a real download.
    # Nothing the script runs before detection (arg count, URL case, the
    # compose.yaml check) needs a PATH lookup, so this is safe.
    result = run_script(script, [FLORIDA_URL], tmp_path, {"PATH": str(bin_dir), "FAKE_LOG": str(tmp_path / "log")})

    assert result.returncode != 0
    assert "neither 'docker compose' nor 'podman-compose'" in result.stderr


def test_compose_cmd_override_bypasses_autodetection(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_podman_compose_fake(bin_dir)

    result = run_script(
        script, [FLORIDA_URL], tmp_path, base_env(bin_dir, log_path, compose_cmd="podman-compose")
    )

    assert result.returncode == 0, result.stderr
    assert any(line.startswith("--profile osrm run") for line in read_log(log_path))


def test_docker_preferred_over_podman_compose_when_both_present(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)
    install_podman_compose_fake(bin_dir)

    result = run_script(script, [FLORIDA_URL], tmp_path, base_env(bin_dir, log_path))

    assert result.returncode == 0, result.stderr
    assert "OSRM_DATASET=florida-latest.osrm" in result.stdout


def test_successful_run_prints_dataset_value_and_issues_expected_command_shape(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(script, [FLORIDA_URL], tmp_path, base_env(bin_dir, log_path, compose_cmd="docker compose"))

    assert result.returncode == 0, result.stderr
    assert "OSRM_DATASET=florida-latest.osrm" in result.stdout
    assert "OSRM_URL=http://osrm:5000" in result.stdout
    assert "docker compose --profile osrm up -d osrm" in result.stdout
    assert "replaces the current contents of the 'osrmdata' volume" in result.stderr

    calls = [line for line in read_log(log_path) if line.startswith("--profile osrm run")]
    assert len(calls) == 4
    assert all("--rm" in c and "--no-deps" in c for c in calls)
    assert "-v" in calls[0] and "/download:ro" in calls[0] and "sh -c" in calls[0]
    assert "florida-latest.osm.pbf" in calls[0]
    assert "osrm-extract" in calls[1] and "/opt/car.lua" in calls[1] and "florida-latest.osm.pbf" in calls[1]
    assert "osrm-partition" in calls[2] and "florida-latest.osrm" in calls[2]
    assert "osrm-customize" in calls[3] and "florida-latest.osrm" in calls[3]


def test_dataset_name_derived_from_nested_geofabrik_path(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    url = "https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf"
    result = run_script(script, [url], tmp_path, base_env(bin_dir, log_path, compose_cmd="docker compose"))

    assert result.returncode == 0, result.stderr
    assert "OSRM_DATASET=washington-latest.osrm" in result.stdout


@pytest.mark.parametrize(
    ("curl_exit", "message"),
    [
        ("6", "could not resolve or connect"),
        ("7", "could not resolve or connect"),
        ("22", "the server returned an HTTP error status"),
        ("23", "usually means the disk ran out of space"),
        ("28", "download timed out"),
        ("99", "download failed (curl exit 99)"),
    ],
)
def test_download_failures_are_diagnosed_clearly(tmp_path, curl_exit, message):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(
        script,
        [FLORIDA_URL],
        tmp_path,
        base_env(bin_dir, log_path, compose_cmd="docker compose", FAKE_CURL_EXIT=curl_exit),
    )

    assert result.returncode != 0
    assert message in result.stderr
    # A download failure must never reach any compose invocation.
    assert not any(line.startswith("--profile osrm run") for line in read_log(log_path))


def test_insufficient_disk_is_caught_before_any_download(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)
    install_low_disk_df_fake(bin_dir)

    result = run_script(script, [FLORIDA_URL], tmp_path, base_env(bin_dir, log_path, compose_cmd="docker compose"))

    assert result.returncode != 0
    assert "insufficient disk space to stage the download" in result.stderr
    assert read_log(log_path) == []


def test_extract_oom_exit_code_is_diagnosed_and_stops_the_pipeline(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(
        script,
        [FLORIDA_URL],
        tmp_path,
        base_env(bin_dir, log_path, compose_cmd="docker compose", FAKE_EXTRACT_EXIT="137"),
    )

    assert result.returncode != 0
    assert "osrm-extract was killed (exit 137)" in result.stderr
    assert "Linux OOM killer" in result.stderr
    assert "memory-hungry step" in result.stderr

    calls = [line for line in read_log(log_path) if line.startswith("--profile osrm run")]
    # prepare, extract, and the failure-diagnosis df probe -- but never
    # partition or customize, since the pipeline must stop on first failure.
    assert not any("osrm-partition" in c for c in calls)
    assert not any("osrm-customize" in c for c in calls)


def test_step_failure_with_low_volume_space_reports_disk_not_generic_failure(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(
        script,
        [FLORIDA_URL],
        tmp_path,
        base_env(
            bin_dir,
            log_path,
            compose_cmd="docker compose",
            FAKE_EXTRACT_EXIT="1",
            FAKE_DF_LINE="overlay 102400000 102300000 1000 99% /data",
        ),
    )

    assert result.returncode != 0
    assert "insufficient disk space in the volume's backing storage" in result.stderr


def test_step_failure_with_plenty_of_space_reports_generic_failure(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(
        script,
        [FLORIDA_URL],
        tmp_path,
        base_env(bin_dir, log_path, compose_cmd="docker compose", FAKE_PARTITION_EXIT="1"),
    )

    assert result.returncode != 0
    assert "osrm-partition failed (exit 1); see the container output above" in result.stderr


def test_customize_failure_still_reported_after_earlier_steps_succeed(tmp_path):
    script = _install_script(tmp_path)
    log_path = tmp_path / "fake.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_curl_fake(bin_dir)
    install_docker_fake(bin_dir)

    result = run_script(
        script,
        [FLORIDA_URL],
        tmp_path,
        base_env(bin_dir, log_path, compose_cmd="docker compose", FAKE_CUSTOMIZE_EXIT="1"),
    )

    assert result.returncode != 0
    assert "osrm-customize failed (exit 1)" in result.stderr
    calls = [line for line in read_log(log_path) if line.startswith("--profile osrm run")]
    assert any("osrm-partition" in c for c in calls)
    assert any("osrm-customize" in c for c in calls)
