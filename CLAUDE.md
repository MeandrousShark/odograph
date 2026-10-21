# Contributor instructions

Odograph is a self-hosted mileage tracker for an administrator
and the members they invite. This public repository
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

Each test case, setup and teardown included, has a 60 second limit (not
enforced under `--pdb` once a phase fails); a longer case dumps every thread's
stack and stops the run.

Every test carries one tier marker. Put pure logic, rendering and validation
in `unit`; `db` is for SQL, row-level security, persistence, commits and
concurrency; `ops` is for scripts, workflows, Compose, the Dockerfile and
release tooling. A `_db` file suffix marks a test db, a module listed in
`_OPS_MODULE_STEMS` in `tests/conftest.py` marks it ops, and the rest are
unit, so a test that starts a subprocess goes in a listed module. Run an
exhaustive input matrix once, at the cheapest layer that catches the
regression. Review applies these rules to new tests; existing tests move only
when a change touches them.

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
- A migration from 045 on starts with `-- migration-class: <class>`, one of
  `additive` (adds objects or nullable/defaulted columns only), `rewrite`
  (changes existing rows or constraints, keeps supported data) or
  `destructive` (drops data or narrows its meaning; major versions only).
  `tests/test_migration_class.py` enforces it.
- Migrations run as a superuser or `BYPASSRLS` role; startup refuses any
  other. Under forced row-level security, data migrations must not rely on
  table ownership to see rows.
- Preserve CSRF, auth-before-body reads, exact OIDC identity matching,
  source-aware deletion, and graceful routing/geocoding fallback.
- Keep portable format compatibility and release image identity intact.

Use plain ASCII hyphens in new text. Do not add credentials, private hostnames,
or private operational details.
