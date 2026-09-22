# Security

This document describes the security posture of a self-hosted instance: what
each part of the deployment is trusted to do, what every network-reachable
entry point actually checks, and a hardening checklist you can run against a
real installation. See [SECURITY.md](../SECURITY.md) for how to report a
vulnerability and what's in scope. See [privacy.md](privacy.md) for exactly
which data can leave your instance and under what configuration. This
document is about who can reach the application and what it does with what
they send it, not about outbound data flows.

This remains a single-account, self-hosted application. Account ownership is
explicit in queries and restricted database roles are active, but row-level
security policies are only prepared, not enabled or forced. The singleton
account guard remains and invitations are unavailable. This stage must not be
operated as a multi-user service. The operator is trusted and controls the host
and database; nothing here defends against a hostile operator.

## Trust boundaries

- **Operator.** Whoever controls the host, the `.env` file, and the database
  can already read or change anything the application stores. That's inherent
  to self-hosting, not a gap this document tries to close.
- **Host.** The container runtime, the filesystem, and anything else running
  on the same machine are trusted by the application. A compromised host
  compromises the application regardless of any setting described here.
- **Database.** Postgres/PostGIS is reachable only from the application
  container over the compose network; the shipped compose file publishes no
  database port to the host or the internet. The connection between the app
  and the database is not encrypted. Both containers are expected to share a
  private network the operator controls, not a network with unrelated,
  untrusted workloads on it.
- **Reverse proxy.** The application never terminates TLS itself. Whatever
  reverse proxy sits in front of it is trusted to do that correctly, to set
  `X-Forwarded-For` and `X-Forwarded-Proto` honestly, and to be the only path
  by which a browser or phone reaches the application. See
  [Reverse proxy and TLS](reverse-proxy.md) for the supported proxy
  configurations.
- **Optional OIDC identity provider.** When configured, the provider is trusted
  to authenticate its users and maintain stable issuer and subject values.
  Odograph stores that exact pair as the linked identity. Provider email and
  display name are non-authoritative metadata and cannot create or select a
  link.
- **Phone (OwnTracks).** The device posting location fixes is trusted with
  its own ingest credentials and nothing else. It has no access to the web
  UI, and a compromised phone can only inject or spoof its own location
  history, not read the ledger. An upgraded shared login can inject into
  legacy streams within its owning account until converted or revoked.
- **Optional external services.** OSRM, the geocoder, ntfy, and SMTP are all
  optional and, when configured, are treated as semi-trusted network peers:
  the application sends them the minimum data each needs to do its job (see
  [privacy.md](privacy.md) for exactly what that is per service) and treats
  their responses as untrusted input, but does not otherwise sandbox them.
  Leaving any of them unconfigured removes that trust relationship entirely;
  the application runs fully without any of them.

## Entry points

Every network-reachable route, and what actually guards it:

- **`/ingest`**: HTTP Basic auth resolves a durable, hashed credential to an
  enabled account and stable device before accepting data. Credential and
  device generation/revocation are checked again in the write transaction.
  A new device's payload `tid` cannot select another device or account. Only
  the migrated shared-login adapter uses its account's legacy alias map.
  Environment credentials cannot restore a revoked or replaced login.
  Authentication failures use the per-IP limiter. An authenticated sender
  without an established account receives a retryable `503` and writes no
  data. Oversized or malformed authenticated input retains the poison-message
  acknowledgment behavior: `200` with an empty list, without logging the body.
  This server response is not a measured guarantee of OwnTracks offline-queue
  behavior; verify credential transitions on the real phone before retiring
  its old setup.
