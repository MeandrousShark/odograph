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
#   scripts/upgrade_check.sh --base REF --candidate REF --database-image-migration [--keep]
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
ADMIN_EMAIL="development@localhost.invalid"
POSTRESTORE_DEVICE="postrestore-check"
BASE_SCHEMA_VERSION=0
INGEST_USERNAME="owntracks"
OIDC_ISSUER="https://idp.example.test"
OIDC_CLIENT_ID="upgrade-drill-client"
OIDC_SUBJECT="upgrade-drill-subject"
OIDC_EMAIL="drill-oidc@example.test"

usage() {
    cat >&2 <<'EOF'
usage: scripts/upgrade_check.sh --base REF --candidate REF [--base-image IMAGE] [--candidate-image IMAGE] [--database-image-migration] [--keep]

Runs a disposable Compose backup/restore/upgrade/rollback rehearsal from a
base git ref to a candidate git ref. When --candidate-image is given, the
candidate tree supplies Compose and operational scripts while its app service
uses that exact published image. Never touches the default Compose project,
any existing container/volume, or the repository working tree.

  --base REF              git commit-ish for the currently-supported release
  --base-image IMAGE      exact earlier app image, instead of rebuilding base source
  --candidate REF         git commit-ish to build as the candidate
  --candidate-image IMAGE published candidate image with an explicit non-latest
                          tag or digest; omitted to build candidate source
  --database-image-migration
                          allow exactly the rendered db image change and exercise
                          fresh-volume candidate restore plus old-image rollback
  --keep                  skip container/volume/scratch cleanup for debugging

Base and candidate trees may differ in their app service, but the rendered db
service must be identical by default. Migration mode allows only the rendered
db image to change; any other db-service drift needs a release-specific drill.
EOF
    exit "${1:-1}"
}

BASE_REF=""
BASE_IMAGE=""
CANDIDATE_REF=""
CANDIDATE_IMAGE=""
DATABASE_IMAGE_MIGRATION=0
KEEP=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --base)
            [ "$#" -ge 2 ] || { echo "error: --base requires a value" >&2; usage; }
            BASE_REF="$2"; shift 2 ;;
        --base-image)
            [ "$#" -ge 2 ] || { echo "error: --base-image requires a value" >&2; usage; }
            BASE_IMAGE="$2"; shift 2 ;;
        --candidate)
            [ "$#" -ge 2 ] || { echo "error: --candidate requires a value" >&2; usage; }
            CANDIDATE_REF="$2"; shift 2 ;;
        --candidate-image)
            [ "$#" -ge 2 ] || { echo "error: --candidate-image requires a value" >&2; usage; }
            CANDIDATE_IMAGE="$2"; shift 2 ;;
        --database-image-migration)
            DATABASE_IMAGE_MIGRATION=1; shift ;;
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

if [ -n "$BASE_IMAGE" ] && ! immutable_image_ref "$BASE_IMAGE"; then
    echo "error: --base-image must use an explicit non-latest tag or a full sha256 digest" >&2
    usage
fi

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
    (exec 3<>"/dev/tcp/127.0.0.1/${HEALTH_PORT}") 2>/dev/null
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
MIGRATION_BACKUP_ARCHIVE="$SCRATCH/backup/mileage-after-ingest.dump"
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
    if [ -n "$BASE_IMAGE" ]; then
        files+=(-f compose.base-image.override.yml)
    elif [ -f "$BASE_DIR/compose.build.override.yml" ]; then
        files+=(-f compose.build.override.yml)
    fi
    (cd "$BASE_DIR" && $compose_cmd "${files[@]}" "$@")
}

start_base_app() {
    if [ -n "$BASE_IMAGE" ]; then
        compose_base up -d --no-build "$@"
    else
        compose_base up -d --build "$@"
    fi
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
    matches="$($runtime volume ls --format '{{.Name}}')" || return 1
    matches="$(printf '%s\n' "$matches" | grep -E "^${PROJECT}_" || true)"
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
        if ! "$runtime" volume rm "$vol" >/dev/null; then
            echo "error: could not remove disposable volume $vol; refusing to reuse it" >&2
            return 1
        fi
    done <<< "$matches"
}

remove_stamp_images() {
    # Same discipline as remove_stamp_volumes above: compose builds an image
    # per project that plain "compose down" never removes, so this is the
    # only thing that cleans it up. The two frontends name it differently --
    # podman-compose: "localhost/mtdrill<stamp>_app"; docker compose:
    # "mtdrill<stamp>-app" (no localhost/ prefix, hyphen not underscore).
    # Match only this task's exact app repository, preserving every tag:
    # dropping :dev would make rmi default to an unrelated/missing :latest.
    local runtime matches img
    runtime="$(runtime_cmd)"
    matches="$($runtime image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | LC_ALL=C sort -u || true)"
    [ -n "$matches" ] || return 0
    while IFS= read -r img; do
        [ -z "$img" ] && continue
        case "${img%:*}" in
            "localhost/${PROJECT}_app"|"localhost/${PROJECT}-app"|"${PROJECT}_app"|"${PROJECT}-app") ;;
            *) continue ;;
        esac
        case "${img##*:}" in "<none>"|"") continue ;; esac
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
        echo "  'localhost/${PROJECT}_app:*' (podman) or '${PROJECT}-app:*'/'${PROJECT}_app:*' (docker),"
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

