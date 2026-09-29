#!/usr/bin/env bash
# Produces a portable custom-format pg_dump of the live "mileage" database
# from a running Compose or direct container install, plus a SHA-256 checksum sidecar and a
# non-secret manifest, without ever stopping the app or putting a password
# on a command line (the database trusts the container-local socket
# connection pg_dump/psql use here). Run from the installation checkout.
#
# Usage:
#   scripts/backup_database.sh [--output PATH] [--container NAME]
#
# Default output: backups/mileage-<UTC timestamp>.dump, next to a
# <archive>.sha256 checksum sidecar and a <archive>.manifest file. Refuses
# to overwrite any of the three if one already exists.
#
# Env vars:
#   COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
#   CONTAINER_RUNTIME  override direct container runtime autodetection, e.g. "podman"
set -euo pipefail
umask 077

usage() {
    echo "usage: $0 [--output PATH] [--container NAME]" >&2
    exit "${1:-1}"
}

OUTPUT=""
CONTAINER_NAME=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output)
            if [ "$#" -lt 2 ]; then
                echo "error: --output requires a value" >&2
                usage
            fi
            OUTPUT="$2"
            shift 2
            ;;
        --container)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "error: --container requires a non-empty value" >&2
                usage
            fi
            CONTAINER_NAME="$2"
            shift 2
            ;;
        -h|--help) usage 0 ;;
        *) echo "error: unrecognized argument: $1" >&2; usage ;;
    esac
done

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

detect_container_runtime() {
    if [ -n "${CONTAINER_RUNTIME:-}" ]; then
        echo "$CONTAINER_RUNTIME"
        return
    fi
    if command -v podman >/dev/null 2>&1; then
        echo "podman"
        return
    fi
    if command -v docker >/dev/null 2>&1; then
        echo "docker"
        return
    fi
    echo "error: neither 'podman' nor 'docker' found on PATH; set CONTAINER_RUNTIME to override." >&2
    exit 1
}

if [ -n "$CONTAINER_NAME" ]; then
    runtime_cmd="$(detect_container_runtime)"
else
    if [ ! -f compose.yaml ]; then
        echo "error: compose.yaml not found in the current directory; run this script from the canonical installation checkout." >&2
        exit 1
    fi
    compose_cmd="$(detect_compose_cmd)"
fi

if [ -z "$OUTPUT" ]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    OUTPUT="backups/mileage-${stamp}.dump"
fi

archive="$OUTPUT"
sidecar="${archive}.sha256"
manifest="${archive}.manifest"
output_dir="$(dirname -- "$archive")"
# BSD chmod (stock macOS) takes the mode as its first operand and doesn't
# permute args like GNU chmod does, so a leading "-" in output_dir would be
# parsed as an option; normalize it to a "./"-relative path.
case "$output_dir" in
    -*) output_dir="./$output_dir" ;;
esac

# Skip mkdir/chmod when the archive has no directory component: chmod 700
# on "." would lock down the whole install checkout, not just the backup,
# which is well outside what "make the backup directory private" means.
if [ "$output_dir" != "." ]; then
    mkdir -p -- "$output_dir"
    # No "--" here: BSD chmod parses "700" as the first operand and stops
    # option parsing there, so a following "--" is treated as a literal
    # filename ("chmod: --: No such file or directory") instead of an
    # end-of-options marker. GNU chmod permutes args and tolerates it, but
    # stock macOS/BSD chmod does not.
    chmod 700 "$output_dir"
fi

# Use a deterministic, short reservation name based on the resolved output
# location. mkdir is atomic, so exactly one invocation can own this artifact
# set even when default timestamps collide within a second.
archive_basename="$(basename -- "$archive")"
canonical_output_dir="$(cd "$output_dir" && pwd -P)"
reservation_id="$(printf '%s/%s\n' "$canonical_output_dir" "$archive_basename" | cksum | awk '{print $1 "-" $2}')"
reservation_dir="${canonical_output_dir}/.odograph-backup-${reservation_id}.lock"
if ! mkdir "$reservation_dir" 2>/dev/null; then
    echo "error: the output set for $archive is already reserved; refusing to overlap another backup." >&2
    exit 1
