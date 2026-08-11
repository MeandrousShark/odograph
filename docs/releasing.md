# Releasing

This is the maintainer procedure for publishing a versioned release and its
multi-architecture container image. Releases are built by CI from an immutable
annotated tag. Never publish from a workstation when CI is available, never
publish `latest`, and never move or reuse a release tag.

## Preconditions

Before preparing a release:

- Confirm the target version follows semantic versioning and has not been used
  as a git tag, GitHub release, or container tag.
- Confirm `main` is the intended release tree and its required checks are green.
- Run the dependency scan yourself against the exact tree you intend to tag,
  and resolve it to a clean result before going further:

  ```sh
  pip-audit -r requirements.lock --no-deps --disable-pip
  ```

  Do this even when the scheduled scan was green. That scan runs weekly, so
  its result can be up to seven days stale, and an advisory published in
  between first surfaces inside the release workflow, where the same check
  blocks. By then the tag exists and is immutable, so a finding there costs
  the version number rather than a few minutes: the release has to be
  abandoned and recut as the next patch. Resolve findings by upgrading the
  affected dependency where possible, or record a narrowly scoped acceptance
  as described below.

  Changing a pin has its own precondition. The image installs
  `requirements.lock` on the `python:3.13-slim` base for both published
  architectures, so a new version without prebuilt wheels for either one turns
  the build into a source build. Confirm both before committing the change:

  ```sh
  for PLAT in manylinux_2_28_x86_64 manylinux_2_28_aarch64; do
    pip download --only-binary=:all: --platform "$PLAT" --python-version 3.13 \
      --no-deps -d /tmp/wheelcheck "<package>==<version>"
  done
  ```

- Review the scheduled image-scan results. Resolve blocking findings or record
  a narrowly scoped acceptance as described below.
- Read every release note since the previous supported release. Decide the
  supported upgrade path, identify breaking operational or data changes, and
  determine whether the standard artifact upgrade drill applies.
- Have `git`, `gh`, Docker with Buildx, `jq`, and `pip-audit` available, the
  last of these because the dependency scan above is required rather than
  optional. Install Trivy when reproducing the image scans locally.
  Authenticate `gh` and Docker to GitHub/GHCR with the repository and package
  permissions needed for the release.

Set the release values without a `latest` alias:

```sh
VERSION=vX.Y.Z
IMAGE=ghcr.io/meandrousshark/odograph
```

Prerelease versions use a semantic prerelease suffix, such as
`vX.Y.Z-rc.1`. The changelog heading omits the leading `v`.

## Prepare the release commit

Start from a clean, current `main`:

```sh
git fetch origin --tags
git switch main
git pull --ff-only origin main
git status --short
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
```

Stop if `git status --short` prints anything or the revision check fails.

Update `compose.yaml` so `services.app.image` is exactly
`$IMAGE:$VERSION`. Move the release's entries out of `Unreleased` into one
exact, nonempty changelog section:

```text
## [X.Y.Z] - YYYY-MM-DD
```

Every release section must state:

- the supported upgrade path, including which prior release or releases may be
  upgraded directly;
- all breaking application, database, configuration, and operational changes,
  or an explicit statement that there are none; and
- any security scan acceptance in the exact syntax described below.

For the first public release, use the explicit statement "Prior-release
artifact upgrade gate: not applicable. No prior public release exists." Do
not imply that the gate ran.

Validate that the Compose image, changelog section, documented exact-tag
install path, and runtime-reported version contract all agree. Then confirm
the release-note extractor selects the intended section and review and commit
the Compose pin and changelog together:

```sh
RELEASE_TMP=$(mktemp -d)
python3 scripts/check_release_contract.py --tag "$VERSION" --image "$IMAGE"
python3 scripts/release_notes.py --tag "$VERSION" \
  --notes-output "$RELEASE_TMP/release-notes.md" \
  --pip-audit-output "$RELEASE_TMP/pip-audit.ignore" \
  --trivy-amd64-output "$RELEASE_TMP/trivy-amd64.ignore" \
  --trivy-arm64-output "$RELEASE_TMP/trivy-arm64.ignore"
sed -n '1,240p' "$RELEASE_TMP/release-notes.md"
git diff --check
git diff -- compose.yaml CHANGELOG.md
git add compose.yaml CHANGELOG.md
git diff --cached --check
git diff --cached
git commit -m "Prepare $VERSION release"
GIT_REVISION=$(git rev-parse HEAD)
```

