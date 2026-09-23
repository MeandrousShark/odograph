#!/usr/bin/env bash
# Smoke-test one native release image against a disposable database.
set -euo pipefail

usage() {
    echo "usage: scripts/release_preflight_smoke.sh IMAGE VERSION REVISION EVIDENCE [DB_PLATFORM]" >&2
    exit "${1:-1}"
}

[ "$#" -ge 4 ] && [ "$#" -le 5 ] || usage
IMAGE="$1"
VERSION="$2"
REVISION="$3"
EVIDENCE="$4"
DB_PLATFORM="${5:-linux/$(docker version --format '{{.Server.Arch}}')}"
DB_IMAGE="${POSTGIS_IMAGE:-ghcr.io/meandrousshark/odograph-postgis@sha256:b352024dd6f9ca2ba0f1e7125dcfdcf78b824f2cbe86edf89559e1ddf4d80241}"

stamp="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${RUNNER_ARCH:-unknown}-$$"
stamp="$(printf '%s' "$stamp" | tr '[:upper:]' '[:lower:]')"
network="odograph-preflight-${stamp}"
db="${network}-db"
app="${network}-app"
base_url="https://127.0.0.1:18443"
cookie_jar="$(mktemp "${RUNNER_TEMP:-/tmp}/odograph-preflight-cookie.XXXXXX")"
signup_html="$(mktemp "${RUNNER_TEMP:-/tmp}/odograph-preflight-signup.XXXXXX")"
headers="$(mktemp "${RUNNER_TEMP:-/tmp}/odograph-preflight-headers.XXXXXX")"
settings_html="$(mktemp "${RUNNER_TEMP:-/tmp}/odograph-preflight-settings.XXXXXX")"
tls_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/odograph-preflight-tls.XXXXXX")"
tls_key="$tls_dir/key.pem"
tls_cert="$tls_dir/cert.pem"

cleanup() {
    local exit_code=$?
    if docker container inspect "$app" >/dev/null 2>&1; then
        docker logs "$app" >> "${EVIDENCE}.app.log" 2>&1 || true
    fi
    if docker container inspect "$db" >/dev/null 2>&1; then
        docker logs "$db" >> "${EVIDENCE}.db.log" 2>&1 || true
    fi
    docker rm -fv "$app" "$db" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    rm -f -- "$cookie_jar" "$signup_html" "$headers" "$settings_html"
    rm -rf -- "$tls_dir"
    exit "$exit_code"
}
trap cleanup EXIT

mkdir -p "$(dirname "$EVIDENCE")"
: > "$EVIDENCE"
openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes \
    -subj /CN=localhost \
    -addext subjectAltName=DNS:localhost,IP:127.0.0.1 \
    -keyout "$tls_key" -out "$tls_cert" >/dev/null 2>&1
chmod 755 "$tls_dir"
chmod 644 "$tls_key" "$tls_cert"
docker network create "$network" >/dev/null
docker run -d --name "$db" --network "$network" --platform "$DB_PLATFORM" \
    -e POSTGRES_DB=mileage \
    -e POSTGRES_USER=mileage \
    -e POSTGRES_PASSWORD=testpw \
    "$DB_IMAGE" >/dev/null

deadline=$(( $(date +%s) + 180 ))
until docker exec "$db" pg_isready -h 127.0.0.1 -U mileage -d mileage >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || {
        echo "disposable PostGIS did not become ready" >&2
        exit 1
    }
    sleep 2
done

docker run -d --name "$app" --network "$network" \
    -p 127.0.0.1:18443:8443 \
    --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
    --mount type=bind,source="$tls_dir",target=/tls,readonly \
    -e DATABASE_URL=postgresql://mileage:testpw@"$db":5432/mileage \
    -e INGEST_PASSWORD=preflight-ingest-password \
    -e SESSION_SECRET=preflight-session-secret-with-enough-entropy \
    -e INITIAL_ADMIN_SIGNUP=1 \
    -e DISPLAY_TZ=UTC \
    -e FORWARDED_ALLOW_IPS=127.0.0.1 \
    "$IMAGE" uvicorn app.main:create_app --factory \
    --host 0.0.0.0 --port 8443 --proxy-headers \
    --ssl-keyfile /tls/key.pem --ssl-certfile /tls/cert.pem >/dev/null

deadline=$(( $(date +%s) + 240 ))
until [ "$(curl --cacert "$tls_cert" -sS -m 5 -o /dev/null -w '%{http_code}' "$base_url/healthz" 2>/dev/null)" = "200" ]; do
    [ "$(date +%s)" -lt "$deadline" ] || {
        echo "application did not become healthy" >&2
        exit 1
    }
    sleep 2
done

health="$(curl --cacert "$tls_cert" -fsS "$base_url/healthz")"
[ "$health" = '{"ok":true}' ] || {
    echo "unexpected /healthz response: $health" >&2
    exit 1
}

status="$(curl --cacert "$tls_cert" -sS -D "$headers" -o /dev/null -w '%{http_code}' "$base_url/settings")"
if [ "$status" != "303" ] || \
   ! tr -d '\r' < "$headers" | grep -qi '^location: /login$'; then
    echo "protected settings page did not redirect to login" >&2
    exit 1
fi

curl --cacert "$tls_cert" -fsS -D "$headers" -c "$cookie_jar" \
    -o "$signup_html" "$base_url/signup"
tr -d '\r' < "$headers" | grep -qi '^set-cookie: session=.*;.*secure' || {
    echo "signup session cookie was not marked Secure" >&2
    exit 1
}
csrf="$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$signup_html" | head -n1)"
[ -n "$csrf" ] || {
    echo "could not establish the signup session" >&2
    exit 1
}

status="$(curl --cacert "$tls_cert" -sS -b "$cookie_jar" -c "$cookie_jar" \
    -D "$headers" -o /dev/null -w '%{http_code}' \
    --data-urlencode email=preflight-admin@example.test \
    --data-urlencode password=preflight-password-12345 \
    --data-urlencode password_confirm=preflight-password-12345 \
    --data-urlencode "csrf_token=$csrf" \
    "$base_url/signup")"
[ "$status" = "303" ] || {
    echo "administrator signup failed with HTTP $status" >&2
    exit 1
}
tr -d '\r' < "$headers" | grep -qi '^set-cookie: session=.*;.*secure' || {
    echo "authenticated session cookie was not marked Secure" >&2
    exit 1
}

curl --cacert "$tls_cert" -fsS -b "$cookie_jar" \
    -o "$settings_html" "$base_url/settings"
grep -Fq "<dt>App version</dt><dd><code>${VERSION}</code>" "$settings_html"
grep -Fq "<dt>Git revision</dt><dd><code>${REVISION}</code>" "$settings_html"

{
    echo "platform: $(uname -s)/$(uname -m)"
    echo "image: $IMAGE"
    echo "database image: $DB_IMAGE"
    echo "database platform: $DB_PLATFORM"
    docker exec "$db" psql -U mileage -d mileage -Atc \
        'SELECT version(); SELECT postgis_full_version();'
    echo "version: $VERSION"
    echo "revision: $REVISION"
    echo "healthz over TLS: 200 {\"ok\":true}"
    echo "unauthenticated /settings: 303 /login"
    echo "Secure session cookie: present"
    echo "authenticated identity: matched"
} >> "$EVIDENCE"
