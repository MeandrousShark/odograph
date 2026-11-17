#!/usr/bin/env bash
# Run the install block of docs/install-compose.md against a rendered install
# compose.yaml, from an empty directory, and check what an operator gets: a
# healthy app on loopback, a private .env with generated secrets, and a second
# run that refuses to touch either file.
#
#   scripts/install_smoke.sh COMPOSE_FILE
#
# The block's two download URLs are pointed at a local copy of COMPOSE_FILE and
# this checkout's .env.example; nothing else in it changes, except that
# COMPOSE_CMD (default "docker compose"; podman-compose works too) replaces
# `docker compose`. INSTALL_SMOKE_GUIDE overrides the guide path and
# INSTALL_SMOKE_TIMEOUT the seconds allowed for the app to become healthy.
set -euo pipefail

PORT=8077
HEALTH_URL="http://127.0.0.1:$PORT/healthz"
PROJECT_LABEL=com.docker.compose.project
RELEASE_PREFIX=https://github.com/MeandrousShark/odograph/releases/download/vX.Y.Z/
RAW_PREFIX=https://raw.githubusercontent.com/MeandrousShark/odograph/vX.Y.Z/
ADMIN_EMAIL=install-smoke@example.com
ADMIN_PASSWORD=install-smoke-password

[ $# -eq 1 ] || { echo "usage: $0 COMPOSE_FILE" >&2; exit 2; }
source_file="$1"
[ -f "$source_file" ] || { echo "error: $source_file not found" >&2; exit 2; }
root="$(cd "$(dirname "$0")/.." && pwd)"
guide="${INSTALL_SMOKE_GUIDE:-$root/docs/install-compose.md}"
[ -f "$guide" ] || { echo "error: $guide not found" >&2; exit 2; }

compose_cmd="${COMPOSE_CMD:-docker compose}"
read -r -a compose <<< "$compose_cmd"
case "${compose[0]}" in
    podman-compose | podman) runtime=podman ;;
    *) runtime=docker ;;
esac
timeout_s="${INSTALL_SMOKE_TIMEOUT:-600}"
# The shell environment outranks the block's COMPOSE_PROJECT_NAME in .env for
# both tools, so a run here can never act on an operator's real `odograph`.
PROJECT="odograph-install-smoke-$$"
export PYTHONUNBUFFERED=1

workdir="$(mktemp -d)"
parent="$workdir/parent"
install_dir="$parent/odograph"
up_log="$workdir/up.log"
started=false
mkdir "$workdir/source" "$parent"
cp "$source_file" "$workdir/source/compose.yaml"
cp "$root/.env.example" "$workdir/source/.env.example"
: > "$up_log"

fail() {
    echo "error: $*" >&2
    echo "--- install output" >&2
    tail -n 60 "$up_log" >&2 || true
    echo "--- containers" >&2
    "$runtime" ps -a --filter "label=$PROJECT_LABEL=$PROJECT" >&2 || true
    exit 1
}

run_compose() {
    (cd "$install_dir" && "${compose[@]}" -p "$PROJECT" "$@")
}

project_containers() {
    "$runtime" ps -aq --filter "label=$PROJECT_LABEL=$PROJECT"
}

