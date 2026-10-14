#!/usr/bin/env bash
# Operator-facing upgrade rehearsal. Restores a verified
# scripts/backup_database.sh archive into a disposable, network-isolated
# Compose project built from a candidate release, starts that candidate
# against the copy, and writes a summary of migration timings, the startup
# result, row-count changes and relation-set findings.
#
# The archive holds real data, so this script fails closed. Every container,
# volume, network and built image belongs to one unique project
# ("odorehearse<stamp>"), the script refuses to run when anything with that
# name already exists, and cleanup removes only names it positively
# discovered with that prefix. The project network is internal (no egress),
# the generated environment holds no outbound credentials, and the summary
# holds only schema versions, timings, counts, relation and migration names,
# image identities, the port, the environment source and pass/fail findings.
# It never holds row values or log lines.
#
# Usage:
#   scripts/rehearse_upgrade.sh --archive PATH --candidate REF
#       [--candidate-image IMAGE] [--legacy-env FILE] [--health-timeout SECONDS]
#       [--output PATH] [--keep]
#
# Env vars:
#   COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
#   TMPDIR        parent of the scratch directory; the restore renders the whole
#                 archive to SQL there, so it needs room for a full copy
set -euo pipefail
umask 077

# Keys migration 026 reads from the environment for account preferences
# (app.account_settings.CONFIG_PREFERENCE_COLUMNS, parsed by Config.from_env)
# and legacy mileage-rate overrides (app.ownership.legacy_rate_overrides).
# Nothing else from an operator environment is ever copied.
# tests/test_rehearse_upgrade_script.py checks both against the code.
LEGACY_ENV_PREFERENCE_KEYS=(
    DISPLAY_TZ
    NTFY_TOPIC
    NUDGE_WEEKLY_HOUR
    ODOMETER_REMINDER
    ODOMETER_REMINDER_HOUR
    EMAIL_TO
    EMAIL_WEEKLY_NUDGE
    EMAIL_MONTHLY_SUMMARY
    EMAIL_FILING_REMINDER
    EMAIL_ODOMETER_REMINDER
    EMAIL_DIGEST_HOUR
    EMAIL_FILING_REMINDER_MMDD
)
LEGACY_ENV_RATE_PATTERN='^MILEAGE_RATE_[0-9]+$'

# The only keys the generated environment may set to a non-empty value, on top
# of the legacy allowlist above. Anything else (SMTP, geocoding, routing,
# OIDC, ntfy, admin token, provider settings) makes the rehearsal refuse.
SYNTHETIC_ENV_KEYS=(
    POSTGRES_PASSWORD
    INGEST_PASSWORD
    SESSION_SECRET
    INITIAL_ADMIN_SIGNUP
    DISPLAY_TZ
    FORWARDED_ALLOW_IPS
)

DRILL_PORT=8077
DEFAULT_HEALTH_TIMEOUT=3600
DB_READY_TIMEOUT=300
POLL_INTERVAL=2
HEALTHZ_PROBE="import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

usage() {
    cat >&2 <<'EOF'
usage: scripts/rehearse_upgrade.sh --archive PATH --candidate REF [--candidate-image IMAGE]
           [--legacy-env FILE] [--health-timeout SECONDS] [--output PATH] [--keep]

Restores a verified backup archive into a disposable, network-isolated Compose
project built from a candidate release, starts the candidate against that copy
and writes a summary without data values. Never touches the operator's
installation, any other Compose project, or the repository working tree.

  --archive PATH          scripts/backup_database.sh archive (with its .sha256)
  --candidate REF         git commit-ish supplying Compose and operational scripts;
                          its app is built from source unless --candidate-image
  --candidate-image IMAGE published candidate app image with an explicit
                          non-latest tag or a full sha256 digest
  --legacy-env FILE       file of allowlisted preference and MILEAGE_RATE_<YEAR>
                          keys for a pre-schema-26 archive; any other non-empty
                          key is refused
  --health-timeout SECONDS
                          wait this long for startup and migrations (default 3600)
  --output PATH           summary file to create (default: printed task-owned path)
  --keep                  leave the project, volume and scratch in place
EOF
    exit "${1:-1}"
}

ARCHIVE=""
CANDIDATE_REF=""
CANDIDATE_IMAGE=""
LEGACY_ENV=""
OUTPUT=""
HEALTH_TIMEOUT="$DEFAULT_HEALTH_TIMEOUT"
KEEP=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --archive)
            [ "$#" -ge 2 ] || { echo "error: --archive requires a value" >&2; usage; }
            ARCHIVE="$2"; shift 2 ;;
        --candidate)
            [ "$#" -ge 2 ] || { echo "error: --candidate requires a value" >&2; usage; }
            CANDIDATE_REF="$2"; shift 2 ;;
        --candidate-image)
            [ "$#" -ge 2 ] || { echo "error: --candidate-image requires a value" >&2; usage; }
            CANDIDATE_IMAGE="$2"; shift 2 ;;
        --legacy-env)
            [ "$#" -ge 2 ] || { echo "error: --legacy-env requires a value" >&2; usage; }
            LEGACY_ENV="$2"; shift 2 ;;
        --health-timeout)
            [ "$#" -ge 2 ] || { echo "error: --health-timeout requires a value" >&2; usage; }
            HEALTH_TIMEOUT="$2"; shift 2 ;;
        --output)
            [ "$#" -ge 2 ] || { echo "error: --output requires a value" >&2; usage; }
            OUTPUT="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "error: unrecognized argument: $1" >&2; usage ;;
    esac
done
if [ -z "$ARCHIVE" ] || [ -z "$CANDIDATE_REF" ]; then
    echo "error: --archive and --candidate are both required" >&2
    usage
fi
if [[ ! "$HEALTH_TIMEOUT" =~ ^[1-9][0-9]{0,6}$ ]]; then
    echo "error: --health-timeout must be a positive whole number of seconds" >&2
    usage
