#!/usr/bin/env bash
# Posts a short synthetic stay -> drive -> stay GPS track to a running
# instance's /ingest endpoint, under the fixed device id "test", so the
# detector produces one real visible trip without walking around the block.
# Run from the directory containing compose.yaml -- --cleanup shells out to
# the compose CLI to remove the test device's rows afterward.
#
# Usage:
#   scripts/send_test_track.sh [--base-url URL] [--password PASSWORD]
#   scripts/send_test_track.sh --cleanup
#
# Env vars (flags win if both are given):
#   BASE_URL          default http://127.0.0.1:8077
#   INGEST_PASSWORD   required unless --password is given
#   COMPOSE_CMD        override compose command autodetection, e.g. "podman-compose"
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8077}"
PASSWORD="${INGEST_PASSWORD:-}"
CLEANUP=0

usage() {
    echo "usage: $0 [--base-url URL] [--password PASSWORD] | --cleanup" >&2
    exit "${1:-1}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-url|--password)
            if [ "$#" -lt 2 ]; then
                echo "error: $1 requires a value" >&2
                usage
            fi
            if [ "$1" = "--base-url" ]; then
                BASE_URL="$2"
            else
                PASSWORD="$2"
            fi
            shift 2
            ;;
        --cleanup) CLEANUP=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "error: unrecognized argument: $1" >&2; usage ;;
    esac
done

detect_compose_cmd() {
    if [ -n "${COMPOSE_CMD:-}" ]; then
        echo "$COMPOSE_CMD"
        return
    fi
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        echo "docker compose"
        return
    fi
    if command -v podman-compose >/dev/null 2>&1; then
        echo "podman-compose"
        return
    fi
    echo "error: neither 'docker compose' nor 'podman-compose' found on PATH; set COMPOSE_CMD to override." >&2
    exit 1
}

if [ "$CLEANUP" -eq 1 ]; then
    compose_cmd="$(detect_compose_cmd)"
    echo "Removing all trace of device 'test' via: $compose_cmd exec -T db psql ..."
    # These four statements are the cleanup contract: tests/test_send_test_track_cleanup_db.py
    # runs the identical SQL directly against a seeded DB to pin this logic
    # against schema drift, so keep any change here mirrored there.
    $compose_cmd exec -T db psql -U mileage -d mileage -v ON_ERROR_STOP=1 \
        -c "DELETE FROM trips WHERE device = 'test';" \
        -c "DELETE FROM stays WHERE device = 'test';" \
        -c "DELETE FROM points WHERE device = 'test';" \
        -c "DELETE FROM raw_messages WHERE payload->>'tid' = 'test';"
    exit 0
fi

if [ -z "$PASSWORD" ]; then
    echo "error: no ingest password given; set INGEST_PASSWORD or pass --password." >&2
    exit 1
fi

# Point generation needs real trig and float arithmetic against a moving
# clock -- doing that in bash is unreadable and error-prone, so it's
# delegated to python3's stdlib (no third-party packages) and the JSON
# lines are read back one at a time for POSTing.
points_ndjson="$(python3 - <<'PYEOF'
import json
import math
import random
import time

M_PER_DEG_LAT = 111_320.0

# A public road segment inside Golden Gate Park, San Francisco -- illustrative
# fixed coordinates, not tied to any operator's real location.
START_LAT = 37.76940
START_LON = -122.48300
BEARING_DEG = 200.0
DRIVE_KM = 2.5
DRIVE_SPEED_KMH = 40.0
DRIVE_INTERVAL_S = 15.0
STAY_DURATION_S = 600.0       # 10 minutes at each end
STAY_INTERVAL_S = 60.0
END_MARGIN_S = 180.0          # track ends 3 minutes before "now"

rng = random.Random()


def offset(lat, lon, east_m, north_m):
    return (
        lat + north_m / M_PER_DEG_LAT,
        lon + east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat))),
    )


def travel(lat, lon, dist_m, bearing_deg):
    b = math.radians(bearing_deg)
    return offset(lat, lon, math.sin(b) * dist_m, math.cos(b) * dist_m)


points = []


def emit(t, lat, lon, vel_kmh, jitter_m):
    jlat, jlon = offset(lat, lon, rng.gauss(0, jitter_m), rng.gauss(0, jitter_m))
    points.append({
        "_type": "location", "tid": "test",
        "lat": round(jlat, 6), "lon": round(jlon, 6),
        "tst": int(t), "acc": 10, "vel": round(vel_kmh, 1),
    })


drive_duration_s = DRIVE_KM * 1000.0 / (DRIVE_SPEED_KMH / 3.6)
total_duration_s = STAY_DURATION_S + drive_duration_s + STAY_DURATION_S
end_ts = time.time() - END_MARGIN_S
t0 = end_ts - total_duration_s

lat, lon = START_LAT, START_LON

t = 0.0
while t <= STAY_DURATION_S + 1e-9:
    emit(t0 + t, lat, lon, 0.0, 5.0)
    t += STAY_INTERVAL_S
t_stay1_end = t0 + STAY_DURATION_S

elapsed = DRIVE_INTERVAL_S
while elapsed <= drive_duration_s + 1e-9:
    d = (DRIVE_SPEED_KMH / 3.6) * elapsed
    plat, plon = travel(lat, lon, d, BEARING_DEG)
    emit(t_stay1_end + elapsed, plat, plon, DRIVE_SPEED_KMH, 3.0)
    elapsed += DRIVE_INTERVAL_S
lat, lon = travel(lat, lon, DRIVE_KM * 1000.0, BEARING_DEG)
t_drive_end = t_stay1_end + drive_duration_s

t = 0.0
while t <= STAY_DURATION_S + 1e-9:
    emit(t_drive_end + t, lat, lon, 0.0, 5.0)
    t += STAY_INTERVAL_S

for p in points:
    print(json.dumps(p))
PYEOF
)"

point_count="$(printf '%s\n' "$points_ndjson" | grep -c .)"
echo "Sending $point_count points to ${BASE_URL%/}/ingest as device 'test'..."

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT

i=0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    i=$((i + 1))
    http_code="$(curl -sS -o "$response_file" -w '%{http_code}' \
        -u "owntracks:${PASSWORD}" \
        -H 'Content-Type: application/json' \
        --data-binary "$line" \
        "${BASE_URL%/}/ingest")"
    if [ "$http_code" != "200" ]; then
        echo "error: ingest POST #$i failed with HTTP $http_code" >&2
        cat "$response_file" >&2
        exit 1
    fi
done <<< "$points_ndjson"

echo "Sent $i points. Wait ~90 seconds for the detector's debounce, then check the trip list."
echo "When done testing, run: $0 --cleanup"
