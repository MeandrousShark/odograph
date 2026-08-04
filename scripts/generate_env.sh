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

# Single source of truth for which variables get generated secrets. The awk
# substitution and the printed summary both derive from this array, so a
# later change to the set can't update one and miss the other.
GENERATED_VARS=(POSTGRES_PASSWORD INGEST_PASSWORD SESSION_SECRET ADMIN_TOKEN)

# This value is interpolated directly into DATABASE_URL. Hex retains 256
# bits of entropy without URI delimiters that would change its parse.
POSTGRES_PASSWORD="$(random_uri_secret)"
INGEST_PASSWORD="$(random_secret)"
SESSION_SECRET="$(random_secret)"
ADMIN_TOKEN="$(random_secret)"

# awk generates the whole file in one pass rather than editing in place:
# BSD sed (macOS) and GNU sed (Linux) take incompatible -i syntax, and
# writing fresh output sidesteps that difference entirely.
awk_args=()
awk_program=""
for var in "${GENERATED_VARS[@]}"; do
    awk_args+=(-v "${var}=${!var}")
    awk_program+="/^${var}=/ { print \"${var}=\" ${var}; next } "
done
awk_program+="{ print }"

awk "${awk_args[@]}" "$awk_program" "$ENV_EXAMPLE" > "$ENV_FILE"

# Secrets are inside; not world/group readable.
chmod 600 "$ENV_FILE"

cat <<EOF
Generated $ENV_FILE with fresh random values for:
$(printf '  %s\n' "${GENERATED_VARS[@]}")

Still needs your own input before starting the stack. See .env.example for
what each setting controls and README.md for the install walkthrough:
  - DISPLAY_TZ
  - FORWARDED_ALLOW_IPS (only if you're not using the loopback-bound
    reverse-proxy setup; see docs/reverse-proxy.md)
  - Optional services left commented out: OIDC, reverse geocoding, ntfy,
    and email
  - OSRM (road-snapping), opt-in; see docs/osrm.md
EOF
