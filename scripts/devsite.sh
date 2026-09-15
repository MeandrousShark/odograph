#!/usr/bin/env bash
# Persistent local QA site: a fixed-port loopback database plus uvicorn
# running the current source tree, so visual checking against real routes
# and templates doesn't need a hand-rolled setup every time. Developer
# tooling only; ships no application behavior.
#
# Usage:
#   scripts/devsite.sh up
#   scripts/devsite.sh down
#   scripts/devsite.sh reseed
#   scripts/devsite.sh migrate
#   scripts/devsite.sh status
#   scripts/devsite.sh logs [app|db]
#
# `up` is idempotent: running it twice never starts a second database
# container or a second uvicorn process. `down` stops both and leaves the
# database's named volume in place, so a following `up` sees the same data.
#
# The QA database is named mileage_devsite, never mileage, so it falls
# outside tests/conftest.py's reset allowlist and a stray TEST_DATABASE_URL
# cannot truncate this data. Container labels are namespaced separately from
# scripts/test_db.sh's disposable containers so neither script can ever
# collect the other's containers.
#
# Env vars (all optional, defaults match the rest of this repository):
#   DEVSITE_DB_IMAGE   postgres/postgis image, default docker.io/postgis/postgis:16-3.4
#   DEVSITE_DB_PORT    fixed loopback database port, default 55432
#   DEVSITE_APP_PORT   fixed loopback app port, default 8078
#   DEVSITE_BIND_HOST  app bind address, default 127.0.0.1 (loopback-only).
#                       A non-loopback value (e.g. a Tailscale address) makes
#                       the QA site reachable from other machines, and with
#                       the generated DEV_NO_AUTH=1 that means unauthenticated
#                       admin access for anyone who can reach it.
#   DEVSITE_PYTHON     python interpreter, default REPO_ROOT/.venv/bin/python
#   DEVSITE_UVICORN    uvicorn executable, default REPO_ROOT/.venv/bin/uvicorn
#   DEVSITE_STATE_DIR  pid/log working directory, default REPO_ROOT/.devsite
#   DEVSITE_ENV_FILE   generated secrets file, default REPO_ROOT/.env.devsite
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DB_IMAGE="${DEVSITE_DB_IMAGE:-docker.io/postgis/postgis:16-3.4}"
DB_NAME="mileage_devsite"
DB_USER="mileage"
DB_CONTAINER="odograph-devsite-db"
DB_VOLUME="odograph-devsite-dbdata"
DB_PORT="${DEVSITE_DB_PORT:-55432}"
APP_PORT="${DEVSITE_APP_PORT:-8078}"
BIND_HOST="${DEVSITE_BIND_HOST:-127.0.0.1}"
OWNER_LABEL="io.odograph.devsite"

STATE_DIR="${DEVSITE_STATE_DIR:-$REPO_ROOT/.devsite}"
ENV_FILE="${DEVSITE_ENV_FILE:-$REPO_ROOT/.env.devsite}"
PID_FILE="$STATE_DIR/uvicorn.pid"
LOG_FILE="$STATE_DIR/uvicorn.log"

PYTHON_BIN="${DEVSITE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
UVICORN_BIN="${DEVSITE_UVICORN:-$REPO_ROOT/.venv/bin/uvicorn}"

