#!/usr/bin/env bash
# Maintainer-facing, disposable Compose drill that rehearses backup, restore,
# application upgrade, and rollback from a source ref to either another
# source ref or a published candidate image. Safe by construction: every
# container, volume, and built image it creates is scoped to one unique-per-run
# project (a "mtdrill<epoch>"
# directory basename, which Compose turns into its project name), and the
# script only ever removes a volume whose name begins with that exact stamp.
# It never runs a compose command from this repository's own working tree.
#
# Usage:
#   scripts/upgrade_check.sh --base REF --candidate REF [--keep]
#   scripts/upgrade_check.sh --base REF --candidate REF --candidate-image IMAGE [--keep]
#
# REF is any git commit-ish resolvable in this checkout (tag, branch, SHA).
# --keep skips container/volume/scratch teardown at exit, for inspecting a
# run (successful or failed) in place.
#
# Env vars:
#   COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
set -euo pipefail

HEALTH_PORT=8077
BASE_URL="http://127.0.0.1:${HEALTH_PORT}"
ADMIN_EMAIL="drill-admin@example.test"
POSTRESTORE_DEVICE="postrestore-check"

usage() {
    cat >&2 <<'EOF'
usage: scripts/upgrade_check.sh --base REF --candidate REF [--candidate-image IMAGE] [--keep]

Runs a disposable Compose backup/restore/upgrade/rollback rehearsal from a
base git ref to a candidate git ref. When --candidate-image is given, the
candidate tree supplies Compose and operational scripts while its app service
uses that exact published image. Never touches the default Compose project,
any existing container/volume, or the repository working tree.

  --base REF              git commit-ish for the currently-supported release
  --candidate REF         git commit-ish to build as the candidate
  --candidate-image IMAGE published candidate image with an explicit non-latest
                          tag or digest; omitted to build candidate source
  --keep                  skip container/volume/scratch cleanup for debugging

Base and candidate trees may differ in their app service, but the rendered db
service must be identical. A release that changes db service operations needs
a release-specific drill rather than this script.
EOF
    exit "${1:-1}"
}

BASE_REF=""
CANDIDATE_REF=""
CANDIDATE_IMAGE=""
KEEP=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --base)
            [ "$#" -ge 2 ] || { echo "error: --base requires a value" >&2; usage; }
            BASE_REF="$2"; shift 2 ;;
        --candidate)
            [ "$#" -ge 2 ] || { echo "error: --candidate requires a value" >&2; usage; }
            CANDIDATE_REF="$2"; shift 2 ;;
        --candidate-image)
            [ "$#" -ge 2 ] || { echo "error: --candidate-image requires a value" >&2; usage; }
            CANDIDATE_IMAGE="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "error: unrecognized argument: $1" >&2; usage ;;
    esac
done
if [ -z "$BASE_REF" ] || [ -z "$CANDIDATE_REF" ]; then
    echo "error: --base and --candidate are both required" >&2
    usage
fi