fi

tmp_archive=""
tmp_sidecar=""
tmp_manifest=""

owned_link() {
    [ -f "$2" ] && [ ! -L "$2" ] && [ "$1" -ef "$2" ]
}

remove_owned_link() {
    local staged_path="$1" final_path="$2" nested_path
    if owned_link "$staged_path" "$final_path"; then
        rm -f -- "$final_path" || true
    fi
    # ln can interpret a directory (including a symlink to one) as its
    # destination. A signal may interrupt before publish_no_clobber checks it.
    if [ -d "$final_path" ]; then
        nested_path="${final_path}/$(basename -- "$staged_path")"
        if owned_link "$staged_path" "$nested_path"; then
            rm -f -- "$nested_path" || true
        fi
    fi
}

cleanup() {
    local exit_code=$?
    local archive_complete=0
    trap - EXIT HUP INT TERM

    # A signal can arrive after ln succeeds but before its caller resumes. The
    # staged inode proves ownership independently of the interrupted step.
    if [ -n "$tmp_archive" ] && [ -n "$tmp_sidecar" ] && [ -n "$tmp_manifest" ] \
        && owned_link "$tmp_archive" "$archive" \
        && owned_link "$tmp_sidecar" "$sidecar" \
        && owned_link "$tmp_manifest" "$manifest"; then
        archive_complete=1
    fi

    if [ "$archive_complete" -ne 1 ]; then
        if [ -n "$tmp_archive" ]; then remove_owned_link "$tmp_archive" "$archive"; fi
        if [ -n "$tmp_sidecar" ]; then remove_owned_link "$tmp_sidecar" "$sidecar"; fi
        if [ -n "$tmp_manifest" ]; then remove_owned_link "$tmp_manifest" "$manifest"; fi
    fi
    if [ -n "$tmp_archive" ]; then rm -f -- "$tmp_archive" || true; fi
    if [ -n "$tmp_sidecar" ]; then rm -f -- "$tmp_sidecar" || true; fi
    if [ -n "$tmp_manifest" ]; then rm -f -- "$tmp_manifest" || true; fi
    rmdir "$reservation_dir" 2>/dev/null || true
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for existing in "$archive" "$sidecar" "$manifest"; do
    if [ -e "$existing" ] || [ -L "$existing" ]; then
        echo "error: $existing already exists; refusing to overwrite a previous backup. Pick a different --output or remove it first." >&2
        exit 1
    fi
done

# Stage privately on the same filesystem as the published files. Final links
# are exclusive (ln never replaces an existing destination), and the archive
# remains the last publication step and sole completion signal.
tmp_archive="$(mktemp "${reservation_dir}/archive.XXXXXX")"
tmp_sidecar="${reservation_dir}/sidecar"
tmp_manifest="${reservation_dir}/manifest"

exec_db() {
    if [ -n "$CONTAINER_NAME" ]; then
        $runtime_cmd exec -i "$CONTAINER_NAME" "$@"
    else
        $compose_cmd exec -T db "$@"
    fi
}

# A scoped runtime identity must never produce a deceptively partial
# "full instance" archive; forced row-level security would hide rows from it.
backup_privilege="$(exec_db psql -X -U mileage -d mileage -Atc \
    "SELECT CASE WHEN rolsuper OR rolbypassrls THEN 'full-instance' ELSE 'refused' END FROM pg_roles WHERE rolname = current_user")"
if [ "$backup_privilege" != "full-instance" ]; then
    echo "error: full-instance backup requires the privileged backup identity; scoped runtime dumps are refused." >&2
    exit 1
fi

if [ -n "$CONTAINER_NAME" ]; then
    echo "Dumping database via: $runtime_cmd exec -i $CONTAINER_NAME pg_dump ..."
else
    echo "Dumping database via: $compose_cmd exec -T db pg_dump ..."
fi
# Compose uses -T to disable TTY allocation. Direct runtimes use -i to keep
# stdin attached without allocating a TTY. A TTY would mangle the binary dump
# stream.
#
# The postgis/postgis image's init scripts install postgis_tiger_geocoder
# and postgis_topology into every freshly initialized database, which
# creates the tiger, tiger_data, and topology schemas outside of CREATE
# EXTENSION's own bookkeeping. pg_dump therefore emits bare CREATE SCHEMA
# statements for them, which collide with the same image-provisioned
# schemas already present on any fresh restore target. None of the three
# ever holds application data -- the application uses public and its own
# protected odograph_service schema, and
# tiger_data is populated only if an operator explicitly runs census-loader
# scripts, which this app never does -- so excluding them is safe. This is
# a denylist of known image-provisioned schemas, not an allowlist, so any
# future application schema stays included by default.
if ! exec_db pg_dump -U mileage -d mileage --format=custom \
    --exclude-schema=tiger --exclude-schema=tiger_data --exclude-schema=topology \
    > "$tmp_archive"; then
    echo "error: pg_dump failed; no backup was published." >&2
    exit 1
fi

if ! exec_db pg_restore --list < "$tmp_archive" > /dev/null; then
    echo "error: the dump failed pg_restore --list validation; no backup was published." >&2
    exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
    checksum_hex="$(sha256sum "$tmp_archive" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    checksum_hex="$(shasum -a 256 "$tmp_archive" | awk '{print $1}')"
else
    echo "error: neither sha256sum nor shasum found on PATH." >&2
    exit 1
fi

# Two spaces matches the sha256sum/shasum sidecar format so "-c" can verify
# this file directly from within the archive's directory.
printf '%s  %s\n' "$checksum_hex" "$archive_basename" > "$tmp_sidecar"

# A pre-migration database is still a valid backup target, so a missing
# schema_migrations table records 0 instead of aborting the backup.
schema_version="$(exec_db psql -U mileage -d mileage -Atc \
    "SELECT COALESCE(max(version), 0) FROM schema_migrations" 2>/dev/null)" || schema_version=""
[ -n "$schema_version" ] || schema_version="0"

postgres_version="$(exec_db psql -U mileage -d mileage -Atc "SHOW server_version")"
postgis_version="$(exec_db psql -U mileage -d mileage -Atc "SELECT postgis_lib_version()")"

# git absence/failure must not fail the backup -- "unknown" is a fine
# manifest value when this isn't a git checkout at all.
source_ref="unknown"
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if ref="$(git describe --always --dirty 2>/dev/null)"; then
        source_ref="$ref"
    fi
fi

created_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
    printf 'created_utc=%s\n' "$created_utc"
    printf 'archive=%s\n' "$archive_basename"
    printf 'schema_version=%s\n' "$schema_version"
    printf 'backup_scope=full-instance\n'
    printf 'postgres_version=%s\n' "$postgres_version"
    printf 'postgis_version=%s\n' "$postgis_version"
    printf 'source_ref=%s\n' "$source_ref"
} > "$tmp_manifest"

