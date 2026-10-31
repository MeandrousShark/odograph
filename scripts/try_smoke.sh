#!/usr/bin/env bash
# Run a compose.try.yaml from an empty directory and check what a stranger gets:
# a seeded dashboard with the banner on loopback, and nothing left after `down`.
#
#   scripts/try_smoke.sh COMPOSE_FILE
#
# COMPOSE_CMD selects the tool (default "docker compose"; podman-compose works
# too). The repository file also needs ODOGRAPH_TRY_IMAGE; a release asset does
# not. TRY_SMOKE_TIMEOUT is the seconds allowed for pulls, start and seed.
set -euo pipefail

PROJECT=odograph-try
SERVICE_LABEL=com.docker.compose.service
PROJECT_LABEL=com.docker.compose.project
BASE_URL=http://127.0.0.1:8079
PORT=8079
NOTICE="Demo mode: synthetic data, not persistent, authentication disabled."

[ $# -eq 1 ] || { echo "usage: $0 COMPOSE_FILE" >&2; exit 2; }
source_file="$1"
[ -f "$source_file" ] || { echo "error: $source_file not found" >&2; exit 2; }

read -r -a compose <<< "${COMPOSE_CMD:-docker compose}"
case "${compose[0]}" in
    podman-compose | podman) runtime=podman ;;
    *) runtime=docker ;;
esac
timeout_s="${TRY_SMOKE_TIMEOUT:-600}"

workdir="$(mktemp -d)"
up_log="$workdir/up.log"
up_pid=""
brought_down=false
cp "$source_file" "$workdir/compose.try.yaml"
cd "$workdir"

fail() {
    echo "error: $*" >&2
    echo "--- compose output" >&2
    tail -n 60 "$up_log" >&2 || true
    exit 1
}

run_compose() {
    "${compose[@]}" -p "$PROJECT" -f compose.try.yaml "$@"
}

project_containers() {
    "$runtime" ps -aq --filter "label=$PROJECT_LABEL=$PROJECT"
}

cleanup() {
    status=$?
    trap - EXIT
    if [ "$brought_down" = false ]; then
        run_compose down -v >/dev/null 2>&1 || true
    fi
    if [ -n "$up_pid" ]; then
        kill "$up_pid" 2>/dev/null || true
    fi
    rm -rf "$workdir"
    exit "$status"
}
trap cleanup EXIT

listeners() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH "sport = :$PORT" | awk '{print $4}'
    else
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fn | sed -n 's/^n//p'
    fi
}

[ -z "$(project_containers)" ] || { brought_down=true; fail "project $PROJECT already has containers"; }
[ -z "$(listeners)" ] || { brought_down=true; fail "port $PORT is already in use"; }
volumes_before="$("$runtime" volume ls -q | sort)"

run_compose up >"$up_log" 2>&1 </dev/null &
up_pid=$!

deadline=$((SECONDS + timeout_s))
seed_status=""
while [ "$SECONDS" -lt "$deadline" ]; do
    seed_id="$("$runtime" ps -aq --filter "label=$PROJECT_LABEL=$PROJECT" \
        --filter "label=$SERVICE_LABEL=try-seed" | head -n 1)"
    if [ -n "$seed_id" ]; then
        seed_status="$("$runtime" inspect --format '{{.State.Status}}' "$seed_id")"
        [ "$seed_status" = exited ] && break
    fi
    kill -0 "$up_pid" 2>/dev/null || [ -n "$seed_id" ] || fail "compose exited before the seed started"
    sleep 2
done
[ "$seed_status" = exited ] || fail "try-seed did not exit within ${timeout_s}s"
seed_exit="$("$runtime" inspect --format '{{.State.ExitCode}}' "$seed_id")"
[ "$seed_exit" = 0 ] || fail "try-seed exited with $seed_exit"
echo "ok: try-seed exited 0"

dashboard="$workdir/dashboard.html"
status="$(curl -sS -o "$dashboard" -w '%{http_code}' "$BASE_URL/")" || fail "dashboard unreachable"
[ "$status" = 200 ] || fail "dashboard returned $status"
grep -qF "$NOTICE" "$dashboard" || fail "dashboard has no banner"
echo "ok: dashboard 200 with the banner"

trips="$workdir/trips.html"
curl -sS -o "$trips" "$BASE_URL/trips" || fail "/trips unreachable"
grep -q 'trip-archive-row' "$trips" || fail "no seeded trips on /trips"
echo "ok: seeded trips present"

bound_addresses="$(listeners)"
[ -n "$bound_addresses" ] || fail "nothing listens on port $PORT"
while read -r address; do
    [ "$address" = "127.0.0.1:$PORT" ] || fail "port $PORT is bound on $address"
done <<< "$bound_addresses"
# shellcheck disable=SC2046 # one argument per container ID
"$runtime" inspect --format '{{json .HostConfig.PortBindings}}' $(project_containers) | python3 -c '
import json, sys
bound = [binding for line in sys.stdin for bindings in (json.loads(line) or {}).values()
         for binding in bindings or []]
sys.exit(not all(b["HostIp"] == "127.0.0.1" and b["HostPort"] == "8079" for b in bound) or len(bound) != 1)
' || fail "a container publishes something other than 127.0.0.1:$PORT"
echo "ok: only 127.0.0.1:$PORT is bound"

sleep 2
grep -F 'seed' "$up_log" > "$workdir/seed.log" || true
if grep -qF "$NOTICE" "$workdir/seed.log" && grep -qF "$BASE_URL" "$workdir/seed.log"; then
    seed_lines=yes
else
    seed_lines=no
fi
echo "seed-final-lines-in-up-output: $seed_lines (${compose[*]})"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "Seed's final lines in plain \`up\` output (${compose[*]}): $seed_lines" >> "$GITHUB_STEP_SUMMARY"
fi

run_compose down -v >/dev/null 2>&1 || fail "down failed"
brought_down=true
wait "$up_pid" 2>/dev/null || true
up_pid=""

[ -z "$(project_containers)" ] || fail "containers remain after down"
if "$runtime" network ls --format '{{.Name}}' | grep -qx "$PROJECT"; then
    fail "network $PROJECT remains after down"
fi
[ "$("$runtime" volume ls -q | sort)" = "$volumes_before" ] || fail "volumes changed"
[ -z "$(listeners)" ] || fail "port $PORT is still bound after down"
echo "ok: down left no containers, network or volumes"