fi

immutable_image_ref() {
    local ref="$1" tag
    if [[ "$ref" =~ ^[^[:space:]@]+@sha256:[[:xdigit:]]{64}$ ]]; then
        return 0
    fi
    if [[ "$ref" =~ ^[^[:space:]@]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
        tag="${ref##*:}"
        # tr rather than "${tag,,}", which is a syntax error in macOS bash 3.2.
        [ "$(printf '%s' "$tag" | tr '[:upper:]' '[:lower:]')" != "latest" ] && return 0
    fi
    return 1
}

if [ -n "$CANDIDATE_IMAGE" ] && ! immutable_image_ref "$CANDIDATE_IMAGE"; then
    echo "error: --candidate-image must use an explicit non-latest tag or a full sha256 digest" >&2
    usage
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Shell values that Compose would otherwise use to pick a project, extra
# files or profiles, or to interpolate into the rendered services.
unset COMPOSE_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR POSTGRES_PASSWORD FORWARDED_ALLOW_IPS

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

runtime_cmd() {
    # Neither Compose frontend exposes volume, network or image listing of
    # its own, so those go through the underlying runtime.
    local first="${compose_cmd%% *}"
    if [ "$first" = "podman-compose" ]; then
        echo "podman"
    else
        echo "$first"
    fi
}

legacy_key_allowed() {
    local key="$1" allowed
    for allowed in "${LEGACY_ENV_PREFERENCE_KEYS[@]}"; do
        [ "$key" = "$allowed" ] && return 0
    done
    [[ "$key" =~ $LEGACY_ENV_RATE_PATTERN ]]
}

# Reads FILE into LEGACY_LINES as KEY=VALUE entries. Error messages name line
# numbers and keys only, never a value.
LEGACY_LINES=()
LEGACY_KEYS=()
parse_legacy_env() {
    local file="$1" line trimmed key value bare lineno=0 rejected="" seen=" "
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        line="${line%$'\r'}"
        trimmed="${line#"${line%%[![:space:]]*}"}"
        case "$trimmed" in ""|"#"*) continue ;; esac
        case "$trimmed" in "export "*) trimmed="${trimmed#export }" ;; esac
        if [[ ! "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            echo "error: --legacy-env line $lineno is not a KEY=VALUE assignment" >&2
            return 1
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        bare="$(printf '%s' "$value" | tr -d '[:space:]')"
        case "$bare" in ""|'""'|"''") continue ;; esac
        if ! legacy_key_allowed "$key"; then
            rejected="$rejected $key"
            continue
        fi
        case "$seen" in
            *" $key "*)
                echo "error: --legacy-env sets $key more than once" >&2
                return 1
                ;;
        esac
        seen="$seen$key "
        case "$value" in
            \"*|\'*)
                if [ "${#value}" -lt 2 ] || [ "${value:0:1}" != "${value: -1}" ]; then
                    echo "error: --legacy-env line $lineno ($key) has an unterminated quoted value" >&2
                    return 1
                fi
                ;;
        esac
        LEGACY_LINES+=("$key=$value")
        LEGACY_KEYS+=("$key")
    done < "$file"
    if [ -n "$rejected" ]; then
        echo "error: --legacy-env sets keys outside the rehearsal allowlist:$rejected" >&2
        echo "Only these keys may be non-empty: ${LEGACY_ENV_PREFERENCE_KEYS[*]} and MILEAGE_RATE_<YEAR>." >&2
        echo "Copy just those lines into a separate file; never pass a full .env." >&2
        return 1
    fi
}

absolute_path() {
    local dir
    dir="$(cd "$(dirname -- "$1")" && pwd -P)" || return 1
    printf '%s/%s\n' "$dir" "$(basename -- "$1")"
}

# --- preconditions: nothing below this point may create anything yet ------

if [ ! -f "$ARCHIVE" ]; then
    echo "error: archive not found: $ARCHIVE" >&2
    exit 1
fi
ARCHIVE="$(absolute_path "$ARCHIVE")"
if [ ! -f "${ARCHIVE}.sha256" ]; then
    echo "error: checksum sidecar not found next to the archive (${ARCHIVE}.sha256)" >&2
    exit 1
fi
if [ -n "$OUTPUT" ]; then
    if [ -e "$OUTPUT" ] || [ -L "$OUTPUT" ]; then
        echo "error: --output $OUTPUT already exists; refusing to overwrite it" >&2
        exit 1
    fi
    if [ ! -d "$(dirname -- "$OUTPUT")" ]; then
        echo "error: the directory for --output $OUTPUT does not exist" >&2
        exit 1
    fi
    OUTPUT="$(absolute_path "$OUTPUT")"
fi
if [ -n "$LEGACY_ENV" ]; then
    if [ ! -f "$LEGACY_ENV" ]; then
        echo "error: --legacy-env file not found: $LEGACY_ENV" >&2
        exit 1
    fi
    parse_legacy_env "$LEGACY_ENV" || exit 1
fi

if ! CANDIDATE_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "${CANDIDATE_REF}^{commit}")"; then
    echo "error: --candidate ref '$CANDIDATE_REF' does not resolve to a commit in $REPO_ROOT" >&2
    exit 1
fi

compose_cmd="$(detect_compose_cmd)"
runtime="$(runtime_cmd)"
if ! "$runtime" info >/dev/null 2>&1; then
    echo "error: container runtime '$runtime' is not responding; start it and re-run." >&2
    exit 1
fi

STAMP="$(date +%s)$(printf '%04d' "$((RANDOM % 10000))")"
PROJECT="odorehearse${STAMP}"

