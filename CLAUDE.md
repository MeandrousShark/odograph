# Contributor instructions

Odograph is a self-hosted, single-user mileage tracker. This public repository
is the source for application changes. Internal planning and private operating
procedures are outside this checkout.

## Development workflow

- Create a short-lived branch from current `main`.
- Keep changes focused and include regression tests for behavior changes.
- Commit each logical change separately rather than batching unrelated work.
- Do not co-author commits or add generated-by attribution to a commit message
  or a pull request description. Keep both concise and limited to what changed
  and why.
- Open a pull request for review. Do not force-push `main` or release tags.
- Run the full suite before merge. Maintainers review the complete PR diff and
  merge the approved result to `main`.
- Releases are cut only from the exact reviewed merge commit on `main`.

## Test setup

Use Python 3.13 and the locked development requirements:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pytest
```

Database tests need a disposable Postgres/PostGIS instance. Use
`scripts/test_db.sh` and never point `TEST_DATABASE_URL` at real data. Tests
reset the public schema. Install the Gitleaks version pinned in
`.github/workflows/test.yml`, then run `python scripts/check_public_tree.py`
before opening a pull request.

## Load-bearing behavior

- Keep `detect()` pure and do not casually bump `DETECTOR_VERSION`.
- Human tags and manual trips remain protected from detector-owned changes.
- Keep OSRM match requests at `tidy=false` so dense valid fixes survive.
- Applied migrations are immutable. Add a new migration for a correction.
- Upgrades only validate the database role contract. A migration after 026
  that adds or replaces a public table, sequence or security-definer function
  must set `odograph_migrate` ownership, grant role rights and create account
  policies in its own SQL, enable and force row-level security on an
  account-owned table, and update `app/application_roles.py` to match;
  `tests/test_upgrade_contract_db.py` enforces this.
- Migrations run as a superuser or `BYPASSRLS` role; startup refuses any
  other. Under forced row-level security, data migrations must not rely on
  table ownership to see rows.
- Preserve CSRF, auth-before-body reads, exact OIDC identity matching,
  source-aware deletion, and graceful routing/geocoding fallback.
- Keep portable format compatibility and release image identity intact.

Use plain ASCII hyphens in new text. Do not add credentials, private hostnames,
or private operational details.
