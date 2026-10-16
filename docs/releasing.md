# Releasing

This is the maintainer procedure for publishing a versioned release and its
multi-architecture container image. For installing or updating your own
instance, use the [README](../README.md#install) and
[upgrading guide](upgrading.md).

Releases are built by CI from an immutable
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
  affected dependency; scan acceptances block release preflight.

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

- Review the scheduled image-scan results. Resolve blocking findings by
  updating the affected dependency.
- Read every release note since the previous supported release. Decide the
  supported upgrade path, identify breaking operational or data changes, and
  determine whether the standard artifact upgrade drill applies.
- Have `git`, `gh`, Docker with Buildx, Podman with `podman-compose`,
  `cosign`, `crane`, `jq`, and `pip-audit` available. `pip-audit` is required
  because the dependency scan above is required rather than optional; `cosign`
  and `crane` are used to verify signatures and move floating tags, and Podman
  runs the second upgrade drill. Create the `.venv` described in
  [AGENTS.md](../AGENTS.md#test-setup) first: the commands below call
  `.venv/bin/python`. Install Trivy when reproducing the image scans locally.
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

Start from a clean, current `main`, then create a release branch:

```sh
git fetch origin --tags
git switch main
git pull --ff-only origin main
git status --short
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
git switch -c release/$VERSION
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
  or an explicit statement that there are none;
- each new migration in the release with its `-- migration-class:` header
  value, or an explicit statement that the release adds none; and
- no security scan acceptances (see
  [Blocking dependency and image scans](#blocking-dependency-and-image-scans)).

Migrations from 045 on declare `additive`, `rewrite` or `destructive` on their
first line, and a test enforces it. List the class of every migration the
release adds. For each `rewrite` or `destructive` migration, also state the
operator impact, such as a longer first startup, a backfill, or data that is
removed. Destructive changes to supported existing data belong at a major
version boundary.

State which prior-release artifact upgrade gate ran, or why it did not apply.
Do not imply that a gate ran when it did not.

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

Push the release branch and open a pull request. The resulting merge commit on
`main` is the only commit that may receive this release tag. Wait for all
required checks and review to pass, merge the pull request, then fetch the
exact reviewed `main` commit and run the full suite against it. The exact
merge commit's release preflight and all other vulnerability and CI checks
must pass before creating the immutable tag:

```sh
set -e
git push --set-upstream origin release/$VERSION
# Open the pull request, record its number in RELEASE_PR, and merge it only
# after review and all required checks pass.
RELEASE_PR=123
MERGE_SHA=$(gh pr view "$RELEASE_PR" --json mergeCommit --jq '.mergeCommit.oid')
test -n "$MERGE_SHA" -a "$MERGE_SHA" != "null"
git fetch origin main --tags
git switch main
git pull --ff-only origin main
test "$(git rev-parse HEAD)" = "$MERGE_SHA"
test "$(git rev-parse origin/main)" = "$MERGE_SHA"
GIT_REVISION="$MERGE_SHA"
TASK_ID="release-${VERSION#v}"
DB_EXPORT=$(scripts/test_db.sh start "$TASK_ID")
eval "$DB_EXPORT"
: "${TEST_DATABASE_URL:?disposable database URL required}"
trap 'scripts/test_db.sh cleanup "$TASK_ID"' EXIT
.venv/bin/python -m pytest
scripts/test_db.sh cleanup "$TASK_ID"
trap - EXIT
```

Do not tag a release branch or a pre-merge commit. Do not force-update or
reuse an existing release tag.

## Run the nonpublishing release preflight

The `Release preflight` workflow checks release inputs and the exact checked-out
tree before publication. It runs automatically for every pull request and for
pushes to `release/**` and `main`; its automatic triggers have no path filter.
Release branch pushes always run the preflight. Ordinary pull requests compare
the Compose app image version with their base, and a `main` push compares it
with the previous revision; an unchanged version is recorded as skipped. For a
release, wait for the pull request run, the release branch run, and the run on
the exact merge commit on `main`; the last one must pass before tagging.

The workflow has `contents: read` permission only. It does not push images,
create releases, create or push tags, or move the floating minor alias. Its
preflight builds local image archives and uploads evidence only. It always runs
with `--require-empty-acceptances`, so any changelog scan acceptance fails it
and this path does not weaken `pip-audit` or Trivy gates.

To dispatch a preflight manually, choose the exact candidate branch or tag. The
version input is optional and otherwise comes from the checked
out Compose app image; when supplied, it must match that release contract:

```sh
VERSION=vX.Y.Z
REF=release/$VERSION
gh workflow run release-preflight.yml --ref "$REF" \
  --field mode=preflight

# Optional explicit version override for the same exact ref:
gh workflow run release-preflight.yml --ref "$REF" \
  --field mode=preflight --field version="$VERSION"
```

Preflight mode validates that the version is unused in GitHub and GHCR, runs
the locked full test suite with PostGIS, shell syntax checks, public-tree and
secret checks, and the dependency audit. It builds and scans native
`linux/amd64` and `linux/arm64` application images, writes an SPDX SBOM for
each, and runs the HTTPS authentication smoke: health, protected settings,
administrator signup, Secure session cookie, and authenticated version and
revision checks. The application image, application smoke, and PostGIS
database remain native on each runner. The preflight also runs the source
upgrade and rollback drill with Docker Compose, using the database-image
migration mode because the rendered database image is part of the candidate
release contract.

The drill lives in the reusable `Upgrade drill` workflow
(`.github/workflows/upgrade-drill.yml`). It reads its base releases from
`scripts/upgrade_drill_bases.json` and drills from each one as a separate
matrix job, with the exact revision as `--candidate` and
`--database-image-migration`. Besides both preflight modes, it runs on pull
requests that change migrations, the drill script, the bases file, the backup
or restore scripts, or the workflow itself. To drill any branch or tag on
demand:

```sh
gh workflow run upgrade-drill.yml --ref "$REF"
```

When the supported base releases change, update the bases file.

The v1.0.0 release itself drills from the supported 0.x bases in the bases
file. From the first release after v1.0.0, the bases are v1.0.0 and the
previous release, and the 0.x bases are removed. At each release prep, update
`scripts/upgrade_drill_bases.json` and its exact-list test in
`tests/test_release_preflight.py` to match.

After an immutable release has been published, dispatch the verification mode
from a ref that resolves to the published tag's exact commit. The tag ref is
the recommended target. The helper compares the checked-out revision with the
published tag's commit, so a branch or `main` also passes only when it points to
that same commit:

```sh
gh workflow run release-preflight.yml --ref "$VERSION" \
  --field mode=verify-published --field version="$VERSION"
```

This mode requires the tag, GitHub release, and GHCR manifest to exist, then
resolves the OCI index and both child digests. It verifies the cosign signature
on the index and each child, verifies each child SPDX attestation, and checks
that every attested in-toto subject names the child digest and contains a
nonempty SPDX package list. Native AMD64 and ARM64 jobs pull their child by
digest, check its platform and release identity, and run the same HTTPS smoke.
The published upgrade and rollback drill runs the same base matrix with Docker
Compose, the exact checked-out revision as `--candidate`, the exact OCI index
digest as `--candidate-image`, and `--database-image-migration`.

The automated preflight and published verification use Docker Compose. Run the
equivalent Podman Compose drill in the artifact upgrade section below before
completing the release; it remains a required release check.

## Supply-chain gates: scanning, SBOM, and signing

Every release passes a blocking vulnerability scan of its dependencies and
images, and every published artifact carries a software bill of materials and
a cosign signature. Together these are what let an operator trust a pulled
image without trusting the registry transport or the maintainer's word alone.

### Blocking dependency and image scans

`pip-audit` blocks every reported vulnerability in the locked Python
dependencies. Trivy blocks fixable HIGH and CRITICAL findings in each container
architecture. Upgrade or remove the affected dependency.

Release preflight always runs with `--require-empty-acceptances`
(`.github/workflows/release-preflight.yml`), so any scan acceptance in the
release's changelog section fails the preflight and the publication
verification, blocking tagging and verification. Do not use acceptances:
resolve a finding by updating or removing the affected dependency.

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

Operators verify the app image with `scripts/verify_image.sh`, which pulls the
tag named in `compose.yaml` and runs `cosign verify` with the exact identity
below on the pulled digest. The commands in this section are the manual
equivalent, and also cover the per-architecture digests and SBOMs.

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
  "$IMAGE@$AMD64_DIGEST" \
  | jq -rs '[.[] | if type == "array" then .[] else . end][0].payload' \
  | base64 -d | jq '.predicate' > sbom-amd64.spdx.json

cosign verify-attestation --type spdxjson \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$ARM64_DIGEST" \
  | jq -rs '[.[] | if type == "array" then .[] else . end][0].payload' \
  | base64 -d | jq '.predicate' > sbom-arm64.spdx.json
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
3. runs blocking Trivy HIGH/CRITICAL scans against each application and native
   PostGIS architecture digest;
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

The default PostGIS image is the signed native multi-architecture index
`ghcr.io/meandrousshark/odograph-postgis@sha256:c4c1184f0c4d899f65c3df9ea6d296c3a3a5f1b233090651ac49429b4e2100b4`.
Inspect its index and child digests, verify the index and both child signatures,
and verify each child SPDX subject before pinning a replacement in repository
references. This index was published by the trusted `main` workflow at source
commit `17048177dfea29e07be81bbb719e0d1e79fe4017`.

## Run the artifact upgrade and rollback drill

Every release is drilled from each base tag in
`scripts/upgrade_drill_bases.json` to the exact candidate. Release preflight
runs this drill with Docker Compose for every base, through
`.github/workflows/upgrade-drill.yml`: against the candidate source in
`preflight` mode and against the published image in `verify-published` mode.
For v1.0.0 the bases are v0.10.2, v0.11.0, v0.11.1 and v0.11.3; every
published v0.10.x and v0.11.x release is schema 25, so v0.10.0 and v0.11.2
share these upgrade paths. Later 1.x releases drill from v1.0.0 and the
previous release. In addition, run the drill manually with Podman Compose from
the newest base. For v1.0.0 that is v0.11.3, and because the database image
also changes, use the [database image migration
drill](#database-image-migration-drill) mode. `--base` and `--candidate` are
both required even when the candidate application comes from an image:

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

`--candidate-image` must be an exact release tag (`vX.Y.Z`) or full digest, not a floating alias. The drill
creates a fresh backup, verifies it, restores into a fresh volume, upgrades,
checks the candidate schema and data, then rolls back to the base release and
pre-upgrade data. Run it with both supported Compose frontends. Base and
candidate app definitions may differ, but their rendered database service must
remain identical; a database-service operational change needs a release-specific
drill documented in that release's notes.

Add `--base-image "$IMAGE@sha256:..."` with the previous release's verified
digest to use its published application for both the starting installation and
backup-based rollback. The base ref still supplies that release's Compose and
scripts. Without this option the drill rebuilds the base source.

### Database image migration drill

When a candidate changes only the rendered database image, use the explicit
database-image migration mode. The default stable-database guard remains in
force; this mode rejects any database-service change other than the image:

```sh
COMPOSE_CMD="docker compose" scripts/upgrade_check.sh \
  --base vPREVIOUS \
  --candidate "$VERSION" \
  --database-image-migration

COMPOSE_CMD=podman-compose scripts/upgrade_check.sh \
  --base vPREVIOUS \
  --candidate "$VERSION" \
  --database-image-migration
```

The drill stops the app, verifies a fresh backup after the post-restore ingest
check, destroys only its task-owned database volume, starts the candidate
database on a fresh volume, restores and semantically compares that backup,
then starts the candidate app. It rolls back by starting the original database
image on a fresh volume and restoring the pre-upgrade backup; writes made after
that backup must be lost. It never swaps `PGDATA` directories directly.

A clean-install verification on both architectures is still required for every
release.

## Build and publish the PostGIS image

The dedicated `.github/workflows/postgis-image.yml` workflow and
`docker/postgis/Dockerfile` build the supported native database image. They
build native `linux/amd64` and `linux/arm64` images from the
immutable upstream `nickblah/postgis` PostgreSQL 16 and PostGIS 3.6.4 base,
refresh Debian packages, and build the pinned gosu 1.19 source with the pinned
patched Go toolchain. Each native job tests a fresh database, runs the v0.11.0
application HTTPS smoke, and scans the exact local archive with Trivy. Fixable
HIGH or CRITICAL findings fail the workflow; this workflow has no scan
exceptions.

Publication is limited to trusted pushes or manual dispatches on `main` and
uses the separate GHCR package `ghcr.io/meandrousshark/odograph-postgis`.
The publish job loads the exact tested AMD64 and ARM64 archives, publishes
`sha-<full-source-SHA>` tags and their architecture tags, creates the signed
multi-architecture index, signs both child digests, and attaches an SPDX
attestation to each child digest.

Before updating the upstream base or gosu source pins, maintainers must review
the upstream changes and rerun both native architecture builds, fresh database
checks, blocking Trivy scans, and the v0.11.0 application smoke. Record and
verify the resulting index and child digests, signatures and SPDX subjects
before pinning the index in repository references. The current default is the
immutable index recorded above. Updating it is a database-image migration and
requires the fresh-target backup and rollback procedure in
[Upgrading](upgrading.md#upgrading-the-postgis-database-image). This image
change does not announce a new application release or imply a production
deployment.

## Move the floating minor tag

Alongside every exact `vX.Y.Z` tag, the registry carries a floating `vX.Y`
tag that points at the newest patch within that minor version. Operators who
would otherwise reach for `latest` pin `vX.Y` instead: they pick up patch fixes
without silently crossing a minor boundary, which is where migrations and, before
1.0, breaking changes are allowed to land. There is deliberately no `latest` tag.
From 1.0 a floating major tag, `v1`, is also published; see
[Move the floating major tag](#move-the-floating-major-tag). The first 1.x
release creates both the `v1.0` and `v1` floating tags.

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
verifies, using `$IDENTITY` and `$ISSUER` from
[Verifying a release's signature and SBOM](#verifying-a-releases-signature-and-sbom):

```sh
crane digest "$IMAGE:$VERSION"
crane digest "$IMAGE:${VERSION%.*}"
cosign verify \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE:${VERSION%.*}"
```

Both digests must be identical and the signature must verify through the
floating tag. The signing certificate identity still names the immutable
version, which is correct: it records which release the artifact came from.

A prerelease never moves a floating tag.

## Move the floating major tag

From 1.0, the registry also carries a floating `v1` tag that points at the
newest verified stable 1.x release. The first 1.x release creates both `v1.0`
and `v1`; run both retag steps for it. It is an alias, like the floating minor
tag, and it exists for operators who deliberately choose to follow every 1.x
release.

Move `v1` only to a verified stable 1.x release, and only after the published
artifact, its signature, its SBOM and the forward-upgrade checks all pass.
Never move it for a prerelease or for a 2.x release. Never retag an exact
version tag (the registry does not prevent it; operators verify by digest),
and minor tags such as `v1.0` continue to move as described above.

Retag by digest, exactly as for the minor tag, so `v1` is an alias for the
index that was already signed:

```sh
crane tag "$IMAGE:$VERSION" "${VERSION%%.*}"
```

Then confirm the alias is the same artifact and still verifies, using the same
`$IDENTITY` and `$ISSUER`:

```sh
crane digest "$IMAGE:$VERSION"
crane digest "$IMAGE:${VERSION%%.*}"
cosign verify \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE:${VERSION%%.*}"
```

Both digests must be identical and the signature must verify through `v1`.

Pulling `v1` again selects the current release, not the one an operator ran
before, so the operator documentation tells them to record the installed
digest before upgrading. See
[Floating image aliases](upgrading.md#floating-image-aliases).

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