project_exists() {
    local listing
    listing="$("$runtime" ps -aq --filter "label=com.docker.compose.project=$PROJECT")" || return 0
    if [ -n "$listing" ]; then
        echo "error: containers for project '$PROJECT' already exist" >&2
        return 0
    fi
    listing="$("$runtime" volume ls --format '{{.Name}}')" || return 0
    if printf '%s\n' "$listing" | grep -q "^${PROJECT}_"; then
        echo "error: a volume for project '$PROJECT' already exists" >&2
        return 0
    fi
    listing="$("$runtime" network ls --format '{{.Name}}')" || return 0
    if printf '%s\n' "$listing" | grep -q "^${PROJECT}_"; then
        echo "error: a network for project '$PROJECT' already exists" >&2
        return 0
    fi
    return 1
}
if project_exists; then
    echo "error: refusing to run: the disposable project name is not unused (or the runtime could not be queried)" >&2
    exit 1
fi

# Both Compose frontends and the build override take the project from here.
export COMPOSE_PROJECT_NAME="$PROJECT"

HEALTH_PORT=""
find_free_port() {
    python3 - <<'PYEOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF
}
for _ in 1 2 3 4 5; do
    HEALTH_PORT="$(find_free_port)"
    [ "$HEALTH_PORT" != "$DRILL_PORT" ] && break
    HEALTH_PORT=""
done
if [[ ! "$HEALTH_PORT" =~ ^[0-9]+$ ]]; then
    echo "error: could not pick a free loopback port" >&2
    exit 1
fi

if [ -z "$OUTPUT" ]; then
    OUTPUT="${TMPDIR:-/tmp}/${PROJECT}-summary.txt"
    if [ -e "$OUTPUT" ] || [ -L "$OUTPUT" ]; then
        echo "error: default summary path $OUTPUT already exists" >&2
        exit 1
    fi
fi

echo "[PASS] preconditions: archive and checksum present, candidate $CANDIDATE_COMMIT, compose '$compose_cmd', runtime '$runtime', project '$PROJECT' unused"

# --- scratch layout, summary state and teardown trap -----------------------

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/rehearse_upgrade.XXXXXX")"
CAND_DIR="$SCRATCH/$PROJECT"
STATE_DIR="$SCRATCH/state"
LOG_DIR="$SCRATCH/logs"
mkdir -p "$CAND_DIR" "$STATE_DIR" "$SCRATCH/restore-tmp"

FAILED_STEP=""
FINISHED=0
FINDINGS=()
SCHEMA_BEFORE="unknown"
SCHEMA_AFTER="unknown"
LATEST_MIGRATION="unknown"
STARTUP_RESULT="not attempted"
STARTUP_SECONDS="unknown"
VERIFY_RESULT="not run"
LOOPBACK_RESULT="not checked"
DB_IMAGE_REF="unknown"
DB_IMAGE_ID="unknown"
APP_IMAGE_REF="unknown"
APP_IMAGE_ID="unknown"
UNKNOWN_BEFORE=""
HANDLED_BEFORE=""
UNKNOWN_AFTER="not checked"
RAW_LOGS_SAVED=0

add_finding() { FINDINGS+=("$1"); }
step_pass() { echo "[PASS] $1"; }
step_fail() {
    FAILED_STEP="$1"
    echo "[FAIL] $1" >&2
    exit 1
}

env_source() {
    if [ "${#LEGACY_KEYS[@]}" -eq 0 ]; then
        printf 'synthetic'
    else
        printf 'synthetic plus allowlisted legacy keys: %s' "${LEGACY_KEYS[*]}"
    fi
}

write_counts_table() {
    # Joins before/after "relation<TAB>count" files on relation name.
    local before="$STATE_DIR/counts-before.tsv" after="$STATE_DIR/counts-after.tsv"
    [ -f "$before" ] || before=/dev/null
    [ -f "$after" ] || after=/dev/null
    awk -F '\t' '
        FILENAME == ARGV[1] { b[$1] = $2; seen[$1] = 1; next }
        { a[$1] = $2; seen[$1] = 1 }
        END {
            for (n in seen) {
                bv = (n in b) ? b[n] : "-"
                av = (n in a) ? a[n] : "-"
                d = "-"
                if (bv != "-" && av != "-") {
                    d = av - bv
                    if (d > 0) d = "+" d
                }
                printf "  %-36s %12s %12s %12s\n", n, bv, av, d
            }
        }
    ' "$before" "$after" | LC_ALL=C sort
}

write_summary() {
    local file="$1" finding result="PASS"
    if [ -n "$FAILED_STEP" ]; then
        result="FAIL"
    fi
    for finding in ${FINDINGS[@]+"${FINDINGS[@]}"}; do
        case "$finding" in FAIL:*) result="FAIL" ;; esac
    done
    {
        echo "Odograph upgrade rehearsal summary"
        echo "result: $result"
        if [ -n "$FAILED_STEP" ]; then
            echo "stopped at: $FAILED_STEP"
        fi
        echo "project: $PROJECT (disposable, internal network)"
        echo "candidate commit: $CANDIDATE_COMMIT"
        echo "candidate app image: $APP_IMAGE_REF"
        echo "candidate app image id: $APP_IMAGE_ID"
        echo "database image: $DB_IMAGE_REF"
        echo "database image id: $DB_IMAGE_ID"
        echo "health port: 127.0.0.1:$HEALTH_PORT (loopback reachable: $LOOPBACK_RESULT)"
        echo "environment: $(env_source)"
        echo "schema before: $SCHEMA_BEFORE"
        echo "schema after: $SCHEMA_AFTER"
        echo "candidate latest migration: $LATEST_MIGRATION"
        echo "startup: $STARTUP_RESULT"
        echo "start to healthy or failure: ${STARTUP_SECONDS}s"
        echo "role contract verify: $VERIFY_RESULT"
        echo
        echo "migrations (seconds, from container log timestamps):"
        if [ -s "$STATE_DIR/migrations.txt" ]; then
            sed 's/^/  /' "$STATE_DIR/migrations.txt"
        else
            echo "  none recorded"
        fi
        echo
        echo "public relations not in the candidate contract:"
        echo "  before startup, unknown: ${UNKNOWN_BEFORE:-none}"
        echo "  before startup, removed by a pending migration: ${HANDLED_BEFORE:-none}"
        echo "  after startup, unknown: ${UNKNOWN_AFTER:-none}"
        echo
        echo "row counts per public table:"
        printf '  %-36s %12s %12s %12s\n' table before after delta
        write_counts_table
        echo
        echo "findings:"
        if [ "${#FINDINGS[@]}" -eq 0 ]; then
            echo "  none"
        fi
        for finding in ${FINDINGS[@]+"${FINDINGS[@]}"}; do
            echo "  $finding"
        done
        if [ "$RAW_LOGS_SAVED" -eq 1 ]; then
            echo
            echo "raw container logs: $LOG_DIR"
            echo "WARNING: raw logs may contain personal data. Delete them with the scratch directory."
        fi
    } > "$file"
}

