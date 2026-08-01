#!/usr/bin/env bash
# Produces a portable custom-format pg_dump of the live "mileage" database
# from a running compose install, plus a SHA-256 checksum sidecar and a
# non-secret manifest, without ever stopping the app or putting a password
# on a command line (the database trusts the container-local socket
# connection pg_dump/psql use here). Run from the directory containing
# compose.yaml.
#
# Usage:
#   scripts/backup_database.sh [--output PATH]
#
# Default output: backups/mileage-<UTC timestamp>.dump, next to a
# <archive>.sha256 checksum sidecar and a <archive>.manifest file. Refuses
# to overwrite any of the three if one already exists.
#
# Env vars:
#   COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
set -euo pipefail
umask 077

usage() {
    echo "usage: $0 [--output PATH]" >&2
    exit "${1:-1}"
}

OUTPUT=""
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
        -h|--help) usage 0 ;;
        *) echo "error: unrecognized argument: $1" >&2; usage ;;
    esac
done

if [ ! -f compose.yaml ]; then
    echo "error: compose.yaml not found in the current directory; run this script from the canonical installation checkout." >&2
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

for existing in "$archive" "$sidecar" "$manifest"; do
    if [ -e "$existing" ]; then
        echo "error: $existing already exists; refusing to overwrite a previous backup. Pick a different --output or remove it first." >&2
        exit 1
    fi
done

# mktemp in the same directory as the final archive keeps the eventual
# publish a same-filesystem rename, so it's atomic instead of a
# copy-then-delete that could leave a half-written file behind.
tmp_archive="$(mktemp "${output_dir}/.mileage-backup.XXXXXX")"
archive_published=0

cleanup() {
    # Once the archive is renamed into place, tmp_archive no longer exists
    # at this path, so this rm is a harmless no-op on the success path.
    rm -f -- "$tmp_archive"
    if [ "$archive_published" -ne 1 ]; then
        # The archive's presence is the sole success signal; any failure
        # before the final rename must not leave an orphaned sidecar or
        # manifest that looks like a completed backup.
        rm -f -- "$sidecar" "$manifest"
    fi
}
trap cleanup EXIT

echo "Dumping database via: $compose_cmd exec -T db pg_dump ..."
# -T disables TTY allocation; a TTY would mangle the binary dump stream.
#
# The postgis/postgis image's init scripts install postgis_tiger_geocoder
# and postgis_topology into every freshly initialized database, which
# creates the tiger, tiger_data, and topology schemas outside of CREATE
# EXTENSION's own bookkeeping. pg_dump therefore emits bare CREATE SCHEMA
# statements for them, which collide with the same image-provisioned
# schemas already present on any fresh restore target. None of the three
# ever holds application data -- every migration only touches public, and
# tiger_data is populated only if an operator explicitly runs census-loader
# scripts, which this app never does -- so excluding them is safe. This is
# a denylist of known image-provisioned schemas, not an allowlist, so any
# future application schema stays included by default.
if ! $compose_cmd exec -T db pg_dump -U mileage -d mileage --format=custom \
    --exclude-schema=tiger --exclude-schema=tiger_data --exclude-schema=topology \
    > "$tmp_archive"; then
    echo "error: pg_dump failed; no backup was published." >&2
    exit 1
fi

if ! $compose_cmd exec -T db pg_restore --list < "$tmp_archive" > /dev/null; then
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

archive_basename="$(basename -- "$archive")"
# Two spaces matches the sha256sum/shasum sidecar format so "-c" can verify
# this file directly from within the archive's directory.
printf '%s  %s\n' "$checksum_hex" "$archive_basename" > "$sidecar"

# A pre-migration database is still a valid backup target, so a missing
# schema_migrations table records 0 instead of aborting the backup.
schema_version="$($compose_cmd exec -T db psql -U mileage -d mileage -Atc \
    "SELECT COALESCE(max(version), 0) FROM schema_migrations" 2>/dev/null)" || schema_version=""
[ -n "$schema_version" ] || schema_version="0"

postgres_version="$($compose_cmd exec -T db psql -U mileage -d mileage -Atc "SHOW server_version")"
postgis_version="$($compose_cmd exec -T db psql -U mileage -d mileage -Atc "SELECT postgis_lib_version()")"

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
    printf 'postgres_version=%s\n' "$postgres_version"
    printf 'postgis_version=%s\n' "$postgis_version"
    printf 'source_ref=%s\n' "$source_ref"
} > "$manifest"

# Sidecar and manifest are already at their final names; the archive
# rename is the last thing that can fail, and it's a same-filesystem
# rename so it's atomic.
mv -- "$tmp_archive" "$archive"
archive_published=1

echo "Backup complete: $archive"
echo "Reminder: an unencrypted local file is not an off-host backup."
