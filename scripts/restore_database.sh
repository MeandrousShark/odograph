#!/usr/bin/env bash
# Restores a scripts/backup_database.sh archive into a fresh "mileage"
# database, or inspects an archive offline without touching a database.
# Run from the directory containing compose.yaml. The db service must
# already be running (it supplies the pinned pg_restore/psql build); a
# full restore additionally refuses to run while the app service is up,
# and refuses any target database that already holds application data.
#
# Usage:
#   scripts/restore_database.sh [--skip-checksum] ARCHIVE
#   scripts/restore_database.sh --verify-only ARCHIVE
#
# --skip-checksum skips only the checksum check (archive-structure
# validation always runs), for an operator who separately verified a
# relocated archive's integrity. Not valid together with --verify-only.
#
# Env vars:
#   COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: scripts/restore_database.sh [--skip-checksum] ARCHIVE
       scripts/restore_database.sh --verify-only ARCHIVE

Restores ARCHIVE (a scripts/backup_database.sh custom-format dump) into a
fresh "mileage" database on the running db service, or with --verify-only
checks the archive's checksum and structure without connecting to or
changing a database. Both modes require the db service already running.
A full restore additionally refuses to run while the app service is up.
EOF
    exit "${1:-1}"
}

SKIP_CHECKSUM=0
VERIFY_ONLY=0
POSITIONAL=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-checksum) SKIP_CHECKSUM=1; shift ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        -h|--help) usage 0 ;;
        --) shift
            while [ "$#" -gt 0 ]; do POSITIONAL+=("$1"); shift; done
            ;;
        -*) echo "error: unrecognized argument: $1" >&2; usage ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done

if [ "${#POSITIONAL[@]}" -ne 1 ]; then
    echo "error: expected exactly one ARCHIVE argument, got ${#POSITIONAL[@]}." >&2
    usage
fi
ARCHIVE="${POSITIONAL[0]}"

if [ "$VERIFY_ONLY" -eq 1 ] && [ "$SKIP_CHECKSUM" -eq 1 ]; then
    echo "error: --skip-checksum is not valid with --verify-only." >&2
    usage
fi

if [ ! -f compose.yaml ]; then
    echo "error: compose.yaml not found in the current directory; run this script from the canonical installation checkout." >&2
    exit 1
fi

if [ ! -f "$ARCHIVE" ]; then
    echo "error: archive not found: $ARCHIVE" >&2
    exit 1
fi

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

# The sidecar records only the archive's basename, so verification has to
# run from the archive's own directory to resolve that name.
verify_checksum() {
    local archive="$1" archive_dir archive_base sidecar_base
    archive_dir="$(dirname -- "$archive")"
    archive_base="$(basename -- "$archive")"
    sidecar_base="${archive_base}.sha256"
    if [ ! -f "${archive_dir}/${sidecar_base}" ]; then
        echo "error: checksum sidecar not found: ${archive_dir}/${sidecar_base}" >&2
        exit 1
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        if ! (cd "$archive_dir" && sha256sum -c "$sidecar_base") > /dev/null; then
            echo "error: checksum verification failed for $archive" >&2
            exit 1
        fi
    elif command -v shasum >/dev/null 2>&1; then
        if ! (cd "$archive_dir" && shasum -a 256 -c "$sidecar_base") > /dev/null; then
            echo "error: checksum verification failed for $archive" >&2
            exit 1
        fi
    else
        echo "error: neither sha256sum nor shasum found on PATH." >&2
        exit 1
    fi
}

if [ "$VERIFY_ONLY" -eq 1 ]; then
    verify_checksum "$ARCHIVE"
    echo "Checksum OK: $ARCHIVE matches its .sha256 sidecar."

    # No -d/--dbname anywhere here: --verify-only must never connect to or
    # change a database, only read the archive's own table of contents.
    toc="$($compose_cmd exec -T db pg_restore --list < "$ARCHIVE")" || {
        echo "error: pg_restore --list failed. Either the archive is corrupt / not a compatible custom-format dump, or the db service is not running (start it with: $compose_cmd up -d db)." >&2
        exit 1
    }
    member_count="$(printf '%s\n' "$toc" | wc -l | tr -d '[:space:]')"
    echo "Archive structure OK: $member_count line(s) in the table of contents."
    exit 0
fi

# --- Full restore below. Every precondition is checked before anything
# that could modify the target database. ---

# No -h here would probe the unix socket, which even the postgres
# entrypoint's temporary init-phase server (fresh volume only, before
# initdb/PostGIS setup finishes and the real server starts) listens on --
# a "ready" from that server can be followed by "connection refused" once
# it stops. The init-phase server never listens on TCP, so probing
# 127.0.0.1 only succeeds once the final server is accepting connections.
db_ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if $compose_cmd exec -T db pg_isready -h 127.0.0.1 -p 5432 -U mileage -d mileage > /dev/null 2>&1; then
        db_ready=1
        break
    fi
    sleep 1
done
if [ "$db_ready" -ne 1 ]; then
    echo "error: db service did not answer pg_isready. Start it first (e.g. $compose_cmd up -d db) and wait for it to become healthy." >&2
    exit 1
fi

# podman-compose's `ps` (unlike docker compose's) accepts no positional
# service filter -- `ps app` exits with a usage error under podman-compose,
# which the `2>/dev/null` here would otherwise silently turn into "app is
# not running" even when it is. List every service instead and match app's
# own row by its container-name segment, which both frontends render as
# either `..._app_<n>` or `..-app-<n>`.
app_status="$($compose_cmd ps 2>/dev/null | grep -E '(^|[-_])app([-_][0-9]+)?([[:space:]]|$)')" || app_status=""
if printf '%s\n' "$app_status" | grep -iqE 'up|running'; then
    echo "error: the app service appears to be running. Stop it first (e.g. $compose_cmd stop app) -- a live app must not race the restore." >&2
    exit 1