# Files are passed identically to every invocation, so Compose never sees a
# changed file set and recreates a service.
compose_files() {
    local files="compose.yaml"
    if [ -n "$CANDIDATE_IMAGE" ]; then
        files="$files:compose.candidate-image.override.yml"
    elif [ -f "$CAND_DIR/compose.build.override.yml" ]; then
        files="$files:compose.build.override.yml"
    fi
    printf '%s:compose.rehearsal.override.yml\n' "$files"
}

# $compose_cmd is deliberately unquoted so "docker compose" splits in two.
compose_cand() {
    local args=() files=() file
    IFS=: read -r -a files <<< "$(compose_files)"
    for file in "${files[@]}"; do
        args+=(-f "$file")
    done
    (cd "$CAND_DIR" && $compose_cmd "${args[@]}" "$@")
}

# The candidate's backup/restore scripts run Compose themselves; give them the
# same file set and project.
run_install_script() {
    (cd "$CAND_DIR" && COMPOSE_FILE="$(compose_files)" COMPOSE_PATH_SEPARATOR=: \
        COMPOSE_CMD="$compose_cmd" TMPDIR="$SCRATCH/restore-tmp" "$@")
}

remove_project_containers() {
    # Safety net when "down" fails: only containers carrying this exact
    # project label.
    local ids
    ids="$("$runtime" ps -aq --filter "label=com.docker.compose.project=$PROJECT")" || return 1
    [ -n "$ids" ] || return 0
    # shellcheck disable=SC2086 # one id per word
    "$runtime" rm -f $ids >/dev/null
}

remove_project_volumes() {
    local matches vol
    matches="$("$runtime" volume ls --format '{{.Name}}')" || return 1
    matches="$(printf '%s\n' "$matches" | grep -E "^${PROJECT}_" || true)"
    [ -n "$matches" ] || return 0
    while IFS= read -r vol; do
        [ -z "$vol" ] && continue
        case "$vol" in
            "${PROJECT}_"*) ;;
            *) echo "error: refusing to remove volume '$vol'" >&2; return 1 ;;
        esac
        echo "Removing disposable volume: $vol"
        "$runtime" volume rm "$vol" >/dev/null || return 1
    done <<< "$matches"
}

remove_project_networks() {
    local matches net
    matches="$("$runtime" network ls --format '{{.Name}}')" || return 1
    matches="$(printf '%s\n' "$matches" | grep -E "^${PROJECT}_" || true)"
    [ -n "$matches" ] || return 0
    while IFS= read -r net; do
        [ -z "$net" ] && continue
        echo "Removing disposable network: $net"
        "$runtime" network rm "$net" >/dev/null 2>&1 || true
    done <<< "$matches"
}

remove_project_images() {
    # Only this project's own source-build repository. A published
    # --candidate-image and the database image are never removed.
    local matches img
    matches="$("$runtime" image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | LC_ALL=C sort -u || true)"
    [ -n "$matches" ] || return 0
    while IFS= read -r img; do
        [ -z "$img" ] && continue
        case "${img%:*}" in
            "localhost/${PROJECT}_app"|"localhost/${PROJECT}-app"|"${PROJECT}_app"|"${PROJECT}-app") ;;
            *) continue ;;
        esac
        case "${img##*:}" in "<none>"|"") continue ;; esac
        echo "Removing disposable image: $img"
        "$runtime" rmi "$img" >/dev/null 2>&1 || true
    done <<< "$matches"
}