# Backup/restore invoke Compose themselves, including the one-off app used
# for schema-26 role recovery. Keep their image and file set identical too.
run_install_script() {
    local dir="$1" compose_files="compose.yaml:compose.seed-port.override.yml"; shift
    if [ "$dir" = "$BASE_DIR" ] && [ -n "$BASE_IMAGE" ]; then
        compose_files="$compose_files:compose.base-image.override.yml"
    elif [ "$dir" = "$CAND_DIR" ] && [ -n "$CANDIDATE_IMAGE" ]; then
        compose_files="$compose_files:compose.candidate-image.override.yml"
    elif [ -f "$dir/compose.build.override.yml" ]; then
        compose_files="$compose_files:compose.build.override.yml"
    fi
    (cd "$dir" && COMPOSE_FILE="$compose_files" COMPOSE_PATH_SEPARATOR=: COMPOSE_CMD="$compose_cmd" "$@")
}

db_query() {
    local dir="$1"; shift
    compose_dir "$dir" exec -T db psql -U mileage -d mileage -Atc "$@"
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
        count="$(db_query "$dir" \
            "SELECT count(*) FROM trips WHERE device = '${device}' AND source = 'detected'")"
        [ "${count:-0}" -ge 1 ] && return 0
        sleep 5
    done
    return 1
}

env_value() {
    local file="$1" key="$2" value
    if ! value="$(grep -m1 "^${key}=" "$file" | cut -d= -f2-)"; then
        echo "error: required environment key '$key' is missing from '$file'" >&2
        return 1
    fi
    printf '%s\n' "$value"
}

set_env_value() {
    local file="$1" key="$2" value="$3" updated
    updated="$(mktemp "$SCRATCH/env.XXXXXX")"
    awk -v key="$key" 'index($0, key "=") != 1 { print }' "$file" > "$updated"
    printf '%s=%s\n' "$key" "$value" >> "$updated"
    chmod 600 "$updated"
    mv "$updated" "$file"
}

auth_env_matches() {
    local key base_value candidate_value
    for key in OIDC_ISSUER OIDC_CLIENT_ID OIDC_CLIENT_SECRET; do
        base_value="$(env_value "$BASE_DIR/.env" "$key")" || return 1
        candidate_value="$(env_value "$CAND_DIR/.env" "$key")" || return 1
        [ -n "$base_value" ] || return 1
        [ "$base_value" = "$candidate_value" ] || return 1
    done
}

oidc_env_loaded() {
    # These names must expand inside the app container, not in this script.
    # shellcheck disable=SC2016
    compose_dir "$1" exec -T app sh -c \
        '[ -n "$OIDC_ISSUER" ] && [ -n "$OIDC_CLIENT_ID" ] && [ -n "$OIDC_CLIENT_SECRET" ]' \
        >/dev/null
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
    db_query "$1" \
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
    db_query "$1" \
        "SELECT count(*) FROM points WHERE device = '$2'"
}

administrator_email() {
    local dir="$1" has_accounts
    has_accounts="$(db_query "$dir" "SELECT to_regclass('accounts') IS NOT NULL")"
    if [ "$has_accounts" = "t" ]; then
        db_query "$dir" "SELECT email FROM accounts ORDER BY id"
    else
        db_query "$dir" "SELECT email FROM local_admin ORDER BY id"
    fi
}

