#!/usr/bin/env bash
# Pull the app image named by compose.yaml and verify its cosign signature
# before it is started. Run from the installation directory (the one that
# holds compose.yaml), then run `docker compose up -d`; compose.yaml sets
# `pull_policy: never` for the app, so `up` uses only the image checked here.
#
# Usage:
#   scripts/verify_image.sh [--version vX.Y.Z]
#
# The app image in compose.yaml must be an exact release tag (vX.Y.Z) or a
# sha256 digest of ghcr.io/meandrousshark/odograph. The signature must come
# from this project's release workflow for that exact tag. A digest-pinned
# image has no tag to read, so --version names the release tag it belongs to.
#
# Env vars:
#   CONTAINER_RUNTIME   override engine autodetection (docker, else podman)
set -euo pipefail

IMAGE_REPO="ghcr.io/meandrousshark/odograph"
IDENTITY_REPO="MeandrousShark/odograph"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
COMPOSE_FILE="compose.yaml"

# Release tags are vX.Y.Z with an optional prerelease suffix. Floating
# aliases such as v1, v0.11 and latest do not match.
TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'
DIGEST_RE='^sha256:[0-9a-f]{64}$'

usage() {
    cat >&2 <<'USAGE'
usage: scripts/verify_image.sh [--version vX.Y.Z]

Run from the installation directory. Reads the app image from compose.yaml,
pulls it, and verifies its cosign signature against the project's release
workflow identity. Prints the verified digest on success and exits non-zero
on any failure. Requires cosign and docker (or podman).

  --version vX.Y.Z   release tag the image belongs to; required only when
                     compose.yaml pins the app image by digest

Set CONTAINER_RUNTIME to choose the engine; otherwise docker is used when
present, else podman.
USAGE
    exit "${1:-2}"
}

die() {
    echo "error: $*" >&2
    exit 1
}

VERSION=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || { echo "error: --version requires a value" >&2; usage; }
            VERSION="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "error: unrecognized argument: $1" >&2; usage ;;
    esac
done

app_image() {
    # Mirrors scripts/check_release_contract.py: the first "image:" line of
    # the "app:" service, two levels of indentation in.
    awk '
        /^  [A-Za-z0-9_-]+:$/ { in_app = ($0 == "  app:"); next }
        in_app && /^    image:[[:space:]]*[^[:space:]]+[[:space:]]*$/ {
            sub(/^    image:[[:space:]]*/, "")
            sub(/[[:space:]]+$/, "")
            print
            exit
        }
    ' "$COMPOSE_FILE"
}

detect_engine() {
    if [ -n "${CONTAINER_RUNTIME:-}" ]; then
        echo "$CONTAINER_RUNTIME"
    elif command -v docker >/dev/null 2>&1; then
        echo docker
    elif command -v podman >/dev/null 2>&1; then
        echo podman
    else
        die "neither docker nor podman found on PATH; install one or set CONTAINER_RUNTIME."
    fi
}

command -v cosign >/dev/null 2>&1 \
    || die "cosign not found on PATH; install it from https://docs.sigstore.dev/cosign/system_config/installation/ and re-run."
engine="$(detect_engine)"
command -v "$engine" >/dev/null 2>&1 || die "container engine '$engine' not found on PATH."

[ -f "$COMPOSE_FILE" ] || die "$COMPOSE_FILE not found; run this from the installation directory."
ref="$(app_image)"
[ -n "$ref" ] || die "could not find the app image in $COMPOSE_FILE."

# Split the reference into repository and either a tag or a digest.
tag=""
pinned_digest=""
case "$ref" in
    *@*)
        name="${ref%%@*}"
        pinned_digest="${ref#*@}"
        case "${name##*/}" in
            *:*) die "app image '$ref' has both a tag and a digest; use either a tag or a digest." ;;
        esac
        [[ "$pinned_digest" =~ $DIGEST_RE ]] \
            || die "app image '$ref' has an invalid digest; expected sha256: followed by 64 hex digits."
        ;;
    *)
        name="${ref%:*}"
        tag="${ref##*:}"
        [[ "$tag" =~ $TAG_RE ]] \
            || die "app image '$ref' must be an exact release tag such as vX.Y.Z or a sha256 digest, not a floating alias."
        ;;
esac
[ "$name" = "$IMAGE_REPO" ] \
    || die "app image repository '$name' is not $IMAGE_REPO; this script verifies only that image, signed by $IDENTITY_REPO."

if [ -n "$tag" ]; then
    if [ -n "$VERSION" ] && [ "$VERSION" != "$tag" ]; then
        die "--version $VERSION does not match the compose.yaml tag $tag."
    fi
    VERSION="$tag"
fi
[ -n "$VERSION" ] || die "compose.yaml pins the app image by digest; pass --version vX.Y.Z naming its release tag."
[[ "$VERSION" =~ $TAG_RE ]] || die "--version '$VERSION' is not an exact release tag such as vX.Y.Z."

identity="https://github.com/${IDENTITY_REPO}/.github/workflows/release.yml@refs/tags/${VERSION}"

echo "Pulling $ref with $engine"
"$engine" pull "$ref" || die "could not pull $ref."

inspect_out="$("$engine" image inspect \
    --format '{{range .RepoDigests}}{{.}}{{"\n"}}{{end}}' "$ref")" \
    || die "could not inspect the pulled image $ref."

# An index pulled by tag can list more than one digest (the index and a
# platform manifest); each is signed, so each one found is verified.
digests=()
while IFS= read -r line; do
    case "$line" in
        "$IMAGE_REPO"@sha256:*)
            d="${line#*@}"
            [[ "$d" =~ $DIGEST_RE ]] || continue
            if [ -n "$pinned_digest" ] && [ "$d" != "$pinned_digest" ]; then
                continue
            fi
            digests+=("$d")
            ;;
    esac
done <<< "$inspect_out"
[ "${#digests[@]}" -gt 0 ] || die "the pulled image has no $IMAGE_REPO repository digest; it cannot be verified."

for d in "${digests[@]}"; do
    if ! cosign verify \
        --certificate-identity "$identity" \
        --certificate-oidc-issuer "$OIDC_ISSUER" \
        "${IMAGE_REPO}@${d}" >/dev/null; then
        die "signature verification FAILED for ${IMAGE_REPO}@${d}; do not start this image."
    fi
done

for d in "${digests[@]}"; do
    echo "verified: ${IMAGE_REPO}@${d}"
done
echo "signer: $identity"
