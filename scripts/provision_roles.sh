#!/usr/bin/env bash
# Fixed disposable P0 fixture only. No database URL or secret enters argv.
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
exec "$PYTHON" -m app.role_setup "$@"