- **`/login`, `/login/local`**: the local-login form. `/login/local` checks
  the same per-IP limiter described in
  [Rate limiting](#rate-limiting-and-proxy-trust) before touching the
  database, and reports an identical generic failure ("Invalid email or
  password") whether the failure was no local administrator, a wrong email,
  or a wrong password. A successful login clears and rebuilds the session
  (fixation defense) and issues a fresh CSRF token.
- **`/signup`**: reachable only when `INITIAL_ADMIN_SIGNUP=1`, no account
  exists, and development auth bypass is off. The first successful transaction
  creates the sole administrator. A database singleton constraint prevents a
  concurrent request or application bug from creating a second account. The
  durable account row closes both signup routes permanently.
- **`/settings/account`**: requires the caller's enabled account session.
  Password changes require the current password, matching new passwords, and
  a session-bound CSRF token. A successful change increments the account's
  authentication version, invalidating older sessions while issuing a fresh
  valid session to the browser that completed the change. Linking OIDC requires
  the current local password and a fresh provider authorization. Unlinking
  requires the current password and explicit confirmation, removes the stored
  identity, increments the authentication version, and clears the current
  session.
- **`/settings/tracking`**: requires the caller's enabled account session and
  form CSRF token for mutations. Device creation and password replacement
  show the new secret once, use `no-store`, and preserve stable device history.
  Conversion retires that stream's legacy aliases. Revocation persists across
  restart. A submitted ID belonging to another account cannot select or mutate
  its device or credential.
- **`/settings/diagnostics/check`**: requires an administrator session and CSRF.
  Shared service diagnostics are also visible only to the administrator;
  personal device status remains available with the account's settings.
- **`/account/establish`**: available only to the narrow legacy OIDC session
  used by an upgraded OIDC-only installation with no account and public signup
  disabled. That accountless session cannot access personal routes. It creates
  local administrator credentials and links the current
  provider identity in one transaction. `ALLOWED_EMAIL`, when set, gates only
  entry into this one-time transition. The operator command is the safer
  alternative if the provider's trust boundary is too broad or unavailable.
- **The OIDC callback (`/auth/callback`)**: handles distinct login and linking
  flows protected by state and nonce checks. A linked login resolves the exact
  provider issuer and subject to the same account used by local login. Email is
  display metadata, not an account selector, and `ALLOWED_EMAIL` does not apply
  to linked login. Linking also requires a current account session and the
  password reauthentication that started the flow. The callback does not
  persist access, refresh, or ID tokens. It shares the failed-auth limiter with
  other authentication checks, and the limiter is checked before the outbound
  token exchange so garbage authorization codes cannot freely consume provider
  requests.
- **`/healthz`**: deliberately unauthenticated, and deliberately minimal: it
  runs `SELECT 1` against the database and returns `{"ok": true}` or an
  error. It discloses no version string, no git revision, no schema or
  detector version, and no worker or configuration state. It exists for
  compose healthchecks and your reverse proxy's own upstream check, and
  nothing else should ever depend on what it returns beyond up/down.
- **`/static`**: served with `no-cache` on every response so a browser
  always revalidates instead of trusting a stale cached copy after a
  deploy. Every script the application loads is vendored here at build time;
  the application never loads a script from a third-party origin at runtime.

Everything else in the application (the trip list, trip detail, review
queue, settings, expenses, and every htmx partial and POST behind them)
requires an authenticated session, plus a matching `X-CSRF-Token` header on
every state-changing htmx request or a matching hidden field on the plain
login, signup, and Account Settings forms that can't set custom headers.

## Database and browser isolation

Startup closes the privileged migration/setup connection before serving
requests. Identity operations use `odograph_control`; personal routes and
workers use `odograph_runtime` with an immutable principal and transaction-local
account context. Every personal query still filters ownership explicitly,
because the prepared policies are disabled. Composite foreign keys reject
cross-account references. The live role contract and saved credentials must
validate exactly; there is no privileged fallback for failed startup checks.
See [Database roles](configuration.md#account-ownership-and-database-roles).

Private responses carry `Cache-Control: no-store`. HTMX history snapshots are
disabled, and account-marker checks discard responses or reload old tabs after
an account change. These are browser privacy defenses; server-side session and
ownership checks remain authoritative. Accountless legacy sessions are limited
to establishment and cannot reach personal data.

## Rate limiting and proxy trust

Failure counters are per-IP and in-memory. There are two limiters:
one dedicated to `/ingest`, and one shared by local credential checks,
signup validation, and the OIDC callback, since they are the same shape of risk: an
unauthenticated caller feeding the application plausible-looking credentials.
The local credential limiter counts failures only; a person typing their
password right the first time is not throttled.

Ingest checks the client's failure window before credential verification.
A blocked IP receives `503` with `Retry-After` without reading the body, even
if the new request carries correct credentials. Each application process also
allows at most two concurrent ingest verifications. When both slots are busy,
additional requests receive `503` with `Retry-After: 1` before verification or
body reads. Cancelling a request does not release its slot until verification
finishes, and a completed failure still counts. Successful verification does
not increment the failure counter; devices can retry after either limit clears.
Ingest uses `503` for these temporary limits because the
[OwnTracks iOS response handler](https://github.com/owntracks/ios/blob/26.2.3/OwnTracks/OwnTracks/Connection.m#L508-L532)
deletes a queued message after any `4xx` response, including `429`. Incorrect credentials
still receive `401`; the separate local credential limiter still uses `429`.

Both limiters key on `client_ip()`, which reads only the address Uvicorn's
`--proxy-headers` handling has already normalized from a trusted proxy's
`X-Forwarded-For`, never a header read directly by application code. That
makes `FORWARDED_ALLOW_IPS` a security-relevant setting, not a cosmetic
logging knob: if it's set too permissively, a client can spoof the address
both limiters key on and dodge them entirely. The application logs a startup
warning whenever `FORWARDED_ALLOW_IPS` is `*` or empty outside development
mode, precisely because that combination is only safe under one specific
topology. See the checklist below.

Two things this scope deliberately does not cover: per-account limiting (this
is a single-administrator application, so there is no meaningful notion of a
second account to protect), and a background or IP-reputation-based ban list.
A determined attacker who can present many source addresses is not stopped by
this; that's a job for your reverse proxy or network edge, not this
application.

## Diagnostics

An authenticated Settings page, and `python -m app.diagnose` run inside the
container, share one report: application/git/schema/detector versions,
database connectivity and pool statistics, whether applied migrations match
what the running code expects, per-worker state (enabled or not, last run,
last success, last failure's exception type, next scheduled run), and
configuration **presence** booleans (for example, "is a geocoder key set"),
never the values themselves. Neither surface ever contains a coordinate,
an address, a secret, or a raw payload.

Reachability of OSRM, the geocoder, ntfy, and SMTP is checked only on an
explicit "check now" click on the Settings page (or by running the CLI
command at all), never on a background timer. A failed check reports only
an HTTP status code or an exception's type name, never the exception's
message: an HTTP client's error message conventionally embeds the full
request URL, and the geocoder's URL carries your API key as a query
parameter. The SMTP check connects and issues a `NOOP`; it never
authenticates, so clicking it repeatedly can't get your mail account
rate-limited or locked by your provider.

The in-container CLI command matters most exactly when the authenticated
page can't be reached (a broken database connection, a crashed worker, or
an app that won't start), which is the case an operator most needs a
diagnostic for.

## Hardening checklist

Run through this against your actual running installation, not just its
configuration files.

1. **TLS terminates at your reverse proxy, not at this application.** The
   application never speaks TLS itself; `http://127.0.0.1:8077` is a
   loopback-only upstream address, never a public listener. Confirm your
   proxy serves the production hostname over HTTPS, and that visiting the
   plain HTTP loopback address from off-host fails (because nothing routes
   to it) rather than serving the app in the clear. See
   [Reverse proxy and TLS](reverse-proxy.md) for worked Caddy and nginx
   configurations.

2. **Set HSTS at the proxy, not here, or explicitly opt in.** This
   application does not send `Strict-Transport-Security` by default. That's
   deliberate: a wrongly scoped `max-age` is painful for a browser to forget
   once sent, and this process has no way to know or undo a proxy-level TLS
   mistake underneath it. Prefer configuring the header at your proxy. If
   your proxy can't, set `HSTS_MAX_AGE` (seconds) in `.env`. The header is
   then emitted, but only on a request the application already sees as
   HTTPS via the forwarded scheme, never on a plain HTTP request. Leaving it
   unset (or `0`) sends no header at all.

3. **Get `FORWARDED_ALLOW_IPS` right for your actual topology.** The shipped
   default, `FORWARDED_ALLOW_IPS=*`, is safe only because the shipped
   compose file publishes the application solely on `127.0.0.1:8077`.
   Nothing except a local process (your reverse proxy) can ever present a
   forwarded header to it. Both failed-auth rate limiters, and every
   recorded client address, derive entirely from what this setting lets
   Uvicorn trust. If you ever publish the application port to a
   non-loopback address, or route a proxy to it through anything other than
   that loopback binding, narrow this to the proxy's exact IP or CIDR. See
   [Trusting forwarded headers](reverse-proxy.md#trusting-forwarded-headers)
   for the topology-specific guidance. Confirm at startup: the application
   logs a warning naming the exact risk whenever this is `*` or empty
   outside development mode.

4. **Confirm the port really is loopback-bound.** `docker compose ps` or
   `podman-compose ps` should show `127.0.0.1:8077->8000` for the `app`
   service, not `0.0.0.0:8077` or a bare `8077`. Confirm from another host on
   the network that connecting to the application's port directly fails.

5. **Generate every secret with real entropy, and know which ones you can
   rotate later.** `scripts/generate_env.sh` does this for you at install
   time. `SESSION_SECRET` is safely regenerable at any time. See
   [Password recovery and session revocation](../README.md#password-recovery)
   for the account-level recovery commands and session behavior.
   Replace an issued device password in **Settings > Tracking** and update
   that phone. Editing `INGEST_PASSWORD` after the one-time upgrade import
   does not change the saved credential.
   `POSTGRES_PASSWORD` is fixed for the life of a given database volume.
   See [Protecting `.env`](backups.md#protecting-env) for what a lost or
   rotated value actually costs for each secret.

6. **Treat each ingest credential like any other password.** Create separate
   credentials in Tracking, keep the one-time secret out of shell history,
   screenshots, and logs, and replace or revoke it if it leaks. A credential
   can submit data for its owned device but cannot read the ledger. Move
   upgraded devices off the migrated shared login and revoke it when all
   remaining phones have their own credentials.

7. **Keep account creation and recovery narrow.** Fresh generated
   configuration sets `INITIAL_ADMIN_SIGNUP=1`; the first account row closes
   signup even if that value remains unchanged. Existing configurations that
   omit it stay fail-closed. Recover a missing account or lost password only
   with `python -m app.manage_account create-admin` or `reset-password` inside
   the application container. The command accepts no password argument.

8. **Encrypt backups, and control who can read them.** `scripts/backup_database.sh`
   captures your full location history, credentials hashes, and every other
   application table. Treat the resulting archive with at least the same
   care as `.env`. Neither the database dump nor `.env` are backed up
   together automatically; see [Backups and disaster recovery](backups.md)
   for what each backup does and doesn't cover, and
   [Encryption and off-host copies](backups.md#encryption-and-off-host-copies)
   for using an encrypting tool such as restic or Borg rather than leaving
   an unencrypted `backups/` directory as your only copy.

9. **Leave optional services off unless you specifically want them.** OSRM,
   the geocoder, ntfy, and SMTP are all opt-in, and the application runs
   fully without any of them. Each one you enable is both an outbound data
   flow (see [privacy.md](privacy.md) for exactly what each sends) and a
   configuration change (see the
   [configuration reference](configuration.md#external-services)), plus a
   dependency your diagnostics have to account for. If you don't need
   road-snapping, reverse geocoding, push reminders, or email digests, leave
   the corresponding `.env` variables unset. The geocoder itself is a choice,
   not a single fixed dependency: `GEOCODE_PROVIDER=geoapify` sends trip
   coordinates and your API key to a third-party host, while
   `GEOCODE_PROVIDER=nominatim` keeps that same lookup on infrastructure you
   run yourself. Choosing `nominatim` narrows this item's trust relationship
   without eliminating it: it becomes another semi-trusted network peer of
   yours rather than a third party's.

10. **If a credential leaks, rotate it and understand exactly what that
    does and doesn't fix:**
    - **A device ingest password**: replace or revoke it in **Settings >
      Tracking**, then update that phone. The old password stops immediately;
      historical trips remain. For a migrated shared login, convert devices
      to their own credentials and revoke the shared login. Editing the old
      environment secret cannot rotate or restore the durable credential.
      An ingest credential permits fabricated uploads, not ledger reads or
      browser sign-in.
    - **`SESSION_SECRET`**: generate a new value and recreate the app. This
      immediately invalidates every existing signed session cookie,
      including your own, so everyone has to sign back in. It does not touch
      any stored data.
    - **A local administrator password**: change it in Account Settings when
      signed in, or run `python -m app.manage_account reset-password` inside
      the app container for operator recovery. Either path increments the
      account authentication version and invalidates older sessions.
    - **OIDC client secret**: rotate it with your identity provider and
      update `OIDC_CLIENT_SECRET` in `.env`, then recreate the app.
      A linked identity still belongs to the same account after client-secret
      rotation because it is keyed by the provider's issuer and subject.
    - **Optional-service credentials** (`GEOCODE_API_KEY`, ntfy token or
      username/password, SMTP username/password). Rotate with the
      respective provider and update `.env`. None of these grant access to
      your instance itself; they only grant whatever access that third
      party's own credential scope allows.
    - **`POSTGRES_PASSWORD`**: this only matters if you still have the
      original database volume: the Postgres image applies it only when
      initializing an empty data directory, so an existing volume's actual
      role password was fixed when that volume was first created. See
      [Protecting `.env`](backups.md#protecting-env) for the mechanics of
      changing it against a live volume versus a fresh one.

11. **Do not tighten `Referrer-Policy` at your proxy.** The application
    sends `Referrer-Policy: strict-origin-when-cross-origin`, which gives
    the map tile host your instance's origin and nothing more: no path, no
    query string, and nothing at all on an HTTPS to HTTP downgrade. A proxy
    that overrides this with `no-referrer` or `same-origin` sends no
    `Referer` at all on cross-origin requests, and OpenStreetMap's tile
    servers answer a refererless request with a 403 error tile instead of
    the map. If your hardening snippet sets this header, drop it and let the
    application's value through. Pointing `MAP_TILE_URL` at a tile host of
    your own is the way to stop sending the origin anywhere.

## What to do about a compromise

If you believe the application, its host, or its database has been
compromised:

1. **Take the instance offline first, then investigate**: stop the `app`
   service (or the whole compose project) rather than leaving a possibly
   compromised process running while you look into it. Leave the `dbdata`
   volume untouched; don't run anything destructive against it until you
   understand what happened.
2. **Rotate every credential in `.env`** using the steps in the previous
   section, treating all of them as suspect rather than trying to guess
   which one was actually used.
3. **Sign everyone out** by rotating `SESSION_SECRET`, even if you don't
   suspect a session was hijacked specifically. It costs nothing but a
   re-login.
4. **Rebuild the host, or at minimum the container images, from a known-good
   base** rather than trusting an image or host you have reason to believe
   was tampered with. If you're re-pulling a released image, verify its
   cosign signature first (see
   [Verifying image signatures and SBOMs](#verifying-image-signatures-and-sboms)
   below) so you know you're rebuilding from the real published artifact and
   not something substituted in transit or at the registry.
5. **Restore application data from a backup taken before the suspected
   compromise window**, into a fresh volume, following the
   [disaster recovery runbook](backups.md#disaster-recovery). Don't restore
   over the live volume you're not sure about.
6. **Review what actually changed.** The diagnostics report can tell you
   whether workers, migrations, and connectivity look normal now, but it
   cannot tell you what happened historically. Check your log retention
   (application logs, proxy logs, host audit logs, whatever you keep) for
   anything from the suspected window.
7. **If the compromise involves a vulnerability in this project itself**,
   report it as described in [SECURITY.md](../SECURITY.md) so a fix can ship
   for other operators.

This application has not undergone third-party penetration testing, and does
not include a web application firewall or an automated ban tool such as
fail2ban. Both are reasonable additions at your network edge if your threat
model calls for them; neither is built into the application itself.

## Supply chain

Released images are multi-architecture (`linux/amd64` and `linux/arm64`).
Every release goes through two blocking gates before anything is published:
`pip-audit` against the locked Python dependencies, and a Trivy scan for
fixable HIGH/CRITICAL findings in each architecture's image. Beyond the
gates, every published architecture digest carries an SPDX software bill of
materials, attested with `cosign`, and the manifest list plus each
architecture digest is itself signed with `cosign`, keylessly, via GitHub
Actions OIDC, so there is no long-lived private signing key for anyone to
generate, store, or leak. Verification checks the signing certificate's
recorded identity (which workflow, in which repository, on which tag)
against the public Rekor transparency log rather than trusting a key at all.
[Releasing](releasing.md) covers the publishing side of this in full; this
section is the operator-verification side.

### Verifying image signatures and SBOMs

Install `cosign` (`brew install cosign`, or see the
[Sigstore install docs](https://docs.sigstore.dev/cosign/system_config/installation/)),
then resolve the repository and expected signing identity dynamically rather
than hardcoding either. This keeps the same commands working if the
repository is ever renamed or moved:

```sh
REPOSITORY="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
IDENTITY="https://github.com/$REPOSITORY/.github/workflows/release.yml@refs/tags/$VERSION"
ISSUER="https://token.actions.githubusercontent.com"
```

`$VERSION` is the release tag you're verifying (for example `v1.0.0`);
`$IMAGE` is the published image reference; `$AMD64_DIGEST`/`$ARM64_DIGEST`
are the per-architecture digests recorded in that release's notes. The
identity string above follows the standard shape Sigstore/GitHub Actions
OIDC certificates use for a workflow-triggered release, but treat it as a
starting point to compare against, not a guarantee: if `cosign verify` ever
rejects it, run the same command with `--certificate-identity-regexp
'https://github\.com/.*/\.github/workflows/release\.yml@refs/tags/.*'`
instead (looser, but still repository- and workflow-scoped) to see the
certificate's actual recorded identity, and use that going forward.

Verify the manifest and both architecture signatures:

```sh
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE:$VERSION"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE@$AMD64_DIGEST"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE@$ARM64_DIGEST"
```

Verify and extract each architecture's attested SBOM:

```sh
cosign verify-attestation --type spdxjson --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$AMD64_DIGEST" | jq -r '.payload' | base64 -d | jq . > sbom-amd64.spdx.json
cosign verify-attestation --type spdxjson --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "$IMAGE@$ARM64_DIGEST" | jq -r '.payload' | base64 -d | jq . > sbom-arm64.spdx.json
```

A failed verification means the artifact wasn't produced by that exact
workflow run for that exact repository and tag. Stop and investigate before
deploying it, the same as you would for a mismatched digest.

## Container hardening

The released image runs as fixed, numeric UID/GID `10001:10001`, not root,
and not a named user, so both rootful Docker and rootless Podman map the
identity the same way. The application writes nothing to disk at runtime (no
uploads, no cache directory, no file-based logging), so the shipped compose
defaults run it with an empty Linux capability set (`cap_drop: [ALL]`),
`no-new-privileges`, a read-only root filesystem, and a small `tmpfs` for
`/tmp`. If you bind-mount a host directory into the container yourself,
make sure that path is readable (and writable, if applicable) by UID/GID
`10001`. Nothing does that chown for you.

The compose topology publishes only the application, on
`127.0.0.1:8077`; the database and the optional OSRM service have no
published ports at all and are reachable only from the application over the
private compose network.