# schema_version is its own manifest section so a migration's expected
# difference across an upgrade is trivial to isolate from the rest of the
# diff (see strip_schema_version_section / assert_data_manifests_equal
# below); every other comparison in this drill is base-to-base and expects
# the whole file, schema_version included, to match byte-for-byte.
capture_manifest() {
    local dir="$1" outfile="$2" table
    {
        echo "== schema_version =="
        schema_version "$dir"

        echo "== administrator =="
        administrator_email "$dir"

        echo "== trips_by_source_category =="
        db_query "$dir" \
            "SELECT source::text, category::text, count(*), round(coalesce(sum(distance_m),0)::numeric, -2) FROM trips GROUP BY 1, 2 ORDER BY 1, 2"

        echo "== trip_points =="
        db_query "$dir" \
            "SELECT device, source::text, point_count, (path IS NOT NULL) FROM trips ORDER BY device, started_at, id"

        echo "== places =="
        db_query "$dir" \
            "SELECT name, kind::text, round(radius_m::numeric, 0) FROM places ORDER BY name"

        echo "== tag_rules =="
        db_query "$dir" \
            "SELECT coalesce(a_kind::text,'-'), (a_place IS NOT NULL), coalesce(b_kind::text,'-'), (b_place IS NOT NULL), category::text FROM tag_rules ORDER BY 1, 2, 3, 4, 5"

        echo "== vehicles =="
        db_query "$dir" \
            "SELECT name, coalesce(make,'-'), coalesce(model,'-'), is_default, active FROM vehicles ORDER BY name, id"

        echo "== expenses =="
        db_query "$dir" \
            "SELECT category::text, treatment::text, count(*), round(coalesce(sum(amount),0)::numeric, 0) FROM expenses GROUP BY 1, 2 ORDER BY 1, 2"

        echo "== odometer_readings =="
        db_query "$dir" \
            "SELECT count(*), round(coalesce(min(odometer_m),0)::numeric, -3), round(coalesce(max(odometer_m),0)::numeric, -3) FROM odometer_readings"

        echo "== trip_boundary_overrides =="
        db_query "$dir" \
            "SELECT kind::text, count(*) FROM trip_boundary_overrides GROUP BY 1 ORDER BY 1"

        echo "== reference_rows =="
        db_query "$dir" \
            "SELECT 'mileage_rates', count(*) FROM mileage_rates ORDER BY 1"

        echo "== worker_ledgers =="
        db_query "$dir" \
            "SELECT 'geocode_cache', count(*) FROM geocode_cache
             UNION ALL SELECT 'raw_messages', count(*) FROM raw_messages
             UNION ALL SELECT 'nudge_delivery_windows', count(*) FROM nudge_delivery_windows
             UNION ALL SELECT 'odometer_reminder_windows', count(*) FROM odometer_reminder_windows
             UNION ALL SELECT 'email_deliveries', count(*) FROM email_deliveries
             ORDER BY 1"

        # Exact synthetic rows catch geometry, IDs, foreign keys, exclusions,
        # labels and human edits that aggregate totals alone can conceal.
        # New nullable columns and the ownership columns are expected additions.
        for table in trips points stays places tag_rules vehicles expenses odometer_readings trip_boundary_overrides; do
            echo "== exact_${table} =="
            db_query "$dir" \
                "SELECT jsonb_strip_nulls(to_jsonb(t) - ARRAY['account_id', 'tracking_device_id']) FROM $table t ORDER BY id"
        done
        echo "== exact_mileage_rates =="
        db_query "$dir" \
            "SELECT to_jsonb(r) - 'account_id' FROM mileage_rates r ORDER BY year"
    } > "$outfile"
}

