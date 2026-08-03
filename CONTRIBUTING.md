# Contributing

## Project scope

This is a single-user, self-hosted mileage tracker. It is deliberately
**not** a hosted multi-tenant service, and there are no plans to make it
one. Feature scope is maintainer-driven: proposals that push toward
multi-tenancy, hosted-SaaS operation, or general-purpose scope beyond
personal mileage tracking are unlikely to be accepted. There is a single
maintainer, and review happens on a best-effort basis. There's no fixed
cadence or SLA for issues or pull requests.

## Development setup

Use the project-local virtual environment, not system Python packages:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Most tests are pure functions (trip detector, report builders) and need no
database. If `pytest` cannot import `httpx` or `psycopg_pool`, the `.venv`
is stale or incomplete. Reinstall `requirements-dev.txt` before treating
that as a test failure. Note the naming split: the PyPI package is
`psycopg-pool`, but the Python import is `psycopg_pool`.

DB-backed tests (ingest, detector integration, routes) need a disposable
Postgres/PostGIS instance. Tests reset the `public` schema, so **never**
point `TEST_DATABASE_URL` at a database holding real data:

```sh
podman run -d --name mt_testdb \
  -e POSTGRES_DB=mileage -e POSTGRES_USER=mileage \
  -e POSTGRES_PASSWORD=testpw -p 55432:5432 \
  docker.io/postgis/postgis:16-3.4
TEST_DATABASE_URL=postgresql://mileage:testpw@127.0.0.1:55432/mileage \
  .venv/bin/pytest
```

Without `TEST_DATABASE_URL` set, DB-dependent tests skip rather than fail.

## Expectations for changes

- The full test suite should be green before you open a pull request.
- New behavior needs tests; a bug fix should include a regression test
  where practical.
- Match the existing code style and structure rather than introducing a
  new one.

## Code conventions

Comments explain *why*, not *what*. Names should already make the "what"
clear; comments are for non-obvious constraints, past bugs a piece of code
guards against, or alternatives that were considered and rejected. Avoid
comments that just restate the code in English.

New logic should separate pure "core" functions from thin I/O wrappers
(database access, HTTP calls, etc.) so the core logic is unit-testable
without a database or network connection. This is the difference between
"detects a trip from a list of points" (pure, easy to test with fixed
input) and "fetches points from the database, detects a trip, writes it
back" (I/O wrapper, thin, tested at the integration level).

## Invariants

These behaviors are load-bearing and easy to break by accident. Please
don't change them without discussing it first:

1. **Don't casually change the trip detector's version marker.** Bumping
   it triggers a full reprocess of every device's location history, which
   can delete trips that no longer match under the new logic. Deliberate,
   reviewed bumps only.
2. **Human edits always win over automatic tagging.** Once a trip's
   category has been set by a person, the automatic tagger must never
   overwrite it, including a manual "clear back to unclassified." Manually
   created trips are invisible to the automatic detector entirely.
3. **Road-snapping match requests keep `tidy=false`.** The alternative
   (`tidy=true`) causes the snapping service to silently drop dense, valid
   GPS fixes, which then falsely fails the match-quality gate on
   perfectly good tracks.
4. **Applied database migrations are immutable.** Never edit a migration
   that has already shipped. Fix mistakes with a new, sequentially
   numbered migration file instead.
5. **Routing and geocoding failures must degrade gracefully, never block
   startup.** If road-snapping or address lookup is unavailable or fails,
   the app should fall back to raw distance and raw coordinates rather
   than refusing to start or blocking a request indefinitely.

## Licensing

By contributing, you agree that your contributions are licensed under
AGPL-3.0, the same license as the rest of the project (inbound = outbound).
There is no contributor license agreement to sign.
