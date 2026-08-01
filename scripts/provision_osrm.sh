#!/usr/bin/env bash
# Provisions the "osrmdata" volume from a Geofabrik regional extract by
# running osrm-extract -> osrm-partition -> osrm-customize against it
# inside the same osrm-backend image compose.yaml's `osrm` service already
# uses. Every step runs through `compose run` against that one service
# definition -- the image tag is never re-declared here, so the image that
# builds the dataset can't silently drift from the image that serves it.
#
# This also never assumes the host can write into a named Docker/Podman
# volume directly, since that isn't portable across the docker-compose/
# podman-compose split this project supports; the download lands in a
# host-side staging directory that gets bind-mounted read-only into a
# throwaway container, which then copies it into the volume itself.
#
# Usage:
#   scripts/provision_osrm.sh <geofabrik-extract-url>
#
# <geofabrik-extract-url> is a *.osm.pbf download URL from
# https://download.geofabrik.de/, e.g.
# https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf
# See docs/osrm.md for how to pick the right extract and size a host for it.
#
# On success, prints the exact OSRM_DATASET value to set in .env.
#
# Re-running this script is safe, but REPLACES the current contents of the
# "osrmdata" volume: the previous dataset is gone as soon as the prepare
# step below runs, even if the new extract then fails partway through.
#
# Env vars:
#   COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: scripts/provision_osrm.sh <geofabrik-extract-url>

Downloads a Geofabrik *.osm.pbf regional extract and runs
osrm-extract -> osrm-partition -> osrm-customize against it inside the
osrm-backend image already pinned in compose.yaml, writing the result into
the "osrmdata" volume. Prints the OSRM_DATASET value to put in .env on
success. Run from the directory containing compose.yaml.

Re-running this script is safe, but REPLACES the current contents of the
"osrmdata" volume -- the previous dataset is gone as soon as the prepare
step runs, even if the new run then fails.

Env vars:
  COMPOSE_CMD   override compose command autodetection, e.g. "podman-compose"
EOF
    exit "${1:-1}"
}

if [ "$#" -eq 1 ]; then
    case "$1" in
        -h|--help) usage 0 ;;
    esac
fi

if [ "$#" -ne 1 ]; then
    echo "error: expected exactly one <geofabrik-extract-url> argument, got $#." >&2
    usage
fi
URL="$1"

case "$URL" in
    http://*.osm.pbf|https://*.osm.pbf) ;;
    *)
        echo "error: bad URL: '$URL' doesn't look like a Geofabrik *.osm.pbf extract (expected http(s)://.../<region>-latest.osm.pbf). Find one at https://download.geofabrik.de/ -- see docs/osrm.md for guidance on picking a region." >&2
        exit 1
        ;;
esac

if [ ! -f compose.yaml ]; then
    echo "error: compose.yaml not found in the current directory; run this script from the canonical installation checkout." >&2
    exit 1
fi

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

compose_cmd="$(detect_compose_cmd)"

pbf_filename="$(basename -- "$URL")"
dataset_basename="${pbf_filename%.osm.pbf}"
dataset_value="${dataset_basename}.osrm"

# compose.yaml's `osrm` service command interpolates ${OSRM_DATASET:?...},
# and Compose interpolates the whole file before any CLI override is
# applied -- so even `run`, which replaces that command outright, still
# fails at the interpolation stage if OSRM_DATASET is unset. A fresh
# install has it unset or commented out by design (that's the point of the
# guard: catch anyone who skips this script), so without a placeholder,
# provisioning could never satisfy its own prerequisite. The placeholder
# value itself is never used -- every invocation below supplies its own
# explicit command, ignoring whatever compose.yaml's `command:` resolves to.
export OSRM_DATASET="${OSRM_DATASET:-osrm-provisioning-placeholder}"

MIN_STAGE_FREE_KB=524288   # 512 MiB floor, just to catch an obviously-full
                            # disk before downloading anything. Actual
                            # space needed varies hugely by region -- see
                            # docs/osrm.md for sizing guidance.
stage_free_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
if [ -n "$stage_free_kb" ] && [ "$stage_free_kb" -lt "$MIN_STAGE_FREE_KB" ]; then
    echo "error: insufficient disk space to stage the download here ($((stage_free_kb / 1024)) MiB free, want at least $((MIN_STAGE_FREE_KB / 1024)) MiB just to start). Free up space and re-run. See docs/osrm.md for sizing guidance by region." >&2
    exit 1