verify_ownership_upgrade() {
    local dir="$1" version="$2" credential_count credential_filter before after
    [ "$version" -ge 26 ] || return 0
    compose_dir "$dir" exec -T app python -m app.application_roles verify \
        || step_fail "step 7: account security contract did not validate"
    credential_filter="c.kind = 'legacy'"
    if [ "$BASE_SCHEMA_VERSION" -ge 26 ]; then
        credential_filter="c.kind = 'device' AND c.basic_username = '${INGEST_USERNAME}'"
    fi
    credential_count="$(db_query "$dir" \
        "SELECT count(*) FROM ingest_credentials c JOIN accounts a ON a.id = c.account_id WHERE a.email = '${ADMIN_EMAIL}' AND $credential_filter AND c.revoked_at IS NULL")"
    [ "$credential_count" = "1" ] \
        || step_fail "step 7: original ingest credential was not preserved exactly once"
    before="$(count_points "$dir" "$POSTRESTORE_DEVICE")"
    ingest_one_point "$INGEST_PASSWORD" "$POSTRESTORE_DEVICE" \
        || step_fail "step 7: original ingest credential was rejected after ownership migration"
    after="$(count_points "$dir" "$POSTRESTORE_DEVICE")"
    [ "$after" -eq "$((before + 1))" ] \
        || step_fail "step 7: migrated tracker did not accept a new point ($before -> $after)"
    step_pass "step 7: account security contract validated and original tracker credentials still ingest"
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

# --- HTTP helpers for setup/signup flows and local login --------------------
# Both /setup, /signup, and /login/local check a session-bound CSRF token
# carried as a hidden form field (app/auth.py's check_form_csrf), so each
# flow needs its own GET (to mint the session + read the token) before its POST.

csrf_from_html() {
    grep -o 'name="csrf_token" value="[^"]*"' "$1" | head -n1 | sed -E 's/.*value="([^"]*)".*/\1/'
}

signup_local_admin() {
    local email="$1" password="$2"
    local jar html headers csrf status home_status
    jar="$(mktemp "$SCRATCH/http/signup-cookies.XXXXXX")"
    html="$(mktemp "$SCRATCH/http/signup-get.XXXXXX")"
    headers="$(mktemp "$SCRATCH/http/signup-post-headers.XXXXXX")"
    curl -sS -m 8 -c "$jar" -o "$html" "${BASE_URL}/signup"
    csrf="$(csrf_from_html "$html")"
    [ -n "$csrf" ] || { echo "error: could not read csrf token from /signup" >&2; return 1; }
    status="$(curl -sS -m 8 -o "$SCRATCH/http/signup-post.html" -D "$headers" -w '%{http_code}' \
        -c "$jar" -b "$jar" \
        --data-urlencode "email=${email}" \
        --data-urlencode "password=${password}" \
        --data-urlencode "password_confirm=${password}" \
        --data-urlencode "csrf_token=${csrf}" \
        "${BASE_URL}/signup")"
    [ "$status" = "303" ] || return 1
    grep -Eiq '^location:[[:space:]]*/[[:space:]]*(\r)?$' "$headers" || return 1
    home_status="$(curl -sS -m 8 -b "$jar" -o "$SCRATCH/http/signup-home.html" \
        -w '%{http_code}' "${BASE_URL}/")"
    [ "$home_status" = "200" ]
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

login_page_offers_oidc() {
    local html
    html="$(mktemp "$SCRATCH/http/login-oidc.XXXXXX")"
    curl -sS -m 8 -o "$html" "${BASE_URL}/login"
    grep -q 'href="/login/oidc"' "$html"
}

rotate_candidate_password() {
    local account_id
    account_id="$(compose_cand exec -T app python -m app.manage_account list-accounts \
        | awk -F '\t' -v email="$ADMIN_EMAIL" '$2 == email { print $1 }')"
    [ -n "$account_id" ] || return 1
    printf 'yes\n%s\n%s\n' "$ROTATED_PASSWORD" "$ROTATED_PASSWORD" \
        | compose_cand exec -T app python -m app.manage_account reset-password "$account_id" \
            >/dev/null
}

ingest_one_point() {
    local ingest_password="$1" device="$2" status
    # Same illustrative Golden Gate Park coordinate scripts/send_test_track.sh
    # already uses -- not tied to any operator's real location.
    status="$(curl -sS -m 8 -o "$SCRATCH/http/ingest-post.json" -w '%{http_code}' \
        -u "${INGEST_USERNAME}:${ingest_password}" \
        -H 'Content-Type: application/json' \
        --data-binary "{\"_type\":\"location\",\"tid\":\"${device}\",\"lat\":37.76940,\"lon\":-122.48300,\"tst\":$(date +%s),\"acc\":10}" \
        "${BASE_URL}/ingest")"
    [ "$status" = "200" ]
}

issue_synthetic_tracking_credentials() {
    local credentials="$SCRATCH/http/tracking-credentials.json"
    # Use the normal restricted account API, only after checking the one
    # known synthetic account. The secret output stays in private scratch.
    if ! compose_base exec -T app python - > "$credentials" <<'PYEOF'
import asyncio
import json
import os

from app.account_context import AccountPool, AccountPrincipal, control_connection
from app.accounts import get_account_by_email
from app.application_roles import application_role_pools
from app.tracking import create_device

async def main():
    async with application_role_pools(os.environ["DATABASE_URL"]) as pools:
        async with control_connection(pools.control) as conn:
            account = await get_account_by_email(conn, "development@localhost.invalid")
            count = (await (await conn.execute("SELECT count(*) FROM accounts")).fetchone())[0]
            if count != 1 or account is None or not account["is_enabled"]:
                raise RuntimeError("tracker issuance requires the sole synthetic development account")
        pool = AccountPool(pools.runtime, AccountPrincipal(
            account["id"], account["is_enabled"], account["auth_version"]))
        async with pool.connection() as conn:
            issued = [await create_device(conn, label) for label in ("test", "postrestore-check")]
        print(json.dumps([[item.username, item.secret] for item in issued]))

asyncio.run(main())
PYEOF
    then
        step_fail "step 2: could not issue synthetic tracking credentials"
    fi
    chmod 600 "$credentials"
    TEST_TRACKING_USERNAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0][0])' "$credentials")"
    TEST_TRACKING_SECRET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[0][1])' "$credentials")"
    INGEST_USERNAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[1][0])' "$credentials")"
    INGEST_PASSWORD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[1][1])' "$credentials")"
    rm -f -- "$credentials"
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
    local base_image candidate_image base_without_image candidate_without_image
    capture_db_service_config "$BASE_DIR" "$SCRATCH/base-db-service.yml"
    capture_db_service_config "$CAND_DIR" "$SCRATCH/candidate-db-service.yml"
    if diff -u "$SCRATCH/base-db-service.yml" "$SCRATCH/candidate-db-service.yml" \
        > "$SCRATCH/db-service-diff.txt"; then
        if [ "$DATABASE_IMAGE_MIGRATION" -eq 1 ]; then
            echo "error: --database-image-migration requires a changed db image" >&2
            return 1
        fi
        step_pass "step 1: base/candidate rendered db service definitions match"
        return 0
    fi

    if [ "$DATABASE_IMAGE_MIGRATION" -ne 1 ]; then
        echo "error: base and candidate rendered db service definitions differ:" >&2
        cat "$SCRATCH/db-service-diff.txt" >&2
        echo "error: this generic drill requires a stable db service; use a release-specific operations migration plan." >&2
        return 1
    fi

    if ! base_image="$(sed -nE 's/^[[:space:]]+image:[[:space:]]*//p' "$SCRATCH/base-db-service.yml")" \
        || ! candidate_image="$(sed -nE 's/^[[:space:]]+image:[[:space:]]*//p' "$SCRATCH/candidate-db-service.yml")" \
        || [ -z "$base_image" ] || [ -z "$candidate_image" ] \
        || [ "$(printf '%s\n' "$base_image" | wc -l | tr -d '[:space:]')" -ne 1 ] \
        || [ "$(printf '%s\n' "$candidate_image" | wc -l | tr -d '[:space:]')" -ne 1 ]; then
        echo "error: --database-image-migration requires one rendered db image in each service" >&2
        return 1
    fi
    base_without_image="$SCRATCH/base-db-service-without-image.yml"
    candidate_without_image="$SCRATCH/candidate-db-service-without-image.yml"
    sed -E '/^[[:space:]]+image:[[:space:]]*/d' "$SCRATCH/base-db-service.yml" > "$base_without_image"
    sed -E '/^[[:space:]]+image:[[:space:]]*/d' "$SCRATCH/candidate-db-service.yml" > "$candidate_without_image"
    if ! diff -u "$base_without_image" "$candidate_without_image" \
        > "$SCRATCH/db-service-diff.txt"; then
        echo "error: --database-image-migration permits only the rendered db image change; other db service definitions differ:" >&2
        cat "$SCRATCH/db-service-diff.txt" >&2
        return 1
    fi
    if [ "$base_image" = "$candidate_image" ]; then
        echo "error: --database-image-migration was requested but the rendered db image did not change" >&2
        return 1
    fi
    step_pass "step 1: rendered db service differs only by image ($base_image -> $candidate_image)"
}