cleanup() {
    local exit_code=$?
    trap - EXIT
    if [ -z "$FAILED_STEP" ] && [ "$FINISHED" -ne 1 ] && [ "$exit_code" -ne 0 ]; then
        FAILED_STEP="interrupted or unexpected error (exit $exit_code)"
    fi
    if write_summary "$STATE_DIR/summary.txt" && (set -C; cat "$STATE_DIR/summary.txt" > "$OUTPUT"); then
        echo
        cat "$OUTPUT"
        echo
        echo "Summary written to: $OUTPUT"
    else
        echo "error: could not write the summary to $OUTPUT" >&2
        [ "$exit_code" -ne 0 ] || exit_code=1
    fi
    if [ "$KEEP" -eq 1 ]; then
        echo
        echo "--keep given: leaving the project, volume and scratch in place."
        echo "  project:  $PROJECT"
        echo "  scratch:  $SCRATCH (holds a restored copy's environment; may hold raw logs)"
        echo "  manual teardown: (cd '$CAND_DIR' && COMPOSE_PROJECT_NAME='$PROJECT' COMPOSE_FILE='$(compose_files)' COMPOSE_PATH_SEPARATOR=: $compose_cmd down),"
        echo "  then remove volumes and networks beginning with '${PROJECT}_', any '${PROJECT}' app image,"
        echo "  and: rm -rf '$SCRATCH'"
        exit "$exit_code"
    fi
    if [ -f "$CAND_DIR/compose.rehearsal.override.yml" ]; then
        compose_cand down >/dev/null 2>&1 || true
    fi
    remove_project_containers || echo "error: could not remove every '$PROJECT' container; remove them by hand" >&2
    remove_project_volumes || echo "error: could not remove every '${PROJECT}_' volume; remove it by hand" >&2
    remove_project_networks || true
    remove_project_images || true
    rm -rf -- "$SCRATCH"
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# --- database helpers ---------------------------------------------------

db_query() {
    compose_cand exec -T db psql -X -v ON_ERROR_STOP=1 -U mileage -d mileage -Atc "$1"
}

# Matches the relation set app.application_roles.validate_application_contract
# compares at startup: public tables, views, materialized views and foreign
# tables that no extension owns. Sequences are not part of that comparison.
RELATION_FILTER="n.nspname='public' AND c.relkind IN ('r','p','v','m','f') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')"

capture_schema_version() {
    local present version
    present="$(db_query "SELECT to_regclass('public.schema_migrations') IS NOT NULL")" || return 1
    case "$present" in
        f) printf '0\n'; return 0 ;;
        t) ;;
        *) return 1 ;;
    esac
    version="$(db_query "SELECT COALESCE(max(version), 0) FROM schema_migrations")" || return 1
    [[ "$version" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$version"
}

capture_relations() {
    local outfile="$1" raw expected
    raw="$(db_query "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE $RELATION_FILTER ORDER BY c.relname")" || return 1
    expected="$(db_query "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE $RELATION_FILTER")" || return 1
    [[ "$expected" =~ ^[0-9]+$ ]] || return 1
    if [ -z "$raw" ]; then
        [ "$expected" = 0 ] || return 1
        : > "$outfile"
        return 0
    fi
    # A name holding a newline would split into two rows; refuse rather than
    # report a wrong relation set.
    [ "$(printf '%s\n' "$raw" | wc -l | tr -d '[:space:]')" = "$expected" ] || return 1
    printf '%s\n' "$raw" > "$outfile"
}

# Counts every public table server-side. Names are quoted by format(%I), so
# no relation name is ever interpolated into SQL by this script.
capture_counts() {
    local outfile="$1" raw expected
    raw="$(db_query "SELECT c.relname || E'\\t' || (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e') ORDER BY c.relname")" || return 1
    expected="$(db_query "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')")" || return 1
    [[ "$expected" =~ ^[0-9]+$ ]] || return 1
    if [ -z "$raw" ]; then
        [ "$expected" = 0 ] || return 1
        : > "$outfile"
        return 0
    fi
    printf '%s\n' "$raw" | awk -F '\t' -v expected="$expected" '
        NF != 2 || $2 !~ /^[0-9]+$/ { bad = 1 }
        { rows++ }
        END { exit (bad || rows != expected) }
    ' || return 1
    printf '%s\n' "$raw" > "$outfile"
}

container_id_for() {
    local service="$1" ids
    ids="$("$runtime" ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=$service")" || return 1
    if [ -z "$ids" ] || [ "$(printf '%s\n' "$ids" | wc -l | tr -d '[:space:]')" -ne 1 ]; then
        echo "error: expected exactly one $service container for project '$PROJECT'" >&2
        return 1
    fi
    printf '%s\n' "$ids"
}

image_id_for() {
    local id image
    id="$(container_id_for "$1")" || return 1
    image="$("$runtime" inspect --format '{{.Image}}' "$id" | tr -d '[:space:]')" || return 1
    [ -n "$image" ] || return 1
    printf '%s\n' "$image"
}

wait_for_pg_ready() {
    local deadline
    deadline=$(( $(date +%s) + DB_READY_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        # TCP rather than socket: the fresh-volume init server listens only
        # on the socket and stops before the real server starts.
        if compose_cand exec -T db pg_isready -h 127.0.0.1 -p 5432 -U mileage -d mileage >/dev/null 2>&1; then
            return 0
        fi
        sleep "$POLL_INTERVAL"
    done
    return 1
}

# --- rehearsal steps ---------------------------------------------------

step_materialize() {
    git -C "$REPO_ROOT" archive "$CANDIDATE_COMMIT" | tar -x -C "$CAND_DIR" \
        || step_fail "step 1: could not materialize the candidate tree"
    step_pass "step 1: materialized candidate $CANDIDATE_COMMIT"
}

set_env_value() {
    local file="$1" key="$2" value="$3" updated
    updated="$(mktemp "$STATE_DIR/env.XXXXXX")"
    awk -v key="$key" 'index($0, key "=") != 1 { print }' "$file" > "$updated"
    printf '%s=%s\n' "$key" "$value" >> "$updated"
    chmod 600 "$updated"
    mv "$updated" "$file"
}

synthetic_key_allowed() {
    local key="$1" allowed
    for allowed in "${SYNTHETIC_ENV_KEYS[@]}"; do
        [ "$key" = "$allowed" ] && return 0
    done
    legacy_key_allowed "$key"
}

# Every non-empty assignment in the generated .env must be a synthetic or
# allowlisted legacy key, so no outbound credential can reach the workers.
assert_env_allowlisted() {
    local line key value bare refused=""
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ""|"#"*) continue ;; esac
        [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || { refused="$refused (malformed line)"; continue; }
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        bare="$(printf '%s' "$value" | tr -d '[:space:]')"
        case "$bare" in ""|'""'|"''") continue ;; esac
        synthetic_key_allowed "$key" || refused="$refused $key"
    done < "$CAND_DIR/.env"
    if [ -n "$refused" ]; then
        echo "error: the generated environment sets keys outside the rehearsal allowlist:$refused" >&2
        return 1
    fi
}

step_environment() {
    local entry
    (cd "$CAND_DIR" && ./scripts/generate_env.sh) >/dev/null \
        || step_fail "step 2: generate_env.sh failed"
    for entry in ${LEGACY_LINES[@]+"${LEGACY_LINES[@]}"}; do
        set_env_value "$CAND_DIR/.env" "${entry%%=*}" "${entry#*=}"
    done
    assert_env_allowlisted || step_fail "step 2: generated environment is not limited to synthetic and allowlisted keys"
    step_pass "step 2: synthetic environment generated ($(env_source))"
}

write_overrides() {
    # Written first: cleanup runs "down" once the rehearsal override exists,
    # and every file it names must exist by then.
    if [ -n "$CANDIDATE_IMAGE" ]; then
        local image_quoted="${CANDIDATE_IMAGE//\'/\'\'}"
        cat > "$CAND_DIR/compose.candidate-image.override.yml" <<EOF
services:
  app:
    image: '${image_quoted}'
    pull_policy: always
EOF
    fi
    # !override replaces the canonical 8077 publication instead of adding to
    # it. restart "no" makes a failed startup exit instead of re-running
    # migrations in a loop.
    cat > "$CAND_DIR/compose.rehearsal.override.yml" <<EOF
services:
  app:
    ports: !override
      - "127.0.0.1:${HEALTH_PORT}:8000"
    restart: "no"
  db:
    restart: "no"
networks:
  default:
    internal: true
EOF
}

# Checks the rendered configuration rather than trusting the override: one
# loopback publication of the chosen port on app only, no publication
# elsewhere, an internal default network, and no restart policy. Prints the
# rendered db image.
check_rendered_config() {
    local rendered="$STATE_DIR/compose-config.yml"
    compose_cand config > "$rendered" || return 1
    python3 - "$rendered" "$HEALTH_PORT" <<'PYEOF'
import re
import sys

lines = [line.rstrip() for line in open(sys.argv[1]).read().splitlines()]
port = sys.argv[2]


def fail(message):
    print("rendered Compose configuration: " + message, file=sys.stderr)
    raise SystemExit(1)


def indent(line):
    return len(line) - len(line.lstrip())


def mapping(block):
    """Top-level keys of an indented block, each with its child lines."""
    keyed = [line for line in block if line.strip()]
    if not keyed:
        return {}
    level = min(indent(line) for line in keyed)
    result, current = {}, None
    for line in block:
        # A list may sit at its key's own indent ("ports:" then "- ...").
        if line.strip() and indent(line) == level and not line.strip().startswith("- "):
            key, _, value = line.strip().partition(":")
            current = [value.strip()] if value.strip() else []
            if key in result:
                fail(f"duplicate key {key}")
            result[key] = current
        elif current is not None:
            current.append(line)
    return result


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def scalar(block, key):
    values = block.get(key)
    if values is None or len(values) != 1 or values[0].startswith(" "):
        return None
    return unquote(values[0])


root = mapping(lines)
services = mapping(root.get("services") or [])
if "app" not in services or "db" not in services:
    fail("app and db services are both required")

for name, body in services.items():
    block = mapping(body)
    if name != "app":
        if "ports" in block:
            fail(f"service {name} publishes a port")
        continue
    items, current = [], None
    for line in block.get("ports") or []:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            current = [stripped[2:]]
            items.append(current)
        elif current is not None:
            current.append(stripped)
        else:
            fail("unreadable app port list")
    if len(items) != 1:
        fail("app must publish exactly one port")
    expected = f"127.0.0.1:{port}:8000"
    item = items[0]
    if len(item) == 1 and not re.match(r"^[a-z_]+:\s", item[0]):
        if unquote(item[0]) not in (expected, expected + "/tcp"):
            fail("app port is not the chosen loopback health port")
    else:
        fields = {}
        for entry in item:
            key, _, value = entry.partition(":")
            fields[key.strip()] = unquote(value)
        if (fields.get("host_ip") != "127.0.0.1" or fields.get("target") != "8000"
                or fields.get("published") != port
                or fields.get("protocol", "tcp") != "tcp"):
            fail("app port is not the chosen loopback health port")

for name in ("app", "db"):
    if scalar(mapping(services[name]), "restart") != "no":
        fail(f"service {name} must not restart automatically")

networks = mapping(root.get("networks") or [])
if "default" not in networks:
    fail("no default network")
for name, body in networks.items():
    if scalar(mapping(body), "internal") != "true":
        fail(f"network {name} is not internal")

image = scalar(mapping(services["db"]), "image")
if not image:
    fail("db service needs exactly one image")
print(image)
PYEOF
}

step_project() {
    write_overrides
    DB_IMAGE_REF="$(check_rendered_config)" \
        || step_fail "step 3: rendered Compose configuration is not isolated as required"
    step_pass "step 3: rendered configuration has an internal network and only 127.0.0.1:${HEALTH_PORT} published"

    if [ -n "$CANDIDATE_IMAGE" ]; then
        APP_IMAGE_REF="$CANDIDATE_IMAGE"
        compose_cand pull app || step_fail "step 3: could not pull the candidate app image"
    elif [ -f "$CAND_DIR/compose.build.override.yml" ]; then
        APP_IMAGE_REF="source build of the candidate commit"
        compose_cand build app || step_fail "step 3: could not build the candidate app image"
    else
        APP_IMAGE_REF="image pinned by the candidate compose.yaml"
        compose_cand pull app || step_fail "step 3: could not pull the candidate app image"
    fi

    compose_cand up -d db || step_fail "step 3: candidate database failed to start on a fresh volume"
    wait_for_pg_ready || step_fail "step 3: candidate database did not become ready"
    DB_IMAGE_ID="$(image_id_for db)" || step_fail "step 3: could not read the database image identity"
    step_pass "step 3: fresh candidate database ready ($DB_IMAGE_REF)"
}

step_restore() {
    run_install_script "$CAND_DIR/scripts/restore_database.sh" --verify-only "$ARCHIVE" \
        || step_fail "step 4: the candidate restore_database.sh --verify-only rejected the archive"
    step_pass "step 4: archive checksum and structure verified by the candidate restore script"
    run_install_script "$CAND_DIR/scripts/restore_database.sh" "$ARCHIVE" \
        || step_fail "step 4: restore into the fresh candidate volume failed"
    step_pass "step 4: archive restored with the app stopped"
}

# The candidate's own contract table list and the relations its pending
# migrations drop, read from the candidate app image itself.
read_candidate_contract() {
    local output
    output="$(compose_cand run --rm --no-deps -T app python -c '
import re, sys
from app.application_roles import TABLES
from app.db import MIGRATIONS_DIR
restored = int(sys.argv[1])
drop = re.compile(r"DROP\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+EXISTS\s+)?(?:public\.)?\"?([A-Za-z_][A-Za-z0-9_]*)", re.I)
for table in TABLES:
    print("REHEARSAL table", table)
latest = 0
for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
    version = int(path.name.split("_", 1)[0])
    latest = max(latest, version)
    if version > restored:
        for match in drop.finditer(path.read_text()):
            print("REHEARSAL drop", "%03d" % version, match.group(1))
print("REHEARSAL latest", latest)
' "$SCHEMA_BEFORE")" || return 1
    # A Compose frontend may add lines of its own; only marked lines count,
    # and every marked line must be well formed.
    output="$(printf '%s\n' "$output" | awk '$1 == "REHEARSAL" { sub(/^REHEARSAL /, ""); print }')"
    printf '%s\n' "$output" | awk '
        $1 == "table" && NF == 2 && $2 ~ /^[a-z_][a-z0-9_]*$/ { next }
        $1 == "drop" && NF == 3 && $2 ~ /^[0-9]+$/ { next }
        $1 == "latest" && NF == 2 && $2 ~ /^[0-9]+$/ { latest++; next }
        { bad = 1 }
        END { exit (bad || latest != 1) }
    ' || return 1
    printf '%s\n' "$output" | awk '$1 == "table" { print $2 }' > "$STATE_DIR/contract-tables.txt"
    printf '%s\n' "$output" | awk '$1 == "drop" { print $3 "\t" $2 }' > "$STATE_DIR/pending-drops.tsv"
    LATEST_MIGRATION="$(printf '%s\n' "$output" | awk '$1 == "latest" { print $2 }')"
    [ -s "$STATE_DIR/contract-tables.txt" ] && [ -n "$LATEST_MIGRATION" ]
}

# Prints relations from FILE that are not contract tables, comma-separated.
relations_outside_contract() {
    awk 'FILENAME == ARGV[1] { contract[$0] = 1; next } !($0 in contract) { print }' \
        "$STATE_DIR/contract-tables.txt" "$1"
}

join_lines() {
    awk 'NR > 1 { printf ", " } { printf "%s", $0 } END { if (NR) printf "\n" }'
}

step_before() {
    local outside name version
    SCHEMA_BEFORE="$(capture_schema_version)" || step_fail "step 5: could not read the restored schema version"
    capture_relations "$STATE_DIR/relations-before.txt" \
        || step_fail "step 5: could not list restored public relations"
    capture_counts "$STATE_DIR/counts-before.tsv" \
        || step_fail "step 5: could not count restored rows"
    read_candidate_contract || step_fail "step 5: could not read the candidate contract table list"

    outside="$(relations_outside_contract "$STATE_DIR/relations-before.txt")"
    local unknown=() handled=()
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        version="$(awk -F '\t' -v name="$name" '$1 == name { print $2; exit }' "$STATE_DIR/pending-drops.tsv")"
        if [ -n "$version" ]; then
            handled+=("$name (migration $version)")
        else
            unknown+=("$name")
        fi
    done <<< "$outside"
    UNKNOWN_BEFORE="$(printf '%s\n' ${unknown[@]+"${unknown[@]}"} | sed '/^$/d' | join_lines)"
    HANDLED_BEFORE="$(printf '%s\n' ${handled[@]+"${handled[@]}"} | sed '/^$/d' | join_lines)"
    if [ -n "$UNKNOWN_BEFORE" ]; then
        add_finding "FAIL: startup will refuse: unknown public relations: $UNKNOWN_BEFORE"
        echo "[FINDING] startup will refuse: unknown public relations: $UNKNOWN_BEFORE" >&2
    fi
    if [ "$SCHEMA_BEFORE" -lt 26 ]; then
        add_finding "NOTE: migration 026 imports the synthetic ingest password as a legacy credential, so ingest_credentials grows by one by design"
        if [ "${#LEGACY_KEYS[@]}" -eq 0 ]; then
            add_finding "NOTE: no --legacy-env given; migration 026 imports synthetic default preferences and no mileage-rate overrides"
        fi
    elif [ "${#LEGACY_KEYS[@]}" -gt 0 ]; then
        add_finding "NOTE: the archive is at schema $SCHEMA_BEFORE, so no pending migration reads the legacy keys"
    fi
    step_pass "step 5: restored schema $SCHEMA_BEFORE, $(wc -l < "$STATE_DIR/counts-before.tsv" | tr -d '[:space:]') tables counted, candidate latest migration $LATEST_MIGRATION"
}

# Returns 0 healthy, 1 exited, 2 runtime query failed, 3 timed out.
APP_EXIT_CODE=""
wait_for_app() {
    local deadline id state
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while :; do
        id="$(container_id_for app)" || return 2
        state="$("$runtime" inspect --format '{{.State.Status}}' "$id")" || return 2
        case "$state" in
            exited|dead|stopped)
                APP_EXIT_CODE="$("$runtime" inspect --format '{{.State.ExitCode}}' "$id" 2>/dev/null || true)"
                [[ "$APP_EXIT_CODE" =~ ^[0-9]+$ ]] || APP_EXIT_CODE="unknown"
                return 1
                ;;
        esac
        if compose_cand exec -T app python3 -c "$HEALTHZ_PROBE" >/dev/null 2>&1; then
            return 0
        fi
        [ "$(date +%s)" -lt "$deadline" ] || return 3
        sleep "$POLL_INTERVAL"
    done
}

# Reads timestamped container logs on stdin and prints one
# "<migration file> <seconds>" line per "applying migration" entry. A migration
# ends where the next one starts; the last ends at the next logged line. Only
# file names matching the migration pattern and computed numbers are printed.
parse_migration_timings() {
    python3 -c '
import datetime, re, sys

ts_re = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})")
mig_re = re.compile(r"applying migration (\d+_[A-Za-z0-9_]+\.sql)")