cleanup() {
    status=$?
    trap - EXIT
    if [ "$started" = true ] && [ -f "$install_dir/compose.yaml" ]; then
        run_compose down -v >/dev/null 2>&1 || true
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

# The guide's first ```sh block after "## Install", with only the download
# prefixes and the Compose command rewritten. Each rewrite must apply, so a
# changed guide fails here instead of downloading from the internet.
python3 - "$guide" "$workdir/install.sh" "file://$workdir/source/" "$compose_cmd" \
    "$RELEASE_PREFIX" "$RAW_PREFIX" <<'PY'
import sys

guide, output, local, compose_cmd, release_prefix, raw_prefix = sys.argv[1:]
lines = open(guide, encoding="utf-8").read().splitlines()
try:
    start = lines.index("## Install")
    opening = lines.index("```sh", start + 1)
    closing = lines.index("```", opening + 1)
except ValueError:
    sys.exit(f"error: {guide} has no ```sh block under '## Install'")
block = "\n".join(lines[opening + 1:closing]) + "\n"
for prefix in (release_prefix, raw_prefix):
    if block.count(prefix) != 1:
        sys.exit(f"error: the install block names {prefix} {block.count(prefix)} times, not once")
    block = block.replace(prefix, local)
if block.count("docker compose ") < 1:
    sys.exit("error: the install block never runs docker compose")
block = block.replace("docker compose ", compose_cmd + " ")
open(output, "w", encoding="utf-8").write(block)
PY

"$runtime" ps -q >/dev/null || { echo "error: $runtime does not answer" >&2; exit 2; }
[ -z "$(project_containers)" ] || fail "project $PROJECT already has containers"
[ -z "$(listeners)" ] || fail "port $PORT is already in use"

started=true
# Plain sh, as a paste runs it: the block itself must stop and fail when its
# subshell does.
(cd "$parent" && COMPOSE_PROJECT_NAME="$PROJECT" sh "$workdir/install.sh") \
    >>"$up_log" 2>&1 </dev/null || fail "the install block failed"
echo "ok: the install block ran from an empty directory"

[ -n "$(project_containers)" ] || fail "no containers carry the project label $PROJECT"

deadline=$((SECONDS + timeout_s))
healthy=false
while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
        healthy=true
        break
    fi
    sleep 2
done
[ "$healthy" = true ] || fail "$HEALTH_URL did not answer within ${timeout_s}s"
echo "ok: $HEALTH_URL answers"

python3 - "$install_dir/.env" <<'PY' || fail ".env is not what the guide promises"
import os, re, stat, sys

path = sys.argv[1]
mode = stat.S_IMODE(os.stat(path).st_mode)
if mode != 0o600:
    sys.exit(f"error: .env mode is {mode:o}, expected 600")
lines = open(path, encoding="utf-8").read().splitlines()
for key in ("COMPOSE_PROJECT_NAME", "DISPLAY_TZ", "POSTGRES_PASSWORD", "SESSION_SECRET"):
    values = [line.split("=", 1)[1] for line in lines if line.startswith(key + "=")]
    if len(values) != 1:
        sys.exit(f"error: .env sets {key} {len(values)} times, not once")
    pattern = r"[0-9a-f]{64}" if key.endswith(("PASSWORD", "SECRET")) else r".+"
    if not re.fullmatch(pattern, values[0]):
        sys.exit(f"error: .env {key} is not a usable value")
PY
echo "ok: .env is 0600 with each key once and 64-hex secrets"

printf '%s\n%s\n%s\n' "$ADMIN_EMAIL" "$ADMIN_PASSWORD" "$ADMIN_PASSWORD" \
    | run_compose exec -T app python -m app.manage_account create-admin >>"$up_log" 2>&1 \
    || fail "create-admin failed"
run_compose exec -T app python -m app.manage_account list-accounts </dev/null 2>>"$up_log" \
    | grep -qF "$ADMIN_EMAIL" || fail "the new administrator is not listed"
echo "ok: create-admin works non-interactively"

before="$(cd "$install_dir" && cksum compose.yaml .env)"
if (cd "$parent" && COMPOSE_PROJECT_NAME="$PROJECT" sh "$workdir/install.sh") \
    >>"$up_log" 2>&1 </dev/null; then
    fail "a second run of the install block succeeded"
fi
[ "$(cd "$install_dir" && cksum compose.yaml .env)" = "$before" ] \
    || fail "a second run changed compose.yaml or .env"
echo "ok: a second run fails and leaves compose.yaml and .env alone"

run_compose down -v >>"$up_log" 2>&1 || fail "down failed"
started=false
[ -z "$(project_containers)" ] || fail "containers remain after down"
if "$runtime" volume ls -q | grep -q "^${PROJECT}_"; then
    fail "volumes remain after down -v"
fi
echo "ok: down -v removed the installation's containers and volumes"