fi

stage_dir="$(mktemp -d ./.osrm-provision.XXXXXX)"
stage_dir="$(cd "$stage_dir" && pwd)"
cleanup() {
    rm -rf -- "$stage_dir"
}
trap cleanup EXIT

echo "Downloading $URL ..."
set +e
curl -fSL --retry 3 --retry-delay 2 -o "$stage_dir/$pbf_filename" "$URL"
curl_code=$?
set -e
if [ "$curl_code" -ne 0 ]; then
    case "$curl_code" in
        6|7)
            echo "error: bad URL -- could not resolve or connect to the host in '$URL'. Check the URL; see https://download.geofabrik.de/." >&2
            ;;
        22)
            echo "error: bad URL -- the server returned an HTTP error status for '$URL'. Geofabrik extract paths change when a region is renamed or re-published; re-copy the exact link from https://download.geofabrik.de/ rather than guessing at one." >&2
            ;;
        23|26|27|55|56)
            echo "error: download failed while writing '$stage_dir/$pbf_filename' (curl exit $curl_code) -- this usually means the disk ran out of space partway through. Free up space and re-run." >&2
            ;;
        28)
            echo "error: download timed out fetching '$URL'. Check connectivity and try again." >&2
            ;;
        *)
            echo "error: download failed (curl exit $curl_code) for '$URL'." >&2
            ;;
    esac
    exit 1
fi

# Reported disk-space diagnosis for a step failure below: if the volume's
# /data is nearly full, say so instead of just relaying the raw osrm-*
# tool's (often generic-looking) error output.
report_step_failure() {
    step_name="$1"
    step_code="$2"
    if [ "$step_code" -eq 137 ]; then
        echo "error: $step_name was killed (exit 137) -- almost always the Linux OOM killer. $step_name is the memory-hungry step of provisioning; retry on a host with more RAM (or add swap). See docs/osrm.md for rough RAM figures by extract size." >&2
        return
    fi
    df_output="$($compose_cmd --profile osrm run --rm --no-deps osrm df -Pk /data 2>/dev/null)" || df_output=""
    avail_kb="$(printf '%s\n' "$df_output" | awk 'NR==2 {print $4}')"
    if [ -n "$avail_kb" ] && [ "$avail_kb" -lt "$MIN_STAGE_FREE_KB" ] 2>/dev/null; then
        echo "error: $step_name failed (exit $step_code) and the osrmdata volume has only $((avail_kb / 1024)) MiB free -- this looks like insufficient disk space in the volume's backing storage. Free space there and re-run." >&2
        return
    fi
    echo "error: $step_name failed (exit $step_code); see the container output above for detail." >&2
}

echo "WARNING: this replaces the current contents of the 'osrmdata' volume. Any previously provisioned dataset is gone once this step completes." >&2
echo "Preparing the osrmdata volume ..."
set +e
$compose_cmd --profile osrm run --rm --no-deps -v "$stage_dir:/download:ro" osrm \
    sh -c "rm -rf /data/* && cp /download/$pbf_filename /data/$pbf_filename"
step_code=$?
set -e
if [ "$step_code" -ne 0 ]; then
    report_step_failure "volume preparation" "$step_code"
    exit 1
fi

echo "Running osrm-extract (the memory-hungry step) ..."
set +e
$compose_cmd --profile osrm run --rm --no-deps osrm \
    osrm-extract -p /opt/car.lua "/data/$pbf_filename"
step_code=$?
set -e
if [ "$step_code" -ne 0 ]; then
    report_step_failure "osrm-extract" "$step_code"
    exit 1
fi

echo "Running osrm-partition ..."
set +e
$compose_cmd --profile osrm run --rm --no-deps osrm \
    osrm-partition "/data/$dataset_value"
step_code=$?
set -e
if [ "$step_code" -ne 0 ]; then
    report_step_failure "osrm-partition" "$step_code"
    exit 1
fi

echo "Running osrm-customize ..."
set +e
$compose_cmd --profile osrm run --rm --no-deps osrm \
    osrm-customize "/data/$dataset_value"
step_code=$?
set -e
if [ "$step_code" -ne 0 ]; then
    report_step_failure "osrm-customize" "$step_code"
    exit 1
fi

cat <<EOF

Provisioning complete.

Set in .env, then start (or restart) the osrm service:
  OSRM_DATASET=$dataset_value
  OSRM_URL=http://osrm:5000

  $compose_cmd --profile osrm up -d osrm
EOF