immutable_image_ref() {
    local ref="$1" tag
    if [[ "$ref" =~ ^[^[:space:]@]+@sha256:[[:xdigit:]]{64}$ ]]; then
        return 0
    fi
    if [[ "$ref" =~ ^[^[:space:]@]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
        tag="${ref##*:}"
        # Lowercased with tr rather than "${tag,,}": that expansion is bash 4+,
        # and macOS still ships bash 3.2, where it is a syntax error rather
        # than a graceful failure.
        [ "$(printf '%s' "$tag" | tr '[:upper:]' '[:lower:]')" != "latest" ] && return 0
    fi
    return 1
}

if [ -n "$CANDIDATE_IMAGE" ] && ! immutable_image_ref "$CANDIDATE_IMAGE"; then
    echo "error: --candidate-image must use an explicit non-latest tag or a full sha256 digest" >&2
    usage
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

step_pass() { echo "[PASS] $1"; }
step_fail() { echo "[FAIL] $1" >&2; exit 1; }

detect_compose_cmd() {
    if [ -n "${COMPOSE_CMD:-}" ]; then
        echo "$COMPOSE_CMD"
        return
    fi
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        echo "docker compose"
        return
    fi
    if command -v podman-compose >/dev/null 2>&1; then
        echo "podman-compose"
        return
    fi
    echo "error: neither 'docker compose' nor 'podman-compose' found on PATH; set COMPOSE_CMD to override." >&2
    exit 1
}
compose_cmd="$(detect_compose_cmd)"

runtime_cmd() {
    # Neither "docker compose" nor "podman-compose" exposes a volume
    # subcommand of its own; volume identification/removal has to go
    # through the underlying single-binary runtime instead.
    local first="${compose_cmd%% *}"
    if [ "$first" = "podman-compose" ]; then
        echo "podman"
    else
        echo "$first"
    fi
}

# --- preconditions: nothing below this point may create anything yet ------

if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null; then
    echo "error: --base ref '$BASE_REF' does not resolve to a commit in $REPO_ROOT" >&2
    exit 1
fi
if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "${CANDIDATE_REF}^{commit}" >/dev/null; then
    echo "error: --candidate ref '$CANDIDATE_REF' does not resolve to a commit in $REPO_ROOT" >&2
    exit 1
fi

port_in_use() {
    # A plain connect probe (no lsof/netstat dependency): success means
    # something is already listening on the port compose.yaml publishes,
    # which this drill must refuse to run against.
    (exec 3<>"/dev/tcp/127.0.0.1/${HEALTH_PORT}") 2>/dev/null && { exec 3<&- 3>&- 2>/dev/null || true; return 0; }
    return 1
}
if port_in_use; then
    echo "error: something is already listening on 127.0.0.1:${HEALTH_PORT}; this drill needs that port free. Stop whatever is using it first." >&2
    exit 1
fi

# A stopped podman machine (or a docker daemon that isn't running) still
# lets "docker compose"/"podman-compose" resolve on PATH; only asking the
# underlying runtime to respond catches that before scratch dirs exist.
runtime="$(runtime_cmd)"
if ! "$runtime" info >/dev/null 2>&1; then
    echo "error: container runtime '$runtime' is not responding -- start it (e.g. the podman machine or the docker daemon) and re-run." >&2
    exit 1
fi

step_pass "preconditions: base/candidate refs resolve, compose command is '$compose_cmd', port ${HEALTH_PORT} is free, runtime '$runtime' is responding"

# --- unique run stamp, scratch layout, and teardown trap -------------------

STAMP="$(date +%s)"
PROJECT="mtdrill${STAMP}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/upgrade_check.XXXXXX")"
BASE_DIR="$SCRATCH/base/$PROJECT"
CAND_DIR="$SCRATCH/cand/$PROJECT"
mkdir -p "$SCRATCH/http" "$SCRATCH/backup" "$BASE_DIR" "$CAND_DIR"

# $compose_cmd is a two-word string for "docker compose"; deliberately
# unquoted below (matching backup_database.sh/restore_database.sh) so it
# splits into the "docker"/"compose" words instead of being looked up as one
# literal (nonexistent) binary named "docker compose").
compose_in() {
    local dir="$1"; shift
    (cd "$dir" && $compose_cmd "$@")
}

# Every compose invocation against the base checkout -- not just the "up"
# that creates db -- must pass the exact same -f file set for the life of
# this run. Compose treats a *different* file set on a later "up" as a
# config change and silently recreates the drifted service; observed in
# practice, that recreate can drop a sibling container ("up -d app" after
# the db-only override was omitted removed the already-running app
# container instead of leaving it alone). Passing both files everywhere,
# including "down", sidesteps that class of surprise entirely.
compose_base() {
    local files=(-f compose.yaml -f compose.seed-port.override.yml)
    if [ -f "$BASE_DIR/compose.build.override.yml" ]; then
        files+=(-f compose.build.override.yml)
    fi
    (cd "$BASE_DIR" && $compose_cmd "${files[@]}" "$@")
}

# Same rule as compose_base above: each side keeps its own file set stable for
# every invocation. Source mode opts into a shipped source-build override when
# present; image mode instead appends the generated app-only image override.
compose_cand() {
    local files=(-f compose.yaml -f compose.seed-port.override.yml)
    if [ -n "$CANDIDATE_IMAGE" ]; then
        files+=(-f compose.candidate-image.override.yml)
    elif [ -f "$CAND_DIR/compose.build.override.yml" ]; then
        files+=(-f compose.build.override.yml)
    fi
    (cd "$CAND_DIR" && $compose_cmd "${files[@]}" "$@")
}

remove_stamp_volumes() {
    # The one hazard this script must never risk is deleting a volume that
    # isn't this run's own, so a name is only ever removed after it's been
    # positively discovered (via volume ls, not assumed) to begin with this
    # run's unique project stamp.
    local runtime matches vol
    runtime="$(runtime_cmd)"
    matches="$($runtime volume ls --format '{{.Name}}' 2>/dev/null | grep -E "^${PROJECT}" || true)"
    [ -n "$matches" ] || return 0
    while IFS= read -r vol; do
        [ -z "$vol" ] && continue
        case "$vol" in
            "${PROJECT}"*) ;;
            *)
                echo "error: refusing to remove volume '$vol' -- does not begin with run stamp '$PROJECT'" >&2
                return 1
                ;;
        esac
        echo "Removing disposable volume: $vol"
        "$runtime" volume rm "$vol" >/dev/null 2>&1 || true
    done <<< "$matches"
}