The resulting commit is the only commit that may receive this release tag.
Push `main`, wait for its required checks to pass, and verify the remote still
identifies the reviewed commit:

```sh
git push origin main
test "$(git rev-parse origin/main)" = "$GIT_REVISION"
```

## Supply-chain gates: scanning, SBOM, and signing

Every release passes a blocking vulnerability scan of its dependencies and
images, and every published artifact carries a software bill of materials and
a cosign signature. Together these are what let an operator trust a pulled
image without trusting the registry transport or the maintainer's word alone.

### Blocking dependency and image scans

`pip-audit` blocks every reported vulnerability in the locked Python
dependencies. Trivy blocks fixable HIGH and CRITICAL findings in each container
architecture. Prefer upgrading or removing the affected dependency. An
acceptance is exceptional: review reachability and exposure, constrain it to
the affected scanner and architecture, and record why shipping is safe enough
until a fix is available.

Acceptances belong only in the exact release tag's changelog section. The
reason after the separator must be nonempty and specific. The separator may be
either a colon and a space, shown below and preferred for new entries, or a
spaced em dash, which older entries use and which is still accepted. These are
the accepted forms:

```text
- **Security scan acceptance (pip-audit):** `GHSA-xxxx-xxxx-xxxx`: reason
- **Security scan acceptance (Trivy linux/amd64):** `CVE-YYYY-NNNN`: reason
- **Security scan acceptance (Trivy linux/arm64):** `CVE-YYYY-NNNN`: reason
- **Security scan acceptance (Trivy all):** `CVE-YYYY-NNNN`: reason
```

Use `all` only when the same finding and rationale apply to both published
architectures. A marker in `Unreleased`, another version's section, or with an
empty reason has no effect. Malformed markers fail the release rather than
silently weakening a gate. The release-note extractor turns valid markers into
the scanner-specific ignore files used only for that release run.

Maintainers can reproduce the dependency check locally with `pip-audit` and
scan an already-built image with Trivy:

```sh
pip-audit -r requirements.lock --no-deps --disable-pip
trivy image --ignore-unfixed --severity HIGH,CRITICAL "$IMAGE:$VERSION"
```

### SBOM and signing

After the manifest list is published, the release workflow generates an SPDX
SBOM for each architecture (`syft`, run through `anchore/sbom-action` against
the already-pushed digest), then uses `cosign` to:

- attest each architecture's SBOM to its own architecture digest, and
- sign the manifest list and each architecture digest individually.

Signing each architecture digest on its own matters because `cosign` does not
recurse into a manifest list's children by default: an operator who pulls a
single architecture by digest, rather than the multi-architecture tag, still
gets a signature to check.

Signing is keyless: the job exchanges its GitHub Actions OIDC token for a
short-lived Fulcio certificate scoped to this exact workflow run, so there is
no long-lived private key for a maintainer to generate, store, or rotate, and
no key file that can leak. Verification instead checks the certificate's
recorded identity (which workflow, in which repository, on which ref) against
the public Rekor transparency log, both without a key.

### Verifying a release's signature and SBOM