capture_db_image_identity() {
    local outfile="$2" container_id image_id
    # Both Compose frontends label services; podman-compose ps has no service filter.
    container_id="$($runtime ps -q --filter "label=com.docker.compose.project=$PROJECT" \
        --filter label=com.docker.compose.service=db)" || return 1
    if [ -z "$container_id" ] || [ "$(printf '%s\n' "$container_id" | wc -l | tr -d '[:space:]')" -ne 1 ]; then
        echo "error: expected exactly one running db container for project '$PROJECT'" >&2
        return 1
    fi
    image_id="$($runtime inspect --format '{{.Image}}' "$container_id" | tr -d '[:space:]')" || return 1
    [ -n "$image_id" ] || {
        echo "error: no db image identity was returned for container '$container_id'" >&2
        return 1
    }
    {
        printf 'container_id=%s\n' "$container_id"
        printf 'image_id=%s\n' "$image_id"
    } > "$outfile"
}

db_image_identity_value() {
    sed -n 's/^image_id=//p' "$1"
}

assert_db_image_identity_equal() {
    local expected_file="$1" actual_file="$2" description="$3"
    local expected actual
    expected="$(db_image_identity_value "$expected_file")"
    actual="$(db_image_identity_value "$actual_file")"
    [ -n "$expected" ] || step_fail "$description: expected db image identity is empty"
    [ -n "$actual" ] || step_fail "$description: actual db image identity is empty"
    [ "$expected" = "$actual" ] || step_fail "$description: expected image '$expected', got '$actual'"
    step_pass "$description: image identity $actual"
}

# --- drill steps -------------------------------------------------------

step_materialize() {
    git -C "$REPO_ROOT" archive "$BASE_REF" | tar -x -C "$BASE_DIR"
    git -C "$REPO_ROOT" archive "$CANDIDATE_REF" | tar -x -C "$CAND_DIR"
    step_pass "materialized base ($BASE_REF -> $BASE_DIR) and candidate ($CANDIDATE_REF -> $CAND_DIR)"
}