usage() {
    cat >&2 <<'EOF'
usage: scripts/devsite.sh up|down|reseed|migrate|status|logs [app|db]

  up       start the QA database and uvicorn --reload, generating
           .env.devsite on first run
  down     stop both, preserving the database's named volume
  reseed   wipe and re-run scripts/dev_seed.py for a clean dataset
  migrate  apply new migrations to the existing QA data
  status   report what is running and on which port
  logs     print recent output (app's uvicorn log by default, or db)

  Set DEVSITE_BIND_HOST to bind the app to a non-loopback address (e.g. a
  Tailscale address) for real-device QA. Defaults to loopback-only.
EOF
    exit "${1:-1}"
}

require_executable() {
    local bin="$1" hint="$2"
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "error: $bin not found. $hint" >&2
        exit 1
    fi
}

port_in_use() {
    local host="$1" port="$2"
    (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null
}

random_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    else
        echo "error: OpenSSL or Python 3 is required to generate secrets." >&2
        exit 1
    fi
}

random_uri_secret() {
    # Hex, not base64: this value is interpolated into a postgresql:// URL,
    # and hex retains 256 bits of entropy without introducing URI delimiters
    # that would change how the URL parses (same reasoning as
    # generate_env.sh's POSTGRES_PASSWORD).
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import secrets; print(secrets.token_hex(32))"
    else
        echo "error: OpenSSL or Python 3 is required to generate secrets." >&2
        exit 1
    fi
}

# The database password lives only inside DATABASE_URL in .env.devsite, not
# in a second variable that could drift from it. Format is always the one
# generated below: postgresql://user:password@host:port/db.
db_password_from_url() {
    local url="$1" rest
    rest="${url#*://}"
    rest="${rest%%@*}"
    printf '%s\n' "${rest#*:}"
}

generate_env_file() {
    if [ -e "$ENV_FILE" ]; then
        return
    fi
    local db_password ingest_password session_secret
    db_password="$(random_uri_secret)"
    ingest_password="$(random_secret)"
    session_secret="$(random_secret)"
    umask 077
    cat > "$ENV_FILE" <<EOF
# Generated by scripts/devsite.sh. Loopback-only local QA site with
# synthetic data and DEV_NO_AUTH=1 by default. Never copy this file to a
# deployed instance and never commit it (already gitignored).
DATABASE_URL=postgresql://$DB_USER:$db_password@127.0.0.1:$DB_PORT/$DB_NAME
INGEST_PASSWORD=$ingest_password
SESSION_SECRET=$session_secret
DEV_NO_AUTH=1
EOF
    chmod 600 "$ENV_FILE"
    echo "Generated $ENV_FILE with fresh secrets."
}

load_env_file() {
    if [ ! -e "$ENV_FILE" ]; then
        echo "error: $ENV_FILE not found; run 'scripts/devsite.sh up' first." >&2
        exit 1
    fi
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set +a
}

db_container_exists() {
    podman container exists "$DB_CONTAINER"
}

db_container_running() {
    [ "$(podman inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null)" = "true" ]
}

wait_for_db_ready() {
    local attempt
    for ((attempt = 0; attempt < 30; attempt++)); do
        if podman exec "$DB_CONTAINER" pg_isready -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "error: QA database did not become ready: $DB_CONTAINER" >&2
    exit 1
}

create_db_container() {
    if port_in_use "127.0.0.1" "$DB_PORT"; then
        echo "error: port $DB_PORT (QA database) is already in use. Set DEVSITE_DB_PORT to use a different one." >&2
        exit 1
    fi
    if podman volume exists "$DB_VOLUME"; then
        echo "error: QA volume $DB_VOLUME exists without its container; refusing to attach a new database image to existing data." >&2
        echo "Recover it with the original image or use a verified backup and a fresh volume. See docs/backups.md." >&2
        exit 1
    fi
    podman volume create "$DB_VOLUME" >/dev/null

    local db_password
    db_password="$(db_password_from_url "$DATABASE_URL")"

    echo "Creating QA database container ($DB_CONTAINER) on 127.0.0.1:$DB_PORT..."
    podman run -d --name "$DB_CONTAINER" \
        --label "$OWNER_LABEL=1" \
        -e POSTGRES_DB="$DB_NAME" -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD="$db_password" \
        -v "$DB_VOLUME:/var/lib/postgresql/data" \
        -p "127.0.0.1:$DB_PORT:5432" \
        "$DB_IMAGE" >/dev/null
}

# Used by `up`: create the persistent container on first run, otherwise
# start the existing one if it isn't already running. Never removes or
# recreates an existing container, so its named volume's data is untouched.
start_or_create_db_container() {
    if db_container_exists; then
        if db_container_running; then
            echo "QA database container already running."
        else
            echo "Starting existing QA database container..."
            podman start "$DB_CONTAINER" >/dev/null
        fi
        return
    fi
    create_db_container
}

# Used by `migrate` and `reseed`: operates only on data that must already
# exist, so a missing container is an error rather than something to create.
require_existing_db_container() {
    if ! db_container_exists; then
        echo "error: no QA database container found; run 'scripts/devsite.sh up' first." >&2
        exit 1
    fi
    if ! db_container_running; then
        echo "Starting QA database container..."
        podman start "$DB_CONTAINER" >/dev/null
    fi
}

uvicorn_pid_running() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

host_is_loopback() {
    case "$1" in
        127.0.0.1|::1|localhost) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_app_ready() {
    local attempt
    for ((attempt = 0; attempt < 30; attempt++)); do
        if ! uvicorn_pid_running; then
            echo "error: uvicorn exited before becoming ready; see $LOG_FILE" >&2
            tail -n 40 "$LOG_FILE" >&2 || true
            exit 1
        fi
        # The slim production image has no curl (see compose.yaml), so a
        # Python one-liner against the already-running interpreter is the
        # existing convention for a dependency-free health probe.
        if "$PYTHON_BIN" -c "
import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen('http://$BIND_HOST:$APP_PORT/healthz', timeout=1).status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "error: uvicorn did not become ready on port $APP_PORT; see $LOG_FILE" >&2
    exit 1
}

ensure_uvicorn_running() {
    if uvicorn_pid_running; then
        echo "uvicorn already running (pid $(cat "$PID_FILE"), port $APP_PORT)."
        return
    fi
    rm -f "$PID_FILE"

    if port_in_use "$BIND_HOST" "$APP_PORT"; then
        echo "error: port $APP_PORT (QA app) is already in use. Set DEVSITE_APP_PORT to use a different one." >&2
        exit 1
    fi

    require_executable "$UVICORN_BIN" \
        "Run 'python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt' first, or set DEVSITE_UVICORN."

    if ! host_is_loopback "$BIND_HOST"; then
        echo "warning: binding the QA app to $BIND_HOST, not loopback. The QA site" >&2
        echo "         will be reachable from other machines, and DEV_NO_AUTH=1" >&2
        echo "         (the generated default) means anyone who can reach it gets" >&2
        echo "         unauthenticated admin access." >&2
    fi

    echo "Starting uvicorn --reload on $BIND_HOST:$APP_PORT..."
    (
        cd "$REPO_ROOT"
        # nohup execs uvicorn in place rather than forking, so $! below is
        # uvicorn's own pid, not a wrapper's -- that's what down/status/logs
        # need to track.
        nohup "$UVICORN_BIN" app.main:create_app --factory \
            --host "$BIND_HOST" --port "$APP_PORT" --reload \
            >>"$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
    )
    wait_for_app_ready
}

stop_uvicorn() {
    if ! uvicorn_pid_running; then
        echo "uvicorn is not running."
        rm -f "$PID_FILE"
        return
    fi
    local pid attempt
    pid="$(cat "$PID_FILE")"
    echo "Stopping uvicorn (pid $pid)..."
    kill "$pid" 2>/dev/null || true
    for ((attempt = 0; attempt < 10; attempt++)); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
}

cmd_up() {
    mkdir -p "$STATE_DIR"
    generate_env_file
    load_env_file
    start_or_create_db_container
    wait_for_db_ready
    ensure_uvicorn_running
    echo
    cmd_status
    echo
    echo "Visual QA site: http://$BIND_HOST:$APP_PORT"
}

cmd_down() {
    mkdir -p "$STATE_DIR"
    stop_uvicorn
    if db_container_exists; then
        if db_container_running; then
            echo "Stopping QA database container..."
            podman stop "$DB_CONTAINER" >/dev/null
        else
            echo "QA database container already stopped."
        fi
    else
        echo "QA database container does not exist."
    fi
}

cmd_reseed() {
    load_env_file
    require_existing_db_container
    wait_for_db_ready
    require_executable "$PYTHON_BIN" \
        "Run 'python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt' first, or set DEVSITE_PYTHON."
    echo "Wiping and reseeding $DB_NAME..."
    (cd "$REPO_ROOT" && "$PYTHON_BIN" scripts/dev_seed.py --database-url "$DATABASE_URL" --wipe)
}

cmd_migrate() {
    load_env_file
    require_existing_db_container
    wait_for_db_ready
    require_executable "$PYTHON_BIN" \
        "Run 'python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt' first, or set DEVSITE_PYTHON."
    echo "Applying migrations to $DB_NAME..."
    # DATABASE_URL is read from the environment inside the interpreter,
    # not interpolated into this string, so a password with shell-special
    # characters can never change what this actually runs.
    (cd "$REPO_ROOT" && "$PYTHON_BIN" -c '
import asyncio
import os
from app.db import make_pool, run_migrations

async def _run():
    pool = make_pool(os.environ["DATABASE_URL"])
    await pool.open(wait=True)
    try:
        await run_migrations(pool)
    finally:
        await pool.close()

asyncio.run(_run())
')
    echo "Migrations applied."
}

cmd_status() {
    echo "QA database ($DB_CONTAINER):"
    if db_container_exists; then
        if db_container_running; then
            echo "  running, 127.0.0.1:$DB_PORT, database $DB_NAME"
        else
            echo "  stopped (data preserved in volume $DB_VOLUME)"
        fi
    else
        echo "  not created yet"
    fi

    echo "QA app (uvicorn):"
    if uvicorn_pid_running; then
        echo "  running, pid $(cat "$PID_FILE"), http://$BIND_HOST:$APP_PORT"
    else
        echo "  not running"
    fi
}

cmd_logs() {
    local target="${1:-app}"
    case "$target" in
        app)
            if [ ! -f "$LOG_FILE" ]; then
                echo "no uvicorn log yet: $LOG_FILE" >&2
                exit 1
            fi
            tail -n 200 "$LOG_FILE"
            ;;
        db)
            podman logs --tail 200 "$DB_CONTAINER"
            ;;
        *)
            echo "usage: scripts/devsite.sh logs [app|db]" >&2
            exit 1
            ;;
    esac
}

[ "$#" -ge 1 ] || usage
ACTION="$1"
shift

case "$ACTION" in
    up) cmd_up "$@" ;;
    down) cmd_down "$@" ;;
    reseed) cmd_reseed "$@" ;;
    migrate) cmd_migrate "$@" ;;
    status) cmd_status "$@" ;;
    logs) cmd_logs "$@" ;;
    -h|--help) usage 0 ;;
    *) usage ;;
esac