`cosign` (`brew install cosign` or see the
[Sigstore install docs](https://docs.sigstore.dev/cosign/system_config/installation/))
verifies against the workflow's OIDC issuer and its own identity string. The
identity is repository-scoped and tag-scoped, so resolve both from the
repository actually hosting the verified release rather than hardcoding either:

```sh
REPOSITORY="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
IDENTITY="https://github.com/$REPOSITORY/.github/workflows/release.yml@refs/tags/$VERSION"
ISSUER="https://token.actions.githubusercontent.com"

cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE:$VERSION"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$AMD64_DIGEST"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$ARM64_DIGEST"
```

Each architecture digest also carries an attested SBOM. One command per
architecture verifies that attestation and writes out the SBOM it covers. What
the attestation signs is an in-toto statement whose subject is the architecture
digest; the SPDX document is that statement's `predicate`, so extract the
predicate rather than the whole statement:

```sh
cosign verify-attestation --type spdxjson \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$AMD64_DIGEST" | jq -r '.payload' | base64 -d | jq '.predicate' > sbom-amd64.spdx.json

cosign verify-attestation --type spdxjson \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$ARM64_DIGEST" | jq -r '.payload' | base64 -d | jq '.predicate' > sbom-arm64.spdx.json
```

Each written file is then a standalone SPDX document rather than the statement
wrapping it. Confirm that before relying on it; an empty package list means the
extraction, not the release, went wrong:

```sh
jq -r '.spdxVersion, (.packages | length)' sbom-amd64.spdx.json
```

A failed verification means the artifact was not produced by this exact
workflow run for this exact repository and tag. Treat it the same as a failed
digest comparison: stop and investigate before deploying.

## Create the immutable tag

Create an annotated tag on the exact reviewed release commit and push only
that tag:

```sh
test "$(git rev-parse HEAD)" = "$GIT_REVISION"
test -z "$(git status --short)"
git tag -a "$VERSION" "$GIT_REVISION" -m "Odograph $VERSION"
git show --no-patch --decorate "$VERSION"
git push origin "refs/tags/$VERSION"
```

From this point the tag is immutable. Do not force-push it, delete and recreate
it, overwrite its container tag, or rerun a partially published version under
the same name. A correction uses a new patch version; a failed prerelease uses
a new prerelease number.

## Observe the CI release

The tag starts the release workflow. Do not publish artifacts manually while
it runs. Confirm, in order, that CI:

1. checks out the exact tagged revision, runs shell syntax checks and the full
   test suite with PostGIS, and runs `pip-audit` against `requirements.lock`;
2. builds `linux/amd64` and `linux/arm64` images with `VERSION` and
   `GIT_REVISION` set to the tag and tagged commit;
3. runs blocking Trivy HIGH/CRITICAL scans against each architecture digest;
4. verifies both child manifests, creates the versioned manifest list, and
   reports the manifest, amd64, and arm64 digests;
5. generates and attests an SBOM for each architecture digest, then signs the
   manifest list and each architecture digest with keyless cosign; and
6. creates the GitHub release from the exact changelog section.

Record all three reported digests in the release record. If any job fails,
leave any published object untouched, investigate the failure, and prepare a
new version. Never repair a release by moving its tag.

## Verify the published artifacts

Also verify the cosign signature and SBOM attestation on each digest, as
described in
[Verifying a release's signature and SBOM](#verifying-a-releases-signature-and-sbom)
above, before relying on any digest recorded below.

Inspect the exact tag and compare its manifest and child digests with the CI
summary:

```sh
docker buildx imagetools inspect "$IMAGE:$VERSION"
docker buildx imagetools inspect "$IMAGE:$VERSION" --format '{{json .Manifest}}' | jq .
```

On a real amd64 host, pull the recorded amd64 digest and verify its platform.
Repeat on a native arm64 host with the recorded arm64 digest:

```sh
docker pull "$IMAGE@$AMD64_DIGEST"
docker image inspect "$IMAGE@$AMD64_DIGEST" --format '{{.Os}}/{{.Architecture}}'

docker pull "$IMAGE@$ARM64_DIGEST"
docker image inspect "$IMAGE@$ARM64_DIGEST" --format '{{.Os}}/{{.Architecture}}'
```

Each result must match the host and expected architecture. On each host, check
out the exact release tag, follow the README's pinned-image install, wait for
the database and app to become healthy, and sign in. Confirm Settings reports
the exact version and tagged git revision, plus the expected schema and
detector versions. Confirm `/healthz` exposes none of those identifiers.

## Run the artifact upgrade and rollback drill

For every release after the first public release, run the disposable drill
from the previous supported release tree to the exact candidate tree and
published image. `--base` and `--candidate` are both required even when the
candidate application comes from an image:

```sh
COMPOSE_CMD="docker compose" scripts/upgrade_check.sh \
  --base vPREVIOUS \
  --candidate "$VERSION" \
  --candidate-image "$IMAGE:$VERSION"

COMPOSE_CMD=podman-compose scripts/upgrade_check.sh \
  --base vPREVIOUS \
  --candidate "$VERSION" \
  --candidate-image "$IMAGE@$MANIFEST_DIGEST"
```

`--candidate-image` must be an exact non-`latest` tag or full digest. The drill
creates a fresh backup, verifies it, restores into a fresh volume, upgrades,
checks the candidate schema and data, then rolls back to the base release and
pre-upgrade data. Run it with both supported Compose frontends. Base and
candidate app definitions may differ, but their rendered database service must
remain identical; a database-service operational change needs a release-specific
drill documented in that release's notes.

For the first public release, record "Prior-release artifact upgrade gate: not
applicable. No prior public release exists." A clean-install verification on
both architectures is still required.

## Move the floating minor tag

Alongside every immutable `vX.Y.Z` tag, the registry carries a floating `vX.Y`
tag that points at the newest patch within that minor version. Operators who
would otherwise reach for `latest` pin `vX.Y` instead: they pick up patch fixes
without silently crossing a minor boundary, which is where migrations and, before
1.0, breaking changes are allowed to land. There is deliberately no `latest` tag
and no floating major tag.

**This step is mandatory for every patch release.** A floating tag that stops
moving is worse than no floating tag, because operators believe it is current
while it quietly pins them to an old patch. A release is not complete until
`vX.Y` resolves to the release you just published.

Move it only after the published artifacts verify and the upgrade drill passes.
The floating tag must never point at an unverified, yanked, or partially
published release.

Retag by digest so the tag is an alias for the exact index that was already
signed. Do not rebuild or re-push the manifest: a rebuilt index gets a new
digest, and the existing cosign signature would no longer cover the floating
tag.

```sh
crane tag "$IMAGE:$VERSION" "${VERSION%.*}"
```

This needs a token with `write:packages`; the workflow's own token is not
available here. `gh auth refresh -h github.com -s write:packages` followed by
`gh auth token | crane auth login ghcr.io -u <user> --password-stdin` is
sufficient. Then confirm the floating tag is the same artifact and still
verifies:

```sh
crane digest "$IMAGE:$VERSION"
crane digest "$IMAGE:${VERSION%.*}"
cosign verify \
  --certificate-identity-regexp "^https://github.com/MeandrousShark/odograph/.github/workflows/.*@refs/tags/$VERSION$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE:${VERSION%.*}"
```

Both digests must be identical and the signature must verify through the
floating tag. The signing certificate identity still names the immutable
version, which is correct: it records which release the artifact came from.

A prerelease never moves a floating tag.

## Complete the release

Review the GitHub release as a new operator would. Confirm its upgrade path and
breaking-change statement are visible, its source tag and image tag match, and
the canonical Compose file pins that same version. Keep the pre-upgrade backup
used for any real deployment until the documented observation period ends.

## Break-glass manual multi-architecture publication

The commands in this section bypass the CI-controlled build, test, scan,
SBOM, signing, immutability, and release-note gates. Artifacts produced this
way carry no SBOM and no cosign signature. They are publication-dangerous and
are only for an isolated private registry during registry or workflow
recovery. Do not use them for a public release, production, or an existing
tag. Never substitute `latest`.

Set `VERSION`, `GIT_REVISION`, and `IMAGE` to a new private-registry tag and the
exact source commit. With Docker Buildx:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --build-arg VERSION="$VERSION" --build-arg GIT_REVISION="$GIT_REVISION" --tag "$IMAGE:$VERSION" --push .
docker buildx imagetools inspect "$IMAGE:$VERSION"
```

With Podman, create the manifest locally, add each platform deliberately,
inspect it before publication, and push all children:

```sh
podman manifest create "$IMAGE:$VERSION"
podman build --platform linux/amd64 --build-arg VERSION="$VERSION" --build-arg GIT_REVISION="$GIT_REVISION" --manifest "$IMAGE:$VERSION" .
podman build --platform linux/arm64 --build-arg VERSION="$VERSION" --build-arg GIT_REVISION="$GIT_REVISION" --manifest "$IMAGE:$VERSION" .
podman manifest inspect "$IMAGE:$VERSION"
podman manifest push --all "$IMAGE:$VERSION" "docker://$IMAGE:$VERSION"
```

After a private-registry recovery exercise, run the same per-architecture
digest verification described above. These commands do not make the result an
approved release.