fi

# A fresh postgis-image database already owns extension relations such as
# spatial_ref_sys, so this excludes anything pg_depend marks as
# extension-owned (deptype 'e'); everything else, including
# schema_migrations, means the target already has application data.
EMPTY_TARGET_SQL="SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','m','S','f') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e') LIMIT 5;"
offending="$($compose_cmd exec -T db psql -U mileage -d mileage -Atc "$EMPTY_TARGET_SQL")"
if [ -n "$offending" ]; then
    echo "error: the mileage database is not empty; refusing to restore over existing data. Found relation(s):" >&2
    printf '%s\n' "$offending" >&2
    echo "Restore into a fresh database volume/Compose project instead." >&2
    exit 1
fi

if [ "$SKIP_CHECKSUM" -eq 1 ]; then
    echo "WARNING: --skip-checksum given; skipping checksum verification. Proceeding only because you have separately verified this archive's integrity." >&2
else
    verify_checksum "$ARCHIVE"
    echo "Checksum OK: $ARCHIVE matches its .sha256 sidecar."
fi

restore_tmp=""
# Render first, then feed one complete SQL file to psql so legacy schema
# bootstrap and archive restore share the same transaction.
cleanup_restore_tmp() {
    local exit_code=$?
    trap - EXIT HUP INT TERM
    if [ -n "$restore_tmp" ]; then
        rm -R -f -- "$restore_tmp" || true
    fi
    exit "$exit_code"
}
trap cleanup_restore_tmp EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

umask 077
restore_tmp="$(mktemp -d "${TMPDIR:-/tmp}/odograph-restore.XXXXXX")"
chmod 700 "$restore_tmp"
toc_file="$restore_tmp/archive.toc"
rendered_sql="$restore_tmp/archive.sql"
restore_input="$restore_tmp/restore.sql"

if ! $compose_cmd exec -T db pg_restore --list < "$ARCHIVE" > "$toc_file"; then
    echo "error: pg_restore --list failed against $ARCHIVE; archive appears corrupt or is not a compatible custom-format dump." >&2
    exit 1
fi
echo "Archive structure OK."

toc_has_extension() {
    local extension="$1"
    awk -v extension="$extension" '$4 == "EXTENSION" && $6 == extension { found = 1 } END { exit !found }' "$toc_file"
}

toc_has_schema() {
    local schema="$1"
    awk -v schema="$schema" '$4 == "SCHEMA" && $6 == schema { found = 1 } END { exit !found }' "$toc_file"
}

bootstrap_sql=""
if toc_has_extension postgis_tiger_geocoder; then
    toc_has_schema tiger || bootstrap_sql+=$'CREATE SCHEMA IF NOT EXISTS tiger;\n'
    toc_has_schema tiger_data || bootstrap_sql+=$'CREATE SCHEMA IF NOT EXISTS tiger_data;\n'
fi
if toc_has_extension postgis_topology && ! toc_has_schema topology; then
    bootstrap_sql+=$'CREATE SCHEMA IF NOT EXISTS topology;\n'
fi

echo "Rendering $ARCHIVE into a temporary SQL file before opening a database transaction."
if ! $compose_cmd exec -T db pg_restore --file=- --no-owner --no-privileges < "$ARCHIVE" > "$rendered_sql" 2> "$restore_tmp/render.log"; then
    echo "error: pg_restore could not render $ARCHIVE; no SQL was sent to the database." >&2
    exit 1
fi
chmod 600 "$rendered_sql"

if ! {
    printf '%s' "$bootstrap_sql"
    cat -- "$rendered_sql"
} > "$restore_input"; then
    echo "error: could not prepare the transactional restore input; no SQL was sent to the database." >&2
    exit 1
fi
chmod 600 "$restore_input"

echo "Restoring $ARCHIVE into the mileage database in one transaction via: $compose_cmd exec -T db psql ..."
if ! $compose_cmd exec -T db psql -X --single-transaction -v ON_ERROR_STOP=1 -U mileage -d mileage -f - < "$restore_input"; then
    echo "error: transactional restore failed; schema bootstrap and archive restore were rolled back. Fix the underlying issue and retry." >&2
    exit 1
fi

$compose_cmd exec -T db psql -U mileage -d mileage -v ON_ERROR_STOP=1 -c "ANALYZE;" > /dev/null

# The restore already committed by this point, so a missing ledger here is
# reported, not treated as a failure to roll back from.
restored_version="$($compose_cmd exec -T db psql -U mileage -d mileage -Atc \
    "SELECT COALESCE(max(version), 0) FROM schema_migrations" 2>/dev/null)" || restored_version=""
if [ -z "$restored_version" ]; then
    echo "WARNING: schema_migrations is missing after restore; this archive predates any migration." >&2
    restored_version="0"
fi
echo "Restored schema version: $restored_version"

manifest="${ARCHIVE}.manifest"
if [ -f "$manifest" ]; then
    manifest_version="$(grep -m1 '^schema_version=' "$manifest" | cut -d= -f2-)"
    # The database is the source of truth; a mismatch is worth flagging
    # loudly but must not undo an already-committed restore.
    if [ -n "$manifest_version" ] && [ "$manifest_version" != "$restored_version" ]; then
        echo "WARNING: manifest schema_version ($manifest_version) does not match the restored database's schema_version ($restored_version)." >&2
    fi
fi

echo "Restore complete. Next: start the exact application release that produced this backup (or the documented upgrade target) via: $compose_cmd up -d app"
echo "That release's own startup migrations, if any, will run from here forward."
