# Security

This document describes the security posture of a self-hosted instance: what
each part of the deployment is trusted to do, what every network-reachable
entry point actually checks, and a hardening checklist you can run against a
real installation. See [SECURITY.md](../SECURITY.md) for how to report a
vulnerability and what's in scope. See [privacy.md](privacy.md) for exactly
which data can leave your instance and under what configuration — this
document is about who can reach the application and what it does with what
they send it, not about outbound data flows.

This is a single-user, self-hosted application. The threat model throughout
assumes the person operating the instance is trusted and controls the host
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
  and the database is not encrypted — both containers are expected to share a
  private network the operator controls, not a network with unrelated,
  untrusted workloads on it.
- **Reverse proxy.** The application never terminates TLS itself. Whatever
  reverse proxy sits in front of it is trusted to do that correctly, to set
  `X-Forwarded-For` and `X-Forwarded-Proto` honestly, and to be the only path
  by which a browser or phone reaches the application. See
  [Reverse proxy and TLS](reverse-proxy.md) for the supported proxy
  configurations.
- **Phone (OwnTracks).** The device posting location fixes is trusted with
  its own ingest credentials and nothing else — it has no access to the web
  UI, and a compromised phone can only inject or spoof its own location
  history, not read anyone else's data (there is only one instance, and no
  other data to read).
- **Optional external services.** OSRM, the geocoder, ntfy, and SMTP are all
  optional and, when configured, are treated as semi-trusted network peers:
  the application sends them the minimum data each needs to do its job (see
  [privacy.md](privacy.md) for exactly what that is per service) and treats
  their responses as untrusted input, but does not otherwise sandbox them.
  Leaving any of them unconfigured removes that trust relationship entirely;
  the application runs fully without any of them.

## Entry points

Every network-reachable route, and what actually guards it:

- **`/ingest`** — HTTP Basic auth against `INGEST_USERNAME`/`INGEST_PASSWORD`,
  compared with a constant-time comparison. A failed attempt counts against a
  per-IP limiter; once that IP is over its threshold, further attempts get a
  bare `429` without touching the credential check again. A body over
  `INGEST_MAX_BODY_BYTES` is dropped before it's parsed. Anything that
  authenticates but fails to parse as a valid OwnTracks location fix is
  accepted and silently dropped (HTTP `200`), never retried by the device and
  never logged with its own content — this endpoint's job is to never make an
  OwnTracks device loop on a poison payload.
