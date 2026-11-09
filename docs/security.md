# Security

What Odograph protects, the hardening checklist for an installation, and what
to do after a compromise. For what data leaves the instance, see
[privacy.md](privacy.md).

An installation has one administrator, the first account, and the members the
administrator invites. There is no open signup. The operator controls the
host, `.env` and the database, can read anything stored, and is trusted;
nothing here defends against a hostile operator.

## What Odograph protects

- **Sign-in and sessions.** Local passwords, or an optional OIDC provider
  matched by its exact issuer and subject; provider email never selects an
  account. Sessions are signed cookies, sent only over HTTPS outside
  development mode. Every state-changing browser request needs a CSRF token.
  Password, provider and login-email changes require the current password or
  a fresh provider sign-in.
  [Sessions and sign out everywhere](accounts.md#sessions-and-sign-out-everywhere)
  covers what ends a session.
- **Signup.** `/signup` works only when `INITIAL_ADMIN_SIGNUP=1` and no
  account has ever been created. Members join only by invitation.
- **Device credentials.** Each tracking device has its own password, stored as
  a hash and shown once. It can submit location for that device only; it
  cannot sign in or read trips. Replacing or revoking it in
  **Settings > Tracking** stops the old password.
- **Mobile app grants.** App tokens are stored as digests and listed under
  **Signed-in apps**, where each can be revoked. A browser session cookie never
  authenticates an `/api/` request.
- **Account isolation.** Each account's location history, trips and reports
  are private to it, including from the administrator. Requests run as
  restricted database roles under row-level security enabled and forced on
  every account-owned table, and queries also filter by owner. Startup
  refuses to serve if the role contract does not validate. See
  [Database roles](configuration.md#account-ownership-and-database-roles).
- **Rate limits.** Failed sign-in, API token and ingest attempts are limited
  per client address, in memory, so they do not stop an attacker spread over
  many addresses. Password-reset requests are also limited per submitted
  email. A limited `/ingest` request gets `503` with `Retry-After`,
  because OwnTracks discards a queued message after any `4xx`.
- **Proxy trust.** The client address comes from `X-Forwarded-For` as allowed
  by `FORWARDED_ALLOW_IPS`; under `*` the rightmost entry is used. Outside
  development mode, startup logs a warning when it is `*` or empty.
- **Loopback-only port.** `compose.yaml` publishes the app only on
  `127.0.0.1:8077`. The database and OSRM publish no ports. Odograph never
  terminates TLS.
- **Browser.** HTML responses carry a Content-Security-Policy, and every
  script is served from the instance. Private pages are sent `no-store` and
  htmx history snapshots are disabled.
- **Unauthenticated endpoints.** `/healthz` returns only whether the
  database answers. `/api/v1/capabilities` returns the API versions and
  whether mobile sign-in is available.
- **Signed images.** Release images are signed with cosign and carry attested
  SBOMs; see [Release images](#release-images).
- **Logs.** [Logging](privacy.md#logging) lists what each log path leaves
  out.
- **Container.** The image runs as UID/GID `10001`. `compose.yaml` also runs
  it with no Linux capabilities, `no-new-privileges`, a read-only root
  filesystem and a `tmpfs` `/tmp`. A host directory you bind-mount must be
  readable (and writable, if needed) by `10001`.

## Hardening checklist

1. Terminate TLS at your reverse proxy and proxy to `127.0.0.1:8077`
   ([Reverse proxy and TLS](reverse-proxy.md)).
2. Set HSTS at the proxy. If it cannot, set `HSTS_MAX_AGE` (seconds); Odograph
   then sends it on HTML responses to HTTPS requests only.
3. Keep `FORWARDED_ALLOW_IPS=*` only while the port is loopback-bound behind
   one proxy. Otherwise list each proxy's exact IP or CIDR
   ([Trusting forwarded headers](reverse-proxy.md#trusting-forwarded-headers)).
4. Confirm `docker compose ps` shows `127.0.0.1:8077->8000` for `app`, and
   that the port is unreachable from another host.
5. Generate secrets with `scripts/generate_env.sh` and protect `.env`
   ([Protecting `.env`](backups.md#protecting-env)).
6. Give each phone its own credential in **Settings > Tracking**, and revoke
   the migrated shared login once no phone uses it.
7. Keep `INITIAL_ADMIN_SIGNUP=0` and create the first account with
   `docker compose exec app python -m app.manage_account create-admin`.
8. Encrypt backups and limit who can read them
   ([Encryption and off-host copies](backups.md#encryption-and-off-host-copies)).
9. Leave OSRM, the geocoder, ntfy and SMTP unset unless you need them; each
   one you enable sends data ([privacy.md](privacy.md)).
10. Do not override `Referrer-Policy` at the proxy. OpenStreetMap's tile
    servers answer a request with no `Referer` with an error tile.
11. Verify each app image with `scripts/verify_image.sh` before starting it
    ([Pin an exact release](upgrading.md#pin-an-exact-release)).
12. Never set `DEV_NO_AUTH=1` on a deployed instance; it turns authentication
    off.

## What to do about a compromise

1. Stop the `app` service. Leave the `dbdata` volume untouched.
2. Rotate every secret in `.env`. A new `SESSION_SECRET` signs out every
   browser, not signed-in apps. Rotate `OIDC_CLIENT_SECRET` and
   optional-service credentials with their providers. For
   `POSTGRES_PASSWORD`, see [Protecting `.env`](backups.md#protecting-env).
3. Rebuild the host or containers from a known-good base, and verify the
   image signature before starting it.
4. Restore a backup taken before the compromise into a fresh volume
   ([Disaster recovery](backups.md#disaster-recovery)). The restore signs out
   every browser and app, but brings back the passwords and device
   credentials the backup holds.
5. After the restore, replace every device password in
   **Settings > Tracking** and have each account change its password. If you
   do not restore, also have each account use **Sign out everywhere**.
6. Review application, proxy and host logs for the suspected window.
   `python -m app.diagnose` inside the container shows current state, not
   history.
7. If Odograph itself was vulnerable, report it.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).

## Release images

Release images are built for `linux/amd64` and `linux/arm64`. A release
publishes only after `pip-audit` of the locked dependencies and a Trivy scan
of each architecture for fixable HIGH and CRITICAL findings pass, apart from
findings that release's `CHANGELOG.md` section accepts. The
manifest list and each architecture digest are signed with keyless cosign by
the release workflow, and each architecture carries an attested SPDX SBOM.
`scripts/verify_image.sh` is the routine signature check; the steps below
check every digest and extract the SBOMs.

### Verifying image signatures and SBOMs

Install [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
and set:

```sh
IMAGE=ghcr.io/meandrousshark/odograph
VERSION=vX.Y.Z  # the release tag
IDENTITY="https://github.com/MeandrousShark/odograph/.github/workflows/release.yml@refs/tags/$VERSION"
ISSUER="https://token.actions.githubusercontent.com"
```

Use this exact identity and issuer; do not loosen them to a pattern. Read
the per-architecture digests from the tag, then verify each one:

```sh
AMD64_DIGEST="$(docker manifest inspect "$IMAGE:$VERSION" | jq -r '.manifests[] | select(.platform.architecture == "amd64") | .digest')"
ARM64_DIGEST="$(docker manifest inspect "$IMAGE:$VERSION" | jq -r '.manifests[] | select(.platform.architecture == "arm64") | .digest')"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE:$VERSION"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE@$AMD64_DIGEST"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE@$ARM64_DIGEST"
cosign verify-attestation --type spdxjson --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$AMD64_DIGEST" | jq -r '.payload' | base64 -d | jq . > sbom-amd64.spdx.json
cosign verify-attestation --type spdxjson --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$ARM64_DIGEST" | jq -r '.payload' | base64 -d | jq . > sbom-arm64.spdx.json
```

If any check fails, the image was not produced by that release workflow for
that tag. Treat it as unverified and stop: do not deploy it.
