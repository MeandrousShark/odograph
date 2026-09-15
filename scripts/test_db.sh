#!/usr/bin/env bash
# Starts and cleans up labelled disposable PostGIS containers for DB-backed
# tests. A task ID keeps concurrent test runs isolated from each other.
#
# Usage:
#   eval "$(scripts/test_db.sh start TASK_ID)"
#   scripts/test_db.sh cleanup TASK_ID
set -euo pipefail

IMAGE="${TEST_DB_IMAGE:-docker.io/postgis/postgis:16-3.4}"
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
            podman rm -f "$container_id" >/dev/null
        fi
    done <<< "$container_ids"
    exit 0
fi

container_name="odograph-testdb-${TASK_ID}-${RANDOM}${RANDOM}"
container_id="$(podman run -d --name "$container_name" \
    --label "$OWNER_LABEL=1" --label "$TASK_LABEL=$TASK_ID" \
    -e POSTGRES_DB=mileage -e POSTGRES_USER=mileage -e POSTGRES_PASSWORD=testpw \
    -p 127.0.0.1::5432 "$IMAGE")"

# shellcheck disable=SC2329 # Invoked by the ERR trap.
cleanup_failed_start() {
    podman rm -f "$container_id" >/dev/null 2>&1 || true
}
trap cleanup_failed_start ERR

for ((attempt = 0; attempt < 30; attempt++)); do
    if podman exec "$container_id" pg_isready -h 127.0.0.1 -U mileage -d mileage >/dev/null 2>&1; then
        host_port="$(podman port "$container_id" 5432/tcp | sed -n 's/^127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p')"
        if [ -n "$host_port" ]; then
            printf "export TEST_DATABASE_URL='postgresql://mileage:testpw@127.0.0.1:%s/mileage'\n" "$host_port"
            trap - ERR
            exit 0
        fi
    fi
    sleep 1
done

echo "error: disposable database did not become ready: $container_name" >&2
exit 1