def parse(match):
    base, frac, zone = match.groups()
    frac = (frac or ".0")[1:7].ljust(6, "0")
    zone = "+00:00" if zone == "Z" else zone
    if len(zone) == 5:
        zone = zone[:3] + ":" + zone[3:]
    return datetime.datetime.fromisoformat(f"{base}.{frac}{zone}")

events = []
for line in sys.stdin:
    stamp = ts_re.search(line)
    if not stamp:
        continue
    migration = mig_re.search(line)
    events.append((parse(stamp), migration.group(1) if migration else None))

for index, (start, name) in enumerate(events):
    if name is None:
        continue
    later = events[index + 1:]
    end = next((when for when, other in later if other is not None), None)
    if end is None:
        end = next((when for when, _ in later), None)
    seconds = "unknown" if end is None else "%.3f" % max(0.0, (end - start).total_seconds())
    print(name, seconds)
'
}

record_migration_timings() {
    local id
    id="$(container_id_for app)" || return 1
    if [ "$KEEP" -eq 1 ]; then
        mkdir -p "$LOG_DIR"
        chmod 700 "$LOG_DIR"
        "$runtime" logs -t "$id" > "$LOG_DIR/app.log" 2>&1 || return 1
        RAW_LOGS_SAVED=1
        parse_migration_timings < "$LOG_DIR/app.log" > "$STATE_DIR/migrations.txt" || return 1
    else
        "$runtime" logs -t "$id" 2>&1 | parse_migration_timings > "$STATE_DIR/migrations.txt" || return 1
    fi
}

