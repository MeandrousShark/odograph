#!/usr/bin/env bash
# Starts and cleans up labelled disposable PostGIS containers for DB-backed
# tests. A task ID keeps concurrent test runs isolated from each other.
#
# Usage:
#   eval "$(scripts/test_db.sh start TASK_ID)"
#   scripts/test_db.sh cleanup TASK_ID
set -euo pipefail

IMAGE="${TEST_DB_IMAGE:-ghcr.io/meandrousshark/odograph-postgis@sha256:06b6d9eee52766e4b77f748a962612c8ef00343adba7c5ea09261bc223d1aa56}"
OWNER_LABEL="io.odograph.test-db"
TASK_LABEL="io.odograph.test-db-task"

usage() {
    echo "usage: $0 start TASK_ID | cleanup TASK_ID" >&2
    exit "${1:-1}"
}

[ "$#" -eq 2 ] || usage
ACTION="$1"
TASK_ID="$2"

case "$TASK_ID" in
    *[!A-Za-z0-9_.-]*|"")
        echo "error: TASK_ID may contain only letters, numbers, dot, underscore, and hyphen." >&2
        exit 1
        ;;
esac

case "$ACTION" in
    start|cleanup) ;;
    *) usage ;;
esac

if [ "$ACTION" = cleanup ]; then
    container_ids="$(podman ps -aq --filter "label=$OWNER_LABEL=1" --filter "label=$TASK_LABEL=$TASK_ID")"
    [ -n "$container_ids" ] || exit 0

    while IFS= read -r container_id; do
        [ -n "$container_id" ] || continue
        labels="$(podman inspect -f "{{ index .Config.Labels \"$OWNER_LABEL\" }}|{{ index .Config.Labels \"$TASK_LABEL\" }}" "$container_id")"
        if [ "$labels" = "1|$TASK_ID" ]; then
            podman rm -f --volumes "$container_id" >/dev/null
        fi
    done <<< "$container_ids"
    exit 0
fi

# Readiness attempts, 0.2 s apart; tests of the failure path lower it. A
# leading zero is refused because bash arithmetic would read it as octal.
READY_ATTEMPTS="${TEST_DB_READY_ATTEMPTS:-150}"
case "$READY_ATTEMPTS" in
    *[!0-9]*|0*)
        echo "error: TEST_DB_READY_ATTEMPTS must be a positive whole number." >&2
        exit 1
        ;;
esac

# Disposable data is never worth durability: skip fsync and full-page writes.
# The image entrypoint passes leading "-c" arguments through to postgres.
container_name="odograph-testdb-${TASK_ID}-${RANDOM}${RANDOM}"
container_id="$(podman run -d --name "$container_name" \
    --label "$OWNER_LABEL=1" --label "$TASK_LABEL=$TASK_ID" \
    -e POSTGRES_DB=mileage -e POSTGRES_USER=mileage -e POSTGRES_PASSWORD=testpw \
    -p 127.0.0.1::5432 "$IMAGE" \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off)"

# shellcheck disable=SC2329 # Invoked by the EXIT trap until startup succeeds.
cleanup_failed_start() {
    podman rm -f --volumes "$container_id" >/dev/null 2>&1 || true
}
trap cleanup_failed_start EXIT

# Poll often so a fast start is not padded by a whole second; the default
# 150 x 0.2 s keeps the 30 s ceiling.
for ((attempt = 0; attempt < READY_ATTEMPTS; attempt++)); do
    if podman exec "$container_id" pg_isready -h 127.0.0.1 -U mileage -d mileage >/dev/null 2>&1; then
        host_port="$(podman port "$container_id" 5432/tcp | sed -n 's/^127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p')"
        if [ -n "$host_port" ]; then
            printf "export TEST_DATABASE_URL='postgresql://mileage:testpw@127.0.0.1:%s/mileage'\n" "$host_port"
            trap - EXIT
            exit 0
        fi
    fi
    sleep 0.2
done

echo "error: disposable database did not become ready: $container_name" >&2
exit 1
