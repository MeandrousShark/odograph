#!/usr/bin/env bash
# Print the database identity of a PostGIS image: the upstream versions of
# the components that decide whether an existing data directory can run
# under it. Two images with the same identity run the same PostgreSQL,
# PostGIS, GEOS and PROJ against the same glibc and ICU collations, so one
# can replace the other on the same volume. Debian package revisions are
# deliberately not part of it.
#
# Usage:
#   scripts/db_image_identity.sh IMAGE
#
# Prints one line, for example:
#   postgresql=16.15 postgis=3.6.4 geos=3.11.1 proj=9.8.1 glibc=2.36 icu=72.1
#
# The image runs with its entrypoint replaced by a shell that only reads
# files and version output: no database server starts, no volume is mounted,
# and the container has no network and a read-only root filesystem. Any
# component that cannot be determined fails the probe.
#
# Env vars:
#   CONTAINER_RUNTIME   override engine autodetection (docker, else podman)
set -euo pipefail

usage() {
    echo "usage: scripts/db_image_identity.sh IMAGE" >&2
    exit "${1:-1}"
}

case "${1:-}" in
    -h|--help) usage 0 ;;
esac
[ "$#" -eq 1 ] || usage
IMAGE="$1"
case "$IMAGE" in
    ""|-*) echo "error: IMAGE must be an image reference" >&2; usage ;;
esac

die() {
    echo "error: $IMAGE: $1" >&2
    exit 1
}

detect_runtime() {
    if [ -n "${CONTAINER_RUNTIME:-}" ]; then
        echo "$CONTAINER_RUNTIME"
    elif command -v docker >/dev/null 2>&1; then
        echo docker
    elif command -v podman >/dev/null 2>&1; then
        echo podman
    else
        echo "error: neither docker nor podman found on PATH; install one or set CONTAINER_RUNTIME." >&2
        exit 1
    fi
}

# Runs inside the image under /bin/sh. It only reports raw facts as
# key=value lines; every version is parsed and validated on the host.
# shellcheck disable=SC2016 # expanded by the container shell
COLLECT='
pg="$(command -v postgres 2>/dev/null || true)"
[ -n "$pg" ] || exit 0
pg="$(readlink -f "$pg")"
version="$("$pg" --version 2>/dev/null | head -n 1)"
echo "postgres_version=$version"
major="$(echo "$version" | sed -n "s/^postgres (PostgreSQL) \([0-9][0-9]*\).*/\1/p")"
prefix="$(dirname "$(dirname "$pg")")"
control=""
for candidate in "/usr/share/postgresql/$major/extension/postgis.control" "$prefix/share/postgresql/extension/postgis.control"; do
    if [ -n "$major" ] && [ -f "$candidate" ]; then control="$candidate"; break; fi
done
[ -n "$control" ] || exit 0
echo "postgis_default_version=$(grep -m 1 "^[[:space:]]*default_version" "$control" || true)"
module="$(grep -m 1 "^[[:space:]]*module_pathname" "$control" | sed "s|.*/||" | tr -d "\047\"[:space:]" || true)"
library=""
for candidate in "$prefix/lib/$module.so" "$prefix/lib/postgresql/$module.so"; do
    if [ -n "$module" ] && [ -f "$candidate" ]; then library="$candidate"; break; fi
done
echo "postgis_library=$library"
[ -n "$library" ] || exit 0
linked() {
    path="$(ldd "$1" 2>/dev/null | awk -v name="$2" "index(\$1, name) == 1 && \$2 == \"=>\" { print \$3; exit }")"
    [ -n "$path" ] && [ -e "$path" ] && basename "$(readlink -f "$path")"
}
echo "geos_library=$(linked "$library" libgeos.so. || linked "$library" libgeos- || true)"
echo "proj_library=$(linked "$library" libproj.so. || true)"
echo "icu_library=$(linked "$pg" libicuuc.so. || true)"
echo "glibc_version=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
'

fact() {
    printf '%s\n' "$FACTS" | sed -n "s/^$1=//p" | head -n 1
}

# Validates the raw facts and prints the canonical identity line.
parse_identity() {
    local raw postgresql postgis geos proj icu glibc
    raw="$(fact postgres_version)"
    [ -n "$raw" ] || die "no postgres binary found; not a PostgreSQL image"
    [[ "$raw" =~ ^postgres\ \(PostgreSQL\)\ ([0-9]+\.[0-9]+)([[:space:]]|$) ]] \
        || die "could not determine the PostgreSQL version from '$raw'"
    postgresql="${BASH_REMATCH[1]}"

    raw="$(fact postgis_default_version)"
    [ -n "$raw" ] || die "no postgis.control for PostgreSQL $postgresql; not a PostGIS image"
    [[ "$raw" =~ ^[[:space:]]*default_version[[:space:]]*=[[:space:]]*\'([0-9]+\.[0-9]+\.[0-9]+)\'[[:space:]]*$ ]] \
        || die "could not determine the PostGIS version from '$raw'"
    postgis="${BASH_REMATCH[1]}"
    [ -n "$(fact postgis_library)" ] \
        || die "postgis.control names no installed PostGIS library; not a PostGIS image"

    raw="$(fact geos_library)"
    [[ "$raw" =~ ^libgeos(\.so\.|-)([0-9]+\.[0-9]+\.[0-9]+)(\.so)?$ ]] \
        || die "could not determine the GEOS version from the library PostGIS links ('$raw')"
    geos="${BASH_REMATCH[2]}"

    raw="$(fact proj_library)"
    [[ "$raw" =~ ^libproj\.so\.[0-9]+\.([0-9]+\.[0-9]+\.[0-9]+)$ ]] \
        || die "could not determine the PROJ version from the library PostGIS links ('$raw')"
    proj="${BASH_REMATCH[1]}"

    raw="$(fact glibc_version)"
    [[ "$raw" =~ ^glibc\ ([0-9]+\.[0-9]+)$ ]] \
        || die "could not determine the glibc version from '$raw'"
    glibc="${BASH_REMATCH[1]}"

    raw="$(fact icu_library)"
    [[ "$raw" =~ ^libicuuc\.so\.([0-9]+\.[0-9]+)$ ]] \
        || die "could not determine the ICU version PostgreSQL links ('$raw')"
    icu="${BASH_REMATCH[1]}"

    printf 'postgresql=%s postgis=%s geos=%s proj=%s glibc=%s icu=%s\n' \
        "$postgresql" "$postgis" "$geos" "$proj" "$glibc" "$icu"
}

runtime="$(detect_runtime)"
if ! FACTS="$("$runtime" run --rm --network none --read-only \
    --entrypoint /bin/sh "$IMAGE" -c "$COLLECT" </dev/null)"; then
    die "could not run the image with $runtime"
fi
parse_identity
