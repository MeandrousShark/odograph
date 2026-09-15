# syntax=docker/dockerfile:1

ARG POSTGIS_BASE=docker.io/nickblah/postgis:16-bookworm-postgis-3.6.4@sha256:de90689b3a56831db2b4d520a65b8acee01581d14060749a17b50026c8e37d70
ARG GOSU_BUILDER=docker.io/library/golang:1.27.1-bookworm@sha256:648f440f42a0958804efb24df176f806f9d353b41f1c0627f666428e40310f6b
ARG GOSU_REPOSITORY=https://github.com/tianon/gosu.git
ARG GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513

FROM ${GOSU_BUILDER} AS gosu-build

ARG GOSU_REPOSITORY
ARG GOSU_COMMIT

ENV CGO_ENABLED=0 \
    GOFLAGS=-mod=readonly
WORKDIR /src/gosu

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Fetch the exact upstream source commit. The checked-in go.sum then pins and
# verifies every module used by the build.
RUN git init . \
    && git remote add origin "$GOSU_REPOSITORY" \
    && git fetch --depth 1 origin "$GOSU_COMMIT" \
    && git checkout --detach "$GOSU_COMMIT" \
    && test "$(git rev-parse HEAD)" = "$GOSU_COMMIT"

RUN go mod download \
    && go mod verify

# Keep the symbol and build-info sections for vulnerability scanners. CGO is
# disabled so the resulting binary remains static on every target platform.
RUN go build -trimpath -buildvcs=false -o /out/gosu . \
    && /out/gosu --version

FROM ${POSTGIS_BASE}

ARG GIT_REVISION=unknown

COPY --from=gosu-build /out/gosu /usr/local/bin/gosu

RUN set -eux; \
    apt-get update; \
    apt-get upgrade -y --no-install-recommends; \
    rm -rf /var/lib/apt/lists/*; \
    gosu --version; \
    gosu nobody true

LABEL org.opencontainers.image.source="https://github.com/MeandrousShark/odograph" \
      org.opencontainers.image.revision="$GIT_REVISION"