publish_no_clobber() {
    local staged_path="$1" final_path="$2" link_path="$2" nested_path
    case "$link_path" in
        -*) link_path="./$link_path" ;;
    esac
    if ! ln "$staged_path" "$link_path"; then
        return 1
    fi
    if owned_link "$staged_path" "$final_path"; then
        return 0
    fi

    # ln treats an existing directory target as a destination directory.
    # Remove only the hard link this call may have placed inside it.
    nested_path="${link_path}/$(basename -- "$staged_path")"
    if owned_link "$staged_path" "$nested_path"; then
        rm -f -- "$nested_path"
    fi
    return 1
}

# Publish metadata first, then the archive as the success signal. Hard links
# provide atomic no-clobber publication on the same filesystem.
if ! publish_no_clobber "$tmp_sidecar" "$sidecar"; then
    echo "error: could not publish checksum sidecar without overwriting an existing path." >&2
    exit 1
fi
if ! publish_no_clobber "$tmp_manifest" "$manifest"; then
    echo "error: could not publish manifest without overwriting an existing path." >&2
    exit 1
fi
if ! publish_no_clobber "$tmp_archive" "$archive"; then
    echo "error: could not publish archive without overwriting an existing path." >&2
    exit 1
fi

echo "Backup complete: $archive"
echo "Reminder: an unencrypted local file is not an off-host backup."