remove_stamp_images() {
    # Same discipline as remove_stamp_volumes above: compose builds an image
    # per project that plain "compose down" never removes, so this is the
    # only thing that cleans it up. The two frontends name it differently --
    # podman-compose: "localhost/mtdrill<stamp>_app"; docker compose:
    # "mtdrill<stamp>-app" (no localhost/ prefix, hyphen not underscore) --
    # so both forms are matched. An image is only ever removed after it's
    # been positively discovered (via image ls, not assumed) to match one of
    # those two forms against this run's stamp.
    local runtime matches img
    runtime="$(runtime_cmd)"
    matches="$($runtime image ls --format '{{.Repository}}' 2>/dev/null | grep -E "^(localhost/${PROJECT}|${PROJECT}[-_])" || true)"
    [ -n "$matches" ] || return 0
    while IFS= read -r img; do
        [ -z "$img" ] && continue
        case "$img" in
            "localhost/${PROJECT}"*|"${PROJECT}"[-_]*) ;;
            *)
                echo "error: refusing to remove image '$img' -- does not begin with 'localhost/$PROJECT' or '$PROJECT-'/'$PROJECT_'" >&2
                return 1
                ;;
        esac
        echo "Removing disposable image: $img"
        # Best-effort: an image can still be in use if a prior step failed
        # unusually, and that must not turn a cleanup pass into a hard error.
        "$runtime" rmi "$img" >/dev/null 2>&1 || true
    done <<< "$matches"
}

cleanup() {
    local exit_code=$?
    if [ "$KEEP" -eq 1 ]; then
        echo
        echo "--keep given: leaving containers/volumes/scratch in place for inspection."
        echo "  project:  $PROJECT"
        echo "  scratch:  $SCRATCH"
        echo "  manual teardown: (cd '$BASE_DIR' && $compose_cmd down), then remove any"
        echo "  volume(s) beginning with '$PROJECT' and any image(s) named"
        echo "  'localhost/$PROJECT*' (podman) or '$PROJECT-*'/'${PROJECT}_*' (docker),"
        echo "  then: rm -rf '$SCRATCH'"
        exit "$exit_code"
    fi
    # Same two-file rule as compose_base's own comment, but cleanup can fire
    # before step1 has written the override (e.g. a precondition-adjacent
    # failure after the trap is installed); compose errors on a missing -f
    # file, so fall back to the single-file form until the override exists.
    if [ -d "$BASE_DIR" ]; then
        if [ -f "$BASE_DIR/compose.seed-port.override.yml" ]; then
            compose_base down >/dev/null 2>&1 || true
        else
            compose_in "$BASE_DIR" down >/dev/null 2>&1 || true
        fi
    fi
    remove_stamp_volumes || true
    remove_stamp_images || true
    rm -rf -- "$SCRATCH"
    exit "$exit_code"
}
trap cleanup EXIT

# --- generic helpers ---------------------------------------------------

# Helpers below are shared between the base and candidate checkouts, so they
# take a directory and route through whichever wrapper keeps that
# directory's compose invocations consistent (see compose_base above).
compose_dir() {
    local dir="$1"; shift
    if [ "$dir" = "$BASE_DIR" ]; then
        compose_base "$@"
    else
        compose_cand "$@"
    fi
}