step_startup() {
    local started status=0
    started="$(date +%s)"
    if ! compose_cand up -d --no-build app; then
        STARTUP_SECONDS="$(( $(date +%s) - started ))"
        STARTUP_RESULT="failed (Compose could not start the app)"
        add_finding "FAIL: candidate app could not be started"
        return 0
    fi
    wait_for_app || status=$?
    STARTUP_SECONDS="$(( $(date +%s) - started ))"
    case "$status" in
        0) STARTUP_RESULT="healthy" ;;
        1) STARTUP_RESULT="failed (app exited with code $APP_EXIT_CODE)" ;;
        2) step_fail "step 6: could not read the app container state" ;;
        3) STARTUP_RESULT="timed out after ${HEALTH_TIMEOUT}s" ;;
    esac
    record_migration_timings || step_fail "step 6: could not read app container log timestamps"
    if [ "$status" -ne 0 ]; then
        add_finding "FAIL: candidate startup $STARTUP_RESULT"
        echo "[FAIL] step 6: candidate startup $STARTUP_RESULT; collecting the rest of the picture" >&2
    else
        step_pass "step 6: candidate healthy after ${STARTUP_SECONDS}s"
    fi
    APP_IMAGE_ID="$(image_id_for app)" || APP_IMAGE_ID="unknown"

    if [ "$status" -eq 0 ]; then
        if [ "$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HEALTH_PORT}/healthz" 2>/dev/null || true)" = "200" ]; then
            LOOPBACK_RESULT="yes"
        else
            LOOPBACK_RESULT="no"
        fi
        if compose_cand exec -T app python -m app.application_roles verify; then
            VERIFY_RESULT="pass"
        else
            VERIFY_RESULT="fail"
            add_finding "FAIL: python -m app.application_roles verify failed"
        fi
    else
        VERIFY_RESULT="not run (startup did not succeed)"
    fi
    # Counts are taken with the app stopped so workers cannot move them, and
    # a timed-out migration is abandoned (its transaction rolls back).
    compose_cand stop app >/dev/null || step_fail "step 6: could not stop the candidate app"
}

