# Pinned to the OCI image index, not an architecture-specific child
# manifest, so the same immutable reference resolves correctly for both
# linux/amd64 and linux/arm64. requirements.lock was verified against this
# exact base on both target architectures.
# Digest confirmed 2026-09-12 from Docker Hub for python:3.13-slim.
# A digest pin is reproducible but frozen, so it stops receiving the base
# distribution's rebuilt packages and eventually fails the release image scan
# on findings that are fixed upstream. Re-resolve it as part of preparing a
# release rather than waiting for the scan to block, and re-verify both target
# platforms with:
#   skopeo inspect --raw docker://docker.io/library/python:3.13-slim | sha256sum
#   skopeo inspect --raw docker://docker.io/library/python@sha256:<resolved-digest>
# Resolve it with skopeo rather than `podman pull` plus RepoDigests: that pair
# reports the child manifest for the host's own architecture, so pinning what
# it prints silently produces a single-architecture base. The correct value has
# mediaType application/vnd.oci.image.index.v1+json and lists both linux/amd64
# and linux/arm64.
FROM docker.io/library/python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

# The pinned base predates available Debian fixes in these four packages.
# Upgrade only the affected packages; remove this layer when the base includes
# their fixes. The refreshed base already includes the previous OpenSSL fix.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade \
        gzip libpcre2-8-0 libsqlite3-0 perl-base \
    && rm -rf /var/lib/apt/lists/*

# VERSION/GIT_REVISION default to dev values so unlabeled local builds still
# work; the release build supplies both explicitly. --platform linux/amd64 is
# required regardless of the build host's own architecture: the production
# host is x86_64, and without this flag `podman build` silently targets
# whatever architecture the build host itself is -- this is what produced an
# arm64 image during a past release when built from an Apple Silicon Mac.
#   podman build --platform linux/amd64 --build-arg VERSION=v0.1.0 \
#     --build-arg GIT_REVISION=$(git rev-parse HEAD) \
#     -t your-registry.example.com/odograph:v0.1.0 .
ARG VERSION=dev
ARG GIT_REVISION=unknown
ENV APP_VERSION=$VERSION \
    APP_GIT_REVISION=$GIT_REVISION
LABEL org.opencontainers.image.source="https://github.com/MeandrousShark/odograph" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$GIT_REVISION

WORKDIR /srv/odograph
# requirements.lock (not requirements.txt) so the image installs the exact
# versions pinned against this base image; requirements.txt stays as the
# human-edited source for the dev venv workflow and for regenerating the lock.
COPY requirements.txt requirements.lock ./
# pip is removed once the dependencies are installed. Nothing at runtime uses
# it: the image starts uvicorn as an unprivileged user and never resolves or
# installs a package. Removing it also removes pip's vendored copies of
# msgpack and setuptools, which the base image's own pip declares in a
# CycloneDX SBOM and which an image scanner therefore reports as fixable
# findings against this image even though no code path can reach them. Deleting
# the vendored code is the honest fix; suppressing the report is not. An
# operator who needs a package inside a running container should rebuild the
# image rather than mutate a running one.
RUN pip install --no-cache-dir -r requirements.lock \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip

COPY app/ app/
COPY migrations/ migrations/
COPY static/ static/
COPY scripts/sql/account_bootstrap.sql scripts/sql/account_admission.sql scripts/sql/tracking_admission.sql scripts/sql/

# Fixed numeric UID/GID, not a named user: rootless Podman and rootful Docker
# both map a numeric identity the same way, while a name would need an
# /etc/passwd entry the image may or may not carry consistently. The app
# writes nothing to disk at runtime, so no directory needs a chown -- root's
# default umask already leaves the copied sources and installed packages
# world-readable.
RUN groupadd --gid 10001 odograph \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin odograph
USER 10001:10001

EXPOSE 8000
# --proxy-headers so OIDC redirect URIs use the external https:// scheme
# set by Caddy's X-Forwarded-* headers.
CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers"]