wait_for_healthz() {
    local timeout_s="$1" deadline
    deadline=$(( $(date +%s) + timeout_s ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ "$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "${BASE_URL}/healthz" 2>/dev/null)" = "200" ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

wait_for_pg_ready() {
    local dir="$1" timeout_s="$2" deadline
    deadline=$(( $(date +%s) + timeout_s ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        # TCP probe, not a socket one: same init-phase race
        # scripts/restore_database.sh guards against. On a fresh volume,
        # postgres's temporary init-phase server (initdb/PostGIS setup)
        # listens only on the unix socket; probing 127.0.0.1 only succeeds
        # once the final, TCP-listening server has replaced it.
        if compose_dir "$dir" exec -T db pg_isready -h 127.0.0.1 -p 5432 -U mileage -d mileage >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

wait_for_detected_trip() {
    local dir="$1" device="$2" timeout_s="$3" deadline count
    deadline=$(( $(date +%s) + timeout_s ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        count="$(compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT count(*) FROM trips WHERE device = '${device}' AND source = 'detected'")"
        [ "${count:-0}" -ge 1 ] && return 0
        sleep 5
    done
    return 1
}

env_value() {
    grep -m1 "^$2=" "$1" | cut -d= -f2-
}

find_free_port() {
    python3 - <<'PYEOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF
}

schema_version() {
    compose_dir "$1" exec -T db psql -U mileage -d mileage -Atc \
        "SELECT COALESCE(max(version), 0) FROM schema_migrations"
}

migration_count() {
    if [ -n "$CANDIDATE_IMAGE" ] && [ "$1" = "$CAND_DIR" ]; then
        compose_cand exec -T app sh -c \
            "find migrations -maxdepth 1 -name '*.sql' | wc -l" | tr -d '[:space:]'
    else
        find "$1/migrations" -maxdepth 1 -name '*.sql' | wc -l | tr -d '[:space:]'
    fi
}

count_points() {
    compose_dir "$1" exec -T db psql -U mileage -d mileage -Atc \
        "SELECT count(*) FROM points WHERE device = '$2'"
}

# schema_version is its own manifest section so a migration's expected
# difference across an upgrade is trivial to isolate from the rest of the
# diff (see strip_schema_version_section / assert_data_manifests_equal
# below); every other comparison in this drill is base-to-base and expects
# the whole file, schema_version included, to match byte-for-byte.
capture_manifest() {
    local dir="$1" outfile="$2"
    {
        echo "== schema_version =="
        schema_version "$dir"

        echo "== local_admin =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT email FROM local_admin ORDER BY id"

        echo "== trips_by_source_category =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT source::text, category::text, count(*), round(coalesce(sum(distance_m),0)::numeric, -2) FROM trips GROUP BY 1, 2 ORDER BY 1, 2"

        echo "== trip_points =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT device, source::text, point_count, (path IS NOT NULL) FROM trips ORDER BY device, started_at, id"

        echo "== places =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT name, kind::text, round(radius_m::numeric, 0) FROM places ORDER BY name"

        echo "== tag_rules =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT coalesce(a_kind::text,'-'), (a_place IS NOT NULL), coalesce(b_kind::text,'-'), (b_place IS NOT NULL), category::text FROM tag_rules ORDER BY 1, 2, 3, 4, 5"

        echo "== vehicles =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT name, coalesce(make,'-'), coalesce(model,'-'), is_default, active FROM vehicles ORDER BY name, id"

        echo "== expenses =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT category::text, treatment::text, count(*), round(coalesce(sum(amount),0)::numeric, 0) FROM expenses GROUP BY 1, 2 ORDER BY 1, 2"

        echo "== odometer_readings =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT count(*), round(coalesce(min(odometer_m),0)::numeric, -3), round(coalesce(max(odometer_m),0)::numeric, -3) FROM odometer_readings"

        echo "== trip_boundary_overrides =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT kind::text, count(*) FROM trip_boundary_overrides GROUP BY 1 ORDER BY 1"

        echo "== reference_rows =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT 'mileage_rates', count(*) FROM mileage_rates ORDER BY 1"

        echo "== worker_ledgers =="
        compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc \
            "SELECT 'geocode_cache', count(*) FROM geocode_cache
             UNION ALL SELECT 'raw_messages', count(*) FROM raw_messages
             UNION ALL SELECT 'nudge_delivery_windows', count(*) FROM nudge_delivery_windows
             UNION ALL SELECT 'odometer_reminder_windows', count(*) FROM odometer_reminder_windows
             UNION ALL SELECT 'email_deliveries', count(*) FROM email_deliveries
             ORDER BY 1"
    } > "$outfile"
}

assert_manifests_equal() {
    local a="$1" b="$2" desc="$3"
    if diff -u "$a" "$b" > "$SCRATCH/last-manifest-diff.txt"; then
        step_pass "$desc"
    else
        echo "manifest mismatch ($desc):" >&2
        cat "$SCRATCH/last-manifest-diff.txt" >&2
        step_fail "$desc"
    fi
}

# schema_version legitimately changes across an upgrade that runs a
# migration, so step 7 (the only caller that spans an upgrade) can't reuse
# assert_manifests_equal's whole-file diff; it checks schema_version
# separately against the candidate's migration count instead. This strips
# just that one section before comparing everything else, so every data
# section still has to match byte-for-byte.
strip_schema_version_section() {
    awk '
        /^== schema_version ==$/ { skip = 1; next }
        skip && /^== / { skip = 0 }
        skip { next }
        { print }
    ' "$1"
}

assert_data_manifests_equal() {
    local a="$1" b="$2" desc="$3" a_data b_data
    a_data="$(mktemp "$SCRATCH/manifest-data.XXXXXX")"
    b_data="$(mktemp "$SCRATCH/manifest-data.XXXXXX")"
    strip_schema_version_section "$a" > "$a_data"
    strip_schema_version_section "$b" > "$b_data"
    assert_manifests_equal "$a_data" "$b_data" "$desc"
}

# --- HTTP helpers for the token-gated setup flow and local login -----------
# Both /setup and /login/local check a session-bound CSRF token carried as a
# hidden form field (app/auth.py's _check_form_csrf), so each flow needs its
# own GET (to mint the session + read the token) before its POST.

csrf_from_html() {
    grep -o 'name="csrf_token" value="[^"]*"' "$1" | head -n1 | sed -E 's/.*value="([^"]*)".*/\1/'
}

setup_local_admin() {
    local admin_token="$1" email="$2" password="$3"
    local jar html csrf status
    jar="$(mktemp "$SCRATCH/http/setup-cookies.XXXXXX")"
    html="$(mktemp "$SCRATCH/http/setup-get.XXXXXX")"
    curl -sS -m 8 -c "$jar" -o "$html" "${BASE_URL}/setup"
    csrf="$(csrf_from_html "$html")"
    [ -n "$csrf" ] || { echo "error: could not read csrf token from /setup" >&2; return 1; }
    status="$(curl -sS -m 8 -o "$SCRATCH/http/setup-post.html" -w '%{http_code}' \
        -c "$jar" -b "$jar" \
        --data-urlencode "token=${admin_token}" \
        --data-urlencode "email=${email}" \
        --data-urlencode "password=${password}" \
        --data-urlencode "password_confirm=${password}" \
        --data-urlencode "csrf_token=${csrf}" \
        "${BASE_URL}/setup")"
    [ "$status" = "303" ]
}

login_local_admin() {
    local email="$1" password="$2"
    local jar html csrf status
    jar="$(mktemp "$SCRATCH/http/login-cookies.XXXXXX")"
    html="$(mktemp "$SCRATCH/http/login-get.XXXXXX")"
    curl -sS -m 8 -c "$jar" -o "$html" "${BASE_URL}/login"
    csrf="$(csrf_from_html "$html")"
    [ -n "$csrf" ] || { echo "error: could not read csrf token from /login" >&2; return 1; }
    status="$(curl -sS -m 8 -o "$SCRATCH/http/login-post.html" -w '%{http_code}' \
        -c "$jar" -b "$jar" \
        --data-urlencode "email=${email}" \
        --data-urlencode "password=${password}" \
        --data-urlencode "csrf_token=${csrf}" \
        "${BASE_URL}/login/local")"
    [ "$status" = "303" ]
}

ingest_one_point() {
    local ingest_password="$1" device="$2" status
    # Same illustrative Golden Gate Park coordinate scripts/send_test_track.sh
    # already uses -- not tied to any operator's real location.
    status="$(curl -sS -m 8 -o "$SCRATCH/http/ingest-post.json" -w '%{http_code}' \
        -u "owntracks:${ingest_password}" \
        -H 'Content-Type: application/json' \
        --data-binary "{\"_type\":\"location\",\"tid\":\"${device}\",\"lat\":37.76940,\"lon\":-122.48300,\"tst\":$(date +%s),\"acc\":10}" \
        "${BASE_URL}/ingest")"
    [ "$status" = "200" ]
}

seed_python_bin() {
    # scripts/dev_seed.py imports the app package directly (no install
    # step), so any interpreter with psycopg on its path works; the repo's
    # own dev virtualenv already satisfies that on a maintainer workstation
    # set up per the project's own testing instructions.
    local venv_python="$REPO_ROOT/.venv/bin/python"
    if [ -x "$venv_python" ] && "$venv_python" -c "import psycopg" >/dev/null 2>&1; then
        echo "$venv_python"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1 && python3 -c "import psycopg" >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    echo "error: no Python with psycopg available to run dev_seed.py (set up $REPO_ROOT/.venv per the project's testing instructions)." >&2
    exit 1
}

write_seed_port_override() {
    local dir="$1" port="$2"
    cat > "$dir/compose.seed-port.override.yml" <<EOF
services:
  db:
    ports:
      - "127.0.0.1:${port}:5432"
EOF
}

write_candidate_image_override() {
    local image_quoted
    image_quoted="${CANDIDATE_IMAGE//\'/\'\'}"
    cat > "$CAND_DIR/compose.candidate-image.override.yml" <<EOF
services:
  app:
    image: '${image_quoted}'
    pull_policy: always
EOF
}

capture_db_service_config() {
    local dir="$1" outfile="$2" config_file
    config_file="$(mktemp "$SCRATCH/compose-config.XXXXXX")"
    compose_dir "$dir" config > "$config_file"
    python3 - "$config_file" "$outfile" <<'PYEOF'
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text().splitlines()
start = None
for index, line in enumerate(source):
    if line.rstrip() == "  db:":
        start = index
        break
if start is None:
    raise SystemExit("rendered Compose config has no db service")

captured = []
for line in source[start:]:
    if captured and line.strip() and len(line) - len(line.lstrip()) <= 2:
        break
    captured.append(line.rstrip())
pathlib.Path(sys.argv[2]).write_text("\n".join(captured).rstrip() + "\n")
PYEOF
}

assert_stable_db_service() {
    capture_db_service_config "$BASE_DIR" "$SCRATCH/base-db-service.yml"
    capture_db_service_config "$CAND_DIR" "$SCRATCH/candidate-db-service.yml"
    if ! diff -u "$SCRATCH/base-db-service.yml" "$SCRATCH/candidate-db-service.yml" \
        > "$SCRATCH/db-service-diff.txt"; then
        echo "error: base and candidate rendered db service definitions differ:" >&2
        cat "$SCRATCH/db-service-diff.txt" >&2
        echo "error: this generic drill requires a stable db service; use a release-specific operations migration plan." >&2
        return 1
    fi
}

# --- drill steps -------------------------------------------------------

step_materialize() {
    git -C "$REPO_ROOT" archive "$BASE_REF" | tar -x -C "$BASE_DIR"
    git -C "$REPO_ROOT" archive "$CANDIDATE_REF" | tar -x -C "$CAND_DIR"
    step_pass "materialized base ($BASE_REF -> $BASE_DIR) and candidate ($CANDIDATE_REF -> $CAND_DIR)"
}

step1_base_up() {
    (cd "$BASE_DIR" && ./scripts/generate_env.sh) >/dev/null
    cp "$BASE_DIR/.env" "$CAND_DIR/.env"

    # scripts/dev_seed.py (step 2) refuses any --database-url whose host
    # isn't loopback, and the canonical compose.yaml never publishes db's
    # port -- app only ever reaches it by the compose network hostname
    # "db". Publishing a throwaway loopback port here lets dev_seed.py run
    # exactly as designed (real migrations, real detector) instead of
    # reimplementing its seed data by hand; nothing later in this drill
    # ever connects to db via this port.
    SEED_PORT="$(find_free_port)"
    write_seed_port_override "$BASE_DIR" "$SEED_PORT"
    # Identical override in the candidate checkout too, purely so its -f
    # file set matches the base checkout's (see compose_base above) --
    # nothing reads the port from this copy.
    write_seed_port_override "$CAND_DIR" "$SEED_PORT"

    if [ -n "$CANDIDATE_IMAGE" ]; then
        write_candidate_image_override
    fi
    assert_stable_db_service \
        || step_fail "step 1: base/candidate db service definitions are not stable"

    compose_base up -d --build db app
    wait_for_healthz 240 || step_fail "step 1: base install did not become healthy at ${BASE_URL}/healthz"
    step_pass "step 1: base ($BASE_REF) up and healthy at ${BASE_URL}"
}

step2_seed() {
    local admin_token seed_url seed_python
    admin_token="$(env_value "$BASE_DIR/.env" ADMIN_TOKEN)"
    POSTGRES_PASSWORD="$(env_value "$BASE_DIR/.env" POSTGRES_PASSWORD)"
    INGEST_PASSWORD="$(env_value "$BASE_DIR/.env" INGEST_PASSWORD)"
    ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"

    setup_local_admin "$admin_token" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" \
        || step_fail "step 2: /setup did not create the local administrator"
    step_pass "step 2a: local admin created via the token-gated /setup flow"

    # app is stopped for the duration of seeding: its own background
    # detector scheduler could otherwise grab the same per-run advisory
    # lock dev_seed.py's run_once() needs, turning a rare race into an
    # occasional spurious failure.
    compose_base stop app >/dev/null
    seed_python="$(seed_python_bin)"
    seed_url="postgresql://mileage:${POSTGRES_PASSWORD}@127.0.0.1:${SEED_PORT}/mileage"
    "$seed_python" "$BASE_DIR/scripts/dev_seed.py" --database-url "$seed_url" --wipe \
        || step_fail "step 2: dev_seed.py failed to seed reference data"
    compose_base up -d app >/dev/null
    wait_for_healthz 120 || step_fail "step 2: app did not come back healthy after seeding"
    step_pass "step 2b: reference data (2 vehicles, expenses, odometer readings, places, tag rules, manual trips, a discard override) seeded via dev_seed.py"

    # dev_seed.py truncates points/stays/trips, so the real detected trip
    # below is sent only after seeding -- sending it first would have had
    # dev_seed.py's --wipe erase it again.
    BASE_URL="$BASE_URL" INGEST_PASSWORD="$INGEST_PASSWORD" "$BASE_DIR/scripts/send_test_track.sh" \
        || step_fail "step 2: send_test_track.sh failed"
    wait_for_detected_trip "$BASE_DIR" test 240 \
        || step_fail "step 2: no detected trip appeared for device 'test' within the debounce window"
    step_pass "step 2c: a real detected trip (with points/geometry) captured via send_test_track.sh"
}

step3_manifest_before() {
    capture_manifest "$BASE_DIR" "$SCRATCH/manifest-before.txt"
    step_pass "step 3: captured the semantic manifest before backup"
}

step4_backup_and_verify() {
    BACKUP_ARCHIVE="$SCRATCH/backup/mileage.dump"
    # The candidate's own backup/restore scripts are the artifact under
    # test, invoked by absolute path from the candidate materialization
    # but run with the base install as the live target, exactly as an
    # operator upgrading from base to candidate would run them.
    (cd "$BASE_DIR" && "$CAND_DIR/scripts/backup_database.sh" --output "$BACKUP_ARCHIVE") \
        || step_fail "step 4: backup_database.sh failed"
    (cd "$BASE_DIR" && "$CAND_DIR/scripts/restore_database.sh" --verify-only "$BACKUP_ARCHIVE") \
        || step_fail "step 4: restore_database.sh --verify-only rejected the archive it just produced"
    step_pass "step 4: online backup produced and verified via the candidate's backup/restore scripts"
}

step5_destroy_and_restore() {
    compose_base down
    remove_stamp_volumes
    compose_base up -d db
    # First-init PostGIS provisioning on a fresh volume can run well past a
    # minute, more so under CPU emulation.
    wait_for_pg_ready "$BASE_DIR" 180 || step_fail "step 5: fresh db did not become ready"
    (cd "$BASE_DIR" && "$CAND_DIR/scripts/restore_database.sh" "$BACKUP_ARCHIVE") \
        || step_fail "step 5: restore_database.sh failed against the fresh volume"
    capture_manifest "$BASE_DIR" "$SCRATCH/manifest-after-restore.txt"
    assert_manifests_equal "$SCRATCH/manifest-before.txt" "$SCRATCH/manifest-after-restore.txt" \
        "step 5: semantic manifest matches after destroy + fresh-volume restore"
}

step6_post_restore_check() {
    local before after
    # app is rebuilt on every "up" for either checkout: base and candidate
    # share one project name and therefore one image tag, so without
    # --build here this would silently keep running whatever image a
    # later candidate build (step 7) leaves behind.
    compose_base up -d --build app
    wait_for_healthz 120 || step_fail "step 6: restored app did not become healthy"

    login_local_admin "$ADMIN_EMAIL" "$ADMIN_PASSWORD" \
        || step_fail "step 6: local admin sign-in failed after restore"

    before="$(count_points "$BASE_DIR" "$POSTRESTORE_DEVICE")"
    ingest_one_point "$INGEST_PASSWORD" "$POSTRESTORE_DEVICE" \
        || step_fail "step 6: authenticated /ingest point was rejected"
    after="$(count_points "$BASE_DIR" "$POSTRESTORE_DEVICE")"
    [ "$after" -eq "$((before + 1))" ] \
        || step_fail "step 6: points count for device '$POSTRESTORE_DEVICE' did not increment ($before -> $after)"

    capture_manifest "$BASE_DIR" "$SCRATCH/manifest-after-ingest.txt"
    step_pass "step 6: restored app healthy, admin sign-in succeeded, ingest accepted a new point ($before -> $after)"
}

step7_upgrade() {
    local v mig
    compose_base stop app
    if [ -n "$CANDIDATE_IMAGE" ]; then
        compose_cand up -d --no-build app
    else
        compose_cand up -d --build app
    fi
    wait_for_healthz 120 || step_fail "step 7: candidate app did not become healthy"

    v="$(schema_version "$CAND_DIR")"
    mig="$(migration_count "$CAND_DIR")"
    [ "$v" = "$mig" ] || step_fail "step 7: schema_version ($v) does not equal the candidate's migration count ($mig)"

    capture_manifest "$CAND_DIR" "$SCRATCH/manifest-after-upgrade.txt"
    assert_data_manifests_equal "$SCRATCH/manifest-after-ingest.txt" "$SCRATCH/manifest-after-upgrade.txt" \
        "step 7: candidate healthy, schema_version=$v matches migration count, data unchanged"
}

step8_rollback() {
    local v mig
    compose_base down
    remove_stamp_volumes
    compose_base up -d db
    # Same fresh-volume PostGIS init time as step 5's wait_for_pg_ready call.
    wait_for_pg_ready "$BASE_DIR" 180 || step_fail "step 8: fresh db did not become ready"
    (cd "$BASE_DIR" && "$CAND_DIR/scripts/restore_database.sh" "$BACKUP_ARCHIVE") \
        || step_fail "step 8: restore_database.sh failed restoring the pre-upgrade archive"

    compose_base up -d --build app
    wait_for_healthz 120 || step_fail "step 8: rolled-back base app did not become healthy"

    v="$(schema_version "$BASE_DIR")"
    mig="$(migration_count "$BASE_DIR")"
    [ "$v" = "$mig" ] || step_fail "step 8: schema_version ($v) does not equal the base release's migration count ($mig)"

    capture_manifest "$BASE_DIR" "$SCRATCH/manifest-after-rollback.txt"
    # Compared against the PRE-ingest baseline, not manifest-after-ingest:
    # the archive being restored here predates step 6's post-restore ingest
    # point, so a correct rollback must show that write as lost, exactly as
    # the documented rollback contract promises.
    assert_manifests_equal "$SCRATCH/manifest-before.txt" "$SCRATCH/manifest-after-rollback.txt" \
        "step 8: rollback restored the prior release + pre-upgrade data; the post-backup ingest write was lost as documented"
}

# --- run the drill -------------------------------------------------------

step_materialize
step1_base_up
step2_seed
step3_manifest_before
step4_backup_and_verify
step5_destroy_and_restore
step6_post_restore_check
step7_upgrade
step8_rollback

echo
echo "==== upgrade_check.sh: all drill steps passed ===="
echo "base:      $BASE_REF"
echo "candidate: $CANDIDATE_REF"
if [ -n "$CANDIDATE_IMAGE" ]; then
    echo "candidate image: $CANDIDATE_IMAGE"
fi
echo "project:   $PROJECT"