step1_base_up() {
    (cd "$BASE_DIR" && ./scripts/generate_env.sh) >/dev/null
    OIDC_CLIENT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    set_env_value "$BASE_DIR/.env" OIDC_ISSUER "$OIDC_ISSUER"
    set_env_value "$BASE_DIR/.env" OIDC_CLIENT_ID "$OIDC_CLIENT_ID"
    set_env_value "$BASE_DIR/.env" OIDC_CLIENT_SECRET "$OIDC_CLIENT_SECRET"
    cp "$BASE_DIR/.env" "$CAND_DIR/.env"
    auth_env_matches \
        || step_fail "step 1: complete synthetic OIDC configuration was not preserved into the candidate environment"

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

    if [ -n "$BASE_IMAGE" ]; then
        local base_image_quoted
        base_image_quoted="${BASE_IMAGE//\'/\'\'}"
        cat > "$BASE_DIR/compose.base-image.override.yml" <<EOF
services:
  app:
    image: '${base_image_quoted}'
    pull_policy: always
EOF
    fi
    if [ -n "$CANDIDATE_IMAGE" ]; then
        write_candidate_image_override
    fi
    assert_stable_db_service \
        || step_fail "step 1: base/candidate db service definitions are not stable"

    start_base_app db app
    wait_for_healthz 240 || step_fail "step 1: base install did not become healthy at ${BASE_URL}/healthz"
    if [ "$DATABASE_IMAGE_MIGRATION" -eq 1 ]; then
        capture_db_image_identity "$BASE_DIR" "$SCRATCH/base-db-before-migration.txt" \
            || step_fail "step 1: could not capture the original base db image identity"
        step_pass "step 1: captured original base db container/image identity"
    fi
    oidc_env_loaded "$BASE_DIR" \
        || step_fail "step 1: base app did not receive the complete synthetic OIDC configuration"
    step_pass "step 1: base ($BASE_REF) up and healthy with complete synthetic OIDC configuration at ${BASE_URL}"
}