step_after() {
    local outside
    SCHEMA_AFTER="$(capture_schema_version)" || step_fail "step 7: could not read the schema version after startup"
    capture_relations "$STATE_DIR/relations-after.txt" \
        || step_fail "step 7: could not list public relations after startup"
    capture_counts "$STATE_DIR/counts-after.tsv" \
        || step_fail "step 7: could not count rows after startup"
    outside="$(relations_outside_contract "$STATE_DIR/relations-after.txt" | join_lines)"
    UNKNOWN_AFTER="$outside"
    if [ "$STARTUP_RESULT" = "healthy" ]; then
        if [ -n "$outside" ]; then
            add_finding "FAIL: public relations outside the candidate contract after startup: $outside"
        fi
        if [ "$SCHEMA_AFTER" != "$LATEST_MIGRATION" ]; then
            add_finding "FAIL: schema after startup ($SCHEMA_AFTER) is not the candidate latest migration ($LATEST_MIGRATION)"
        fi
    fi
    awk -F '\t' 'FILENAME == ARGV[1] { before[$1] = $2; next } ($1 in before) && $2 + 0 < before[$1] + 0 { print $1 }' \
        "$STATE_DIR/counts-before.tsv" "$STATE_DIR/counts-after.tsv" > "$STATE_DIR/decreased.txt"
    if [ -s "$STATE_DIR/decreased.txt" ]; then
        add_finding "NOTE: row counts decreased in: $(join_lines < "$STATE_DIR/decreased.txt")"
    fi
    step_pass "step 7: schema after $SCHEMA_AFTER, row counts captured"
}

# --- run the rehearsal ---------------------------------------------------

step_materialize
step_environment
step_project
step_restore
step_before
step_startup
step_after

FINISHED=1
for finding in ${FINDINGS[@]+"${FINDINGS[@]}"}; do
    case "$finding" in FAIL:*) exit 1 ;; esac
done
