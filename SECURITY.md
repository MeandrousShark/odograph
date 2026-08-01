# Security Policy

See [docs/security.md](docs/security.md) for the trust model, what each
network entry point checks, an operator hardening checklist, and how to
verify a released image's signature and software bill of materials. This
document covers vulnerability reporting, supported versions, and scope.

## Reporting a vulnerability

Please report vulnerabilities using GitHub's private vulnerability
reporting: open the repository's **Security** tab and select **Report a
vulnerability**. This creates a private advisory that only the maintainer
(and anyone you add) can see, so details don't become public before a fix
ships.

If private vulnerability reporting is unavailable, email
`security@hannoncloud.com`. Do not include vulnerability details in a public
issue.

## Supported versions

Only the latest release is supported. Fixes ship as a new release, not as
backports to older tags. There is no SLA and no guaranteed response time;
reports are handled on a best-effort basis by a single maintainer.

## Scope

This is a self-hosted, single-user application. The threat model assumes
the operator running the instance is trusted and controls the host and
database.

**In scope** (please report):

- Authentication or session bypass (OIDC login, session cookies, CSRF
  protection).
- Abuse of the location-ingest endpoint (spoofing, injection, denial of
  service reachable without valid credentials).
- Anything that exposes location history, GPS coordinates, addresses, or
  credentials/API keys to a party who shouldn't have access.
- Any other bug that lets an unauthenticated or unauthorized party read or
  modify data.

**Out of scope:**

- Attacks that require a hostile instance operator. The operator owns the
  host and database and can already read or modify anything stored there;
  that's an inherent property of self-hosting, not a vulnerability.
- Issues that only manifest with `DEV_NO_AUTH=1` set. That flag disables
  authentication entirely and is documented as a development-only mode
  that must never be enabled on a deployed instance.

If you're unsure whether something qualifies, report it anyway and it will
be triaged.