- **`/login`, `/login/local`** — the local-login form. `/login/local` checks
  the same per-IP limiter described in
  [Rate limiting](#rate-limiting-and-proxy-trust) before touching the
  database, and reports an identical generic failure ("Invalid email or
  password") whether the failure was no local administrator, a wrong email,
  or a wrong password. A successful login clears and rebuilds the session
  (fixation defense) and issues a fresh CSRF token.
- **`/setup`** — reachable only while `ADMIN_TOKEN` is set in the
  environment; the route 404s otherwise. It shares the same per-IP limiter as
  local login. The token is checked with a constant-time comparison and is
  single-use: once it successfully creates or resets the local administrator,
  a hash of it is stored and a repeat submission of the same token is
  rejected as already-used, distinct from (and not counted against) the
  failure limiter.
- **The OIDC callback (`/auth/callback`)** — only reachable when OIDC is
  configured. Authlib's own `state` check rejects a replayed or forged
  callback before any token exchange happens. Beyond that, the callback
  shares the same per-IP limiter as local login and setup: a caller that gets
  past the `state` check but is rejected by the identity provider, or whose
  email doesn't match a configured `ALLOWED_EMAIL`, counts as a failure on
  that ledger. The limiter is checked *before* the token exchange, not after,
  because the exchange itself is a real outbound HTTP request to your
  identity provider — an unmetered caller could otherwise loop garbage
  authorization codes at this endpoint and get your instance's egress address
  throttled by your own provider, entirely without a valid session ever
  existing.
- **`/healthz`** — deliberately unauthenticated, and deliberately minimal: it
  runs `SELECT 1` against the database and returns `{"ok": true}` or an
  error. It discloses no version string, no git revision, no schema or
  detector version, and no worker or configuration state. It exists for
  compose healthchecks and your reverse proxy's own upstream check, and
  nothing else should ever depend on what it returns beyond up/down.
- **`/static`** — served with `no-cache` on every response so a browser
  always revalidates instead of trusting a stale cached copy after a
  deploy. Every script the application loads is vendored here at build time;
  the application never loads a script from a third-party origin at runtime.

Everything else in the application (the trip list, trip detail, review
queue, settings, expenses, and every htmx partial and POST behind them)
requires an authenticated session, plus a matching `X-CSRF-Token` header on
every state-changing htmx request or a matching hidden field on the plain
login/setup forms that can't set custom headers.

## Rate limiting and proxy trust

Rate limiting is per-IP, in-memory, and counts failures only — a phone
flushing a backlog of correct-credential requests, or a person typing their
password right the first time, is never throttled. There are two limiters:
one dedicated to `/ingest`, and one shared by `/login/local`, `/setup`, and
the OIDC callback, since all three are the same shape of risk: an
unauthenticated caller feeding the application plausible-looking credentials.

Both limiters key on `client_ip()`, which reads only the address Uvicorn's
`--proxy-headers` handling has already normalized from a trusted proxy's
`X-Forwarded-For` — never a header read directly by application code. That
makes `FORWARDED_ALLOW_IPS` a security-relevant setting, not a cosmetic
logging knob: if it's set too permissively, a client can spoof the address
both limiters key on and dodge them entirely. The application logs a startup
warning whenever `FORWARDED_ALLOW_IPS` is `*` or empty outside development
mode, precisely because that combination is only safe under one specific
topology — see the checklist below.

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
configuration **presence** booleans (for example, "is a geocoder key set")
— never the values themselves. Neither surface ever contains a coordinate,
an address, a secret, or a raw payload.

Reachability of OSRM, the geocoder, ntfy, and SMTP is checked only on an
explicit "check now" click on the Settings page (or by running the CLI
command at all) — never on a background timer. A failed check reports only
an HTTP status code or an exception's type name, never the exception's
message: an HTTP client's error message conventionally embeds the full
request URL, and the geocoder's URL carries your API key as a query
parameter. The SMTP check connects and issues a `NOOP`; it never
authenticates, so clicking it repeatedly can't get your mail account
rate-limited or locked by your provider.

The in-container CLI command matters most exactly when the authenticated
page can't be reached — a broken database connection, a crashed worker, or
an app that won't start — which is the case an operator most needs a
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

2. **Set HSTS at the proxy, not here — or explicitly opt in.** This
   application does not send `Strict-Transport-Security` by default. That's
   deliberate: a wrongly scoped `max-age` is painful for a browser to forget
   once sent, and this process has no way to know or undo a proxy-level TLS
   mistake underneath it. Prefer configuring the header at your proxy. If
   your proxy can't, set `HSTS_MAX_AGE` (seconds) in `.env` — the header is
   then emitted, but only on a request the application already sees as
   HTTPS via the forwarded scheme, never on a plain HTTP request. Leaving it
   unset (or `0`) sends no header at all.

3. **Get `FORWARDED_ALLOW_IPS` right for your actual topology.** The shipped
   default, `FORWARDED_ALLOW_IPS=*`, is safe only because the shipped
   compose file publishes the application solely on `127.0.0.1:8077` —
   nothing except a local process (your reverse proxy) can ever present a
   forwarded header to it. Both failed-auth rate limiters, and every
   recorded client address, derive entirely from what this setting lets
   Uvicorn trust. If you ever publish the application port to a
   non-loopback address, or route a proxy to it through anything other than
   that loopback binding, narrow this to the proxy's exact IP or CIDR — see
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
   time. `SESSION_SECRET` and `ADMIN_TOKEN` are both safely regenerable at
   any time — see [Password recovery and session revocation](../README.md#password-recovery-and-session-revocation)
   for exactly what changing each one does and doesn't invalidate.
   `INGEST_PASSWORD` is also safely regenerable, but every OwnTracks device
   needs its stored password updated to match before it can post again.
   `POSTGRES_PASSWORD` is fixed for the life of a given database volume —
   see [Protecting `.env`](backups.md#protecting-env) for what a lost or
   rotated value actually costs for each secret.

6. **Treat the ingest credential like any other password, not like a
   throwaway.** `INGEST_USERNAME`/`INGEST_PASSWORD` are checked with a
   constant-time comparison and back a per-IP failure limiter, but they are
   still a single shared Basic-auth credential known to every OwnTracks
   device you configure. Give it real entropy, keep it out of shell history
   and screenshots, and rotate it (updating every device's OwnTracks
   configuration to match) if you ever suspect it leaked.

7. **Run the `ADMIN_TOKEN` lifecycle to completion: set, bootstrap, clear.**
   `/setup` 404s whenever `ADMIN_TOKEN` is unset, so the bootstrap page is
   reachable at all only while it's populated. After you've created (or
   reset) the local administrator through `/setup`, set `ADMIN_TOKEN=` in
   `.env` and recreate the app container so it loads the change. Leaving a
   populated token in place after setup is complete leaves an unnecessary
   standing credential capable of resetting your administrator password.

8. **Encrypt backups, and control who can read them.** `scripts/backup_database.sh`
   captures your full location history, credentials hashes, and every other
   application table — treat the resulting archive with at least the same
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
   dependency your diagnostics have to account for. If you don't need
   road-snapping, reverse geocoding, push reminders, or email digests, leave
   the corresponding `.env` variables unset. The geocoder itself is a choice,
   not a single fixed dependency: `GEOCODE_PROVIDER=geoapify` sends trip
   coordinates and your API key to a third-party host, while
   `GEOCODE_PROVIDER=nominatim` keeps that same lookup on infrastructure you
   run yourself. Choosing `nominatim` narrows this item's trust relationship
   without eliminating it — it becomes another semi-trusted network peer of
   yours rather than a third party's.

10. **If a credential leaks, rotate it and understand exactly what that
    does and doesn't fix:**
    - **`INGEST_PASSWORD`** — generate a new value, update it in `.env`,
      recreate the app, and update every OwnTracks device's configuration to
      match before it can post again. A leaked ingest credential lets
      someone inject fabricated location fixes or read nothing (the
      endpoint has no read path), but does not expose the web UI.
    - **`SESSION_SECRET`** — generate a new value and recreate the app. This
      immediately invalidates every existing signed session cookie,
      including your own — everyone has to sign back in. It does not touch
      any stored data.
    - **`ADMIN_TOKEN`** — generate a new value, put it in `.env`, and
      recreate the app to reissue a fresh one-time `/setup` page. A leaked
      but already-cleared token (step 7 above) is not itself exploitable,
      since `/setup` 404s with no token set.
    - **A local administrator password** — set a fresh `ADMIN_TOKEN`,
      recreate the app, use `/setup`'s reset flow to set a new password,
      then clear `ADMIN_TOKEN` again and recreate the app once more. This
      alone does not invalidate any session issued before the reset; also
      rotate `SESSION_SECRET` if you need every existing session revoked
      too.
    - **OIDC client secret** — rotate it with your identity provider and
      update `OIDC_CLIENT_SECRET` in `.env`, then recreate the app.
    - **Optional-service credentials** (`GEOCODE_API_KEY`, ntfy token or
      username/password, SMTP username/password) — rotate with the
      respective provider and update `.env`. None of these grant access to
      your instance itself; they only grant whatever access that third
      party's own credential scope allows.
    - **`POSTGRES_PASSWORD`** — this only matters if you still have the
      original database volume: the Postgres image applies it only when
      initializing an empty data directory, so an existing volume's actual
      role password was fixed when that volume was first created. See
      [Protecting `.env`](backups.md#protecting-env) for the mechanics of
      changing it against a live volume versus a fresh one.

## What to do about a compromise

If you believe the application, its host, or its database has been
compromised:

1. **Take the instance offline first, then investigate** — stop the `app`
   service (or the whole compose project) rather than leaving a possibly
   compromised process running while you look into it. Leave the `dbdata`
   volume untouched; don't run anything destructive against it until you
   understand what happened.
2. **Rotate every credential in `.env`** using the steps in the previous
   section, treating all of them as suspect rather than trying to guess
   which one was actually used.
3. **Sign everyone out** by rotating `SESSION_SECRET`, even if you don't
   suspect a session was hijacked specifically — it costs nothing but a
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
   [disaster recovery runbook](backups.md#disaster-recovery) — don't restore
   over the live volume you're not sure about.
6. **Review what actually changed.** The diagnostics report can tell you
   whether workers, migrations, and connectivity look normal now, but it
   cannot tell you what happened historically — check your log retention
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
architecture digest is itself signed with `cosign` — keylessly, via GitHub
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
than hardcoding either — this keeps the same commands working if the
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
workflow run for that exact repository and tag — stop and investigate before
deploying it, the same as you would for a mismatched digest.

## Container hardening

The released image runs as fixed, numeric UID/GID `10001:10001` — not root,
and not a named user, so both rootful Docker and rootless Podman map the
identity the same way. The application writes nothing to disk at runtime (no
uploads, no cache directory, no file-based logging), so the shipped compose
defaults run it with an empty Linux capability set (`cap_drop: [ALL]`),
`no-new-privileges`, a read-only root filesystem, and a small `tmpfs` for
`/tmp`. If you bind-mount a host directory into the container yourself,
make sure that path is readable (and writable, if applicable) by UID/GID
`10001` — nothing does that chown for you.

The compose topology publishes only the application, on
`127.0.0.1:8077`; the database and the optional OSRM service have no
published ports at all and are reachable only from the application over the
private compose network.
