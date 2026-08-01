# Pinned to the OCI image index, not an architecture-specific child
# manifest, so the same immutable reference resolves correctly for both
# linux/amd64 and linux/arm64. requirements.lock was verified against this
# exact base on both target architectures.
# Digest confirmed 2026-07-21 from Docker Hub for python:3.13-slim.
# Re-resolve and verify both target platforms with:
#   podman pull docker.io/library/python:3.13-slim
#   podman image inspect --format '{{index .RepoDigests 0}}' docker.io/library/python:3.13-slim
#   podman manifest inspect docker.io/library/python@sha256:<resolved-digest>
FROM docker.io/library/python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

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
RUN pip install --no-cache-dir -r requirements.lock

COPY app/ app/
COPY migrations/ migrations/
COPY static/ static/

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
