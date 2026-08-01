#!/usr/bin/env bash
# Generates a fresh .env from .env.example, filling the instance secrets
# with high-entropy random values. Refuses to run if .env already exists:
# regenerating secrets under a live deployment would invalidate the running
# database password, ingest credential, session signing key, and setup
# token out from under the operator without warning.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
ENV_FILE="$REPO_ROOT/.env"

if [ -e "$ENV_FILE" ]; then
    echo "error: $ENV_FILE already exists; refusing to overwrite it." >&2
    echo "Delete or move it first if you really want a fresh set of secrets." >&2
    exit 1
fi

if [ ! -e "$ENV_EXAMPLE" ]; then
    echo "error: $ENV_EXAMPLE not found." >&2
    exit 1
fi

random_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32
    else
        python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    fi
}

random_uri_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        python3 -c "import secrets; print(secrets.token_hex(32))"
    fi
}

# This value is interpolated directly into DATABASE_URL. Hex retains 256
# bits of entropy without URI delimiters that would change its parse.
POSTGRES_PASSWORD="$(random_uri_secret)"
INGEST_PASSWORD="$(random_secret)"
SESSION_SECRET="$(random_secret)"
ADMIN_TOKEN="$(random_secret)"

# awk generates the whole file in one pass rather than editing in place:
# BSD sed (macOS) and GNU sed (Linux) take incompatible -i syntax, and
# writing fresh output sidesteps that difference entirely.
awk -v pw="$POSTGRES_PASSWORD" -v ip="$INGEST_PASSWORD" \
    -v ss="$SESSION_SECRET" -v at="$ADMIN_TOKEN" '
    /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" pw; next }
    /^INGEST_PASSWORD=/   { print "INGEST_PASSWORD=" ip; next }
    /^SESSION_SECRET=/    { print "SESSION_SECRET=" ss; next }
    /^ADMIN_TOKEN=/       { print "ADMIN_TOKEN=" at; next }
    { print }
' "$ENV_EXAMPLE" > "$ENV_FILE"

# Secrets are inside; not world/group readable.
chmod 600 "$ENV_FILE"

cat <<EOF
Generated $ENV_FILE with fresh random values for:
  POSTGRES_PASSWORD
  INGEST_PASSWORD
  SESSION_SECRET
  ADMIN_TOKEN

Still needs your own input before starting the stack:
  DISPLAY_TZ           - your IANA timezone (defaults to America/New_York)
  HTTPS reverse proxy  - configure your domain before browser setup; see
                         docs/reverse-proxy.md
  FORWARDED_ALLOW_IPS  - '*' is safe only because port 8077 stays loopback-bound
                         behind a trusted proxy -- both failed-auth rate
                         limiters key off the client address this produces,
                         so publishing the port with '*' still set lets a
                         client spoof X-Forwarded-For and dodge them. Set it
                         to your proxy's exact IP/CIDR if you widen the bind
                         or proxy topology.
  Optional services left commented out: OIDC, reverse geocoding, ntfy, and
  email. Uncomment and fill in only the ones you use.
  OSRM (road-snapping) is opt-in: it ships with no dataset configured, so
  provision your own region's extract with scripts/provision_osrm.sh before
  starting it with --profile osrm; see docs/osrm.md.
EOF