step2_seed() {
    local admin_token initial_admin_signup bootstrap_mode seed_url seed_python account_count
    if admin_token="$(env_value "$BASE_DIR/.env" ADMIN_TOKEN 2>/dev/null)"; then
        :
    else
        admin_token=""
    fi
    if initial_admin_signup="$(env_value "$BASE_DIR/.env" INITIAL_ADMIN_SIGNUP 2>/dev/null)"; then
        :
    else
        initial_admin_signup=""
    fi
    if [ -n "$admin_token" ]; then
        bootstrap_mode="setup"
    elif [ "$initial_admin_signup" = "1" ]; then
        bootstrap_mode="signup"
    else
        step_fail "step 2: base provides no scriptable administrator bootstrap (need ADMIN_TOKEN or INITIAL_ADMIN_SIGNUP=1)"
    fi
    if ! POSTGRES_PASSWORD="$(env_value "$BASE_DIR/.env" POSTGRES_PASSWORD)"; then
        step_fail "step 2: missing required POSTGRES_PASSWORD in base environment"
    fi
    BASE_SCHEMA_VERSION="$(schema_version "$BASE_DIR")"
    if [ "$BASE_SCHEMA_VERSION" -lt 26 ] && ! INGEST_PASSWORD="$(env_value "$BASE_DIR/.env" INGEST_PASSWORD)"; then
        step_fail "step 2: missing required INGEST_PASSWORD in base environment"
    fi
    if [ "$BASE_SCHEMA_VERSION" -lt 26 ]; then
        INGEST_USERNAME="$(env_value "$BASE_DIR/.env" INGEST_USERNAME 2>/dev/null || printf owntracks)"
    fi
    ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"

    if [ "$bootstrap_mode" = "setup" ]; then
        setup_local_admin "$admin_token" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" \
            || step_fail "step 2: /setup did not create the local administrator"
        login_page_offers_oidc \
            || step_fail "step 2: legacy base login did not offer OIDC with complete configuration"
        step_pass "step 2a: local admin created via the token-gated /setup flow and legacy OIDC login is offered"
    else
        signup_local_admin "$ADMIN_EMAIL" "$ADMIN_PASSWORD" \
            || step_fail "step 2: /signup did not create an authenticated local administrator session"
        account_count="$(db_query "$BASE_DIR" \
            "SELECT count(*) FROM accounts WHERE email = '${ADMIN_EMAIL}' AND password_hash IS NOT NULL")"
        [ "$account_count" = "1" ] \
            || step_fail "step 2: /signup did not create the administrator account exactly once"
        step_pass "step 2a: local admin created via the capability-gated /signup flow and authenticated session confirmed"
    fi

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
    if [ "$BASE_SCHEMA_VERSION" -ge 26 ]; then
        issue_synthetic_tracking_credentials
        BASE_URL="$BASE_URL" ODOGRAPH_TRACKING_USERNAME="$TEST_TRACKING_USERNAME" \
            ODOGRAPH_TRACKING_SECRET="$TEST_TRACKING_SECRET" "$BASE_DIR/scripts/send_test_track.sh" \
            || step_fail "step 2: send_test_track.sh failed with the issued test-device login"
    else
        BASE_URL="$BASE_URL" INGEST_PASSWORD="$INGEST_PASSWORD" "$BASE_DIR/scripts/send_test_track.sh" \
            || step_fail "step 2: send_test_track.sh failed"
    fi
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
    run_install_script "$BASE_DIR" "$CAND_DIR/scripts/backup_database.sh" --output "$BACKUP_ARCHIVE" \
        || step_fail "step 4: backup_database.sh failed"
    run_install_script "$BASE_DIR" "$CAND_DIR/scripts/restore_database.sh" --verify-only "$BACKUP_ARCHIVE" \
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
    run_install_script "$BASE_DIR" "$CAND_DIR/scripts/restore_database.sh" "$BACKUP_ARCHIVE" \
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
    start_base_app app
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

step7_database_image_migration() {
    local base_image candidate_image
    compose_base stop app
    run_install_script "$BASE_DIR" "$CAND_DIR/scripts/backup_database.sh" --output "$MIGRATION_BACKUP_ARCHIVE" \
        || step_fail "step 7: post-ingest backup_database.sh failed before database-image migration"
    run_install_script "$BASE_DIR" "$CAND_DIR/scripts/restore_database.sh" --verify-only "$MIGRATION_BACKUP_ARCHIVE" \
        || step_fail "step 7: post-ingest restore_database.sh --verify-only rejected the migration archive"
    step_pass "step 7: post-ingest backup produced and verified before replacing the task-owned db volume"

    compose_base down
    remove_stamp_volumes
    compose_cand up -d db \
        || step_fail "step 7: candidate-image db failed to start on the fresh volume"
    wait_for_pg_ready "$CAND_DIR" 180 || step_fail "step 7: candidate-image db did not become ready on a fresh volume"
    capture_db_image_identity "$CAND_DIR" "$SCRATCH/candidate-db-after-migration.txt" \
        || step_fail "step 7: could not capture candidate db image identity"
    base_image="$(db_image_identity_value "$SCRATCH/base-db-before-migration.txt")"
    candidate_image="$(db_image_identity_value "$SCRATCH/candidate-db-after-migration.txt")"
    [ -n "$base_image" ] || step_fail "step 7: original base db image identity is empty"
    [ -n "$candidate_image" ] || step_fail "step 7: candidate db image identity is empty"
    [ "$base_image" != "$candidate_image" ] \
        || step_fail "step 7: candidate db container is still using the original image identity"
    step_pass "step 7: candidate db image identity differs from the original base image ($base_image -> $candidate_image)"

    run_install_script "$CAND_DIR" "$CAND_DIR/scripts/restore_database.sh" "$MIGRATION_BACKUP_ARCHIVE" \
        || step_fail "step 7: candidate restore_database.sh failed on the fresh candidate-image volume"
    capture_manifest "$CAND_DIR" "$SCRATCH/manifest-after-database-image-restore.txt"
    assert_data_manifests_equal "$SCRATCH/manifest-after-ingest.txt" \
        "$SCRATCH/manifest-after-database-image-restore.txt" \
        "step 7a: candidate image fresh-volume restore preserved the post-ingest data"

}

step7_upgrade() {
    local v mig account_count identity_count
    if [ "$DATABASE_IMAGE_MIGRATION" -eq 1 ]; then
        step7_database_image_migration
    else
        compose_base stop app
    fi
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
        "step 7a: candidate healthy, schema_version=$v matches migration count, data unchanged"
    verify_ownership_upgrade "$CAND_DIR" "$v"

    oidc_env_loaded "$CAND_DIR" \
        || step_fail "step 7: candidate app did not receive the preserved OIDC configuration"
    account_count="$(db_query "$CAND_DIR" \
        "SELECT count(*) FROM accounts WHERE email = '${ADMIN_EMAIL}' AND password_hash IS NOT NULL")"
    [ "$account_count" = "1" ] \
        || step_fail "step 7: migrated local administrator account was not present exactly once"
    identity_count="$(db_query "$CAND_DIR" "SELECT count(*) FROM oidc_identities")"
    [ "$identity_count" = "0" ] \
        || step_fail "step 7: migrated local administrator unexpectedly started with a linked OIDC identity"
    if login_page_offers_oidc; then
        step_fail "step 7: configured but unlinked OIDC was incorrectly offered as a sign-in option"
    fi
    login_local_admin "$ADMIN_EMAIL" "$ADMIN_PASSWORD" \
        || step_fail "step 7: migrated original local password did not sign in"
    step_pass "step 7b: original local password works; preserved OIDC configuration remains unavailable until explicitly linked"

    db_query "$CAND_DIR" \
        "INSERT INTO oidc_identities (account_id, issuer, subject, provider_email, provider_display_name) SELECT id, '${OIDC_ISSUER}', '${OIDC_SUBJECT}', '${OIDC_EMAIL}', 'Upgrade drill identity' FROM accounts WHERE email = '${ADMIN_EMAIL}'" \
        >/dev/null
    identity_count="$(db_query "$CAND_DIR" \
        "SELECT count(*) FROM oidc_identities oi JOIN accounts a ON a.id = oi.account_id WHERE a.email = '${ADMIN_EMAIL}' AND oi.issuer = '${OIDC_ISSUER}' AND oi.subject = '${OIDC_SUBJECT}'")"
    [ "$identity_count" = "1" ] \
        || step_fail "step 7: explicit candidate-schema OIDC identity link was not stored exactly once"
    login_page_offers_oidc \
        || step_fail "step 7: stored OIDC identity was not offered as a candidate sign-in option"
    step_pass "step 7c: representative OIDC identity linked through the candidate schema and offered for sign-in; no external provider contacted"

    ROTATED_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    rotate_candidate_password \
        || step_fail "step 7: supported account recovery command did not rotate the local password"
    login_local_admin "$ADMIN_EMAIL" "$ROTATED_PASSWORD" \
        || step_fail "step 7: rotated local password did not sign in"
    if login_local_admin "$ADMIN_EMAIL" "$ADMIN_PASSWORD"; then
        step_fail "step 7: original local password still signed in after rotation"
    fi
    step_pass "step 7d: supported account recovery command rotated the password; the new password works and the original fails"
}

step8_rollback() {
    local v mig has_accounts oidc_relation local_admin_relation local_admin_count
    local base_image candidate_image
    compose_base down
    remove_stamp_volumes
    compose_base up -d db
    # Same fresh-volume PostGIS init time as step 5's wait_for_pg_ready call.
    wait_for_pg_ready "$BASE_DIR" 180 || step_fail "step 8: fresh db did not become ready"
    if [ "$DATABASE_IMAGE_MIGRATION" -eq 1 ]; then
        capture_db_image_identity "$BASE_DIR" "$SCRATCH/base-db-after-rollback.txt" \
            || step_fail "step 8: could not capture the restored base db image identity"
        assert_db_image_identity_equal "$SCRATCH/base-db-before-migration.txt" \
            "$SCRATCH/base-db-after-rollback.txt" \
            "step 8: fresh rollback db uses the original base image"
        base_image="$(db_image_identity_value "$SCRATCH/base-db-after-rollback.txt")"
        candidate_image="$(db_image_identity_value "$SCRATCH/candidate-db-after-migration.txt")"
        [ "$base_image" != "$candidate_image" ] \
            || step_fail "step 8: rollback db image identity unexpectedly matches the candidate image"
    fi
    run_install_script "$BASE_DIR" "$CAND_DIR/scripts/restore_database.sh" "$BACKUP_ARCHIVE" \
        || step_fail "step 8: restore_database.sh failed restoring the pre-upgrade archive"

    start_base_app app
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
        "step 8a: rollback restored the prior release + pre-upgrade data; the post-backup ingest write was lost as documented"

    login_local_admin "$ADMIN_EMAIL" "$ADMIN_PASSWORD" \
        || step_fail "step 8: original local password did not work after rollback"
    if login_local_admin "$ADMIN_EMAIL" "$ROTATED_PASSWORD"; then
        step_fail "step 8: candidate-rotated password incorrectly worked after rollback"
    fi
    has_accounts="$(db_query "$BASE_DIR" "SELECT to_regclass('accounts') IS NOT NULL")"
    if [ "$has_accounts" = "t" ]; then
        oidc_relation="$(db_query "$BASE_DIR" "SELECT to_regclass('oidc_identities') IS NOT NULL")"
        [ "$oidc_relation" = "t" ] \
            || step_fail "step 8: restored modern auth schema is missing the oidc_identities relation"
        local_admin_relation="$(db_query "$BASE_DIR" "SELECT to_regclass('local_admin') IS NULL")"
        [ "$local_admin_relation" = "t" ] \
            || step_fail "step 8: restored modern auth schema unexpectedly retained the local_admin relation"
        if login_page_offers_oidc; then
            step_fail "step 8: rolled-back modern login offered OIDC without a linked identity"
        fi
        step_pass "step 8b: modern accounts schema and local password restored; rotated password and candidate-only OIDC state absent"
    else
        [ "$(db_query "$BASE_DIR" "SELECT to_regclass('accounts') IS NULL")" = "t" ] \
            || step_fail "step 8: candidate-only accounts relation survived rollback"
        [ "$(db_query "$BASE_DIR" "SELECT to_regclass('oidc_identities') IS NULL")" = "t" ] \
            || step_fail "step 8: candidate-only OIDC identity state survived rollback"
        login_page_offers_oidc \
            || step_fail "step 8: rolled-back legacy login did not offer OIDC from the preserved configuration"
        local_admin_count="$(db_query "$BASE_DIR" \
            "SELECT count(*) FROM local_admin WHERE id = 1 AND email = '${ADMIN_EMAIL}' AND password_hash IS NOT NULL")"
        [ "$local_admin_count" = "1" ] \
            || step_fail "step 8: legacy local_admin state was not restored exactly once"
        step_pass "step 8b: original password and legacy OIDC login restored; rotated password and candidate-only auth state absent"
    fi
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
