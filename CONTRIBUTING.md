# Contributing

Odograph is a self-hosted, single-user mileage tracker. Contributions are
welcome through pull requests. Keep changes focused, explain behavior changes,
and add regression coverage where practical.

## Development setup

Use Python 3.13 and the locked development requirements:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
```

Run the unit suite with:

```sh
.venv/bin/pytest -m unit
```

Database tests require a disposable Postgres/PostGIS container. Start one with
the repository helper, export the printed `TEST_DATABASE_URL`, run the tests,
then clean it up:

```sh
eval "$(scripts/test_db.sh start contributor)"
.venv/bin/pytest -m db
scripts/test_db.sh cleanup contributor
```

Never point tests at real data. The database test suite resets its public
schema. Install the Gitleaks version pinned in `.github/workflows/test.yml`,
then run `python scripts/check_public_tree.py` before opening a pull request.

## Pull requests

Create a short-lived branch from current `main`, run the relevant tests during
development, and run the full suite before opening or merging a pull request.
Do not force-push `main` or release tags. Maintainers merge approved pull
requests to `main`; releases are cut only from the exact reviewed merge commit.
See [the release procedure](docs/releasing.md) for release-specific checks.

## Load-bearing behavior

- Keep `detect()` pure and do not casually bump `DETECTOR_VERSION`.
- Human tags and manual trips remain protected from detector-owned changes.
- Keep OSRM match requests at `tidy=false` so dense valid fixes survive.
- Applied migrations are immutable. Add a new migration for a correction.
- Upgrades only validate the database role contract. A migration after 026
  that adds or replaces a public table, sequence or security-definer function
  must set `odograph_migrate` ownership, grant role rights and create account
  policies in its own SQL, and update `app/application_roles.py` to match;
  `tests/test_upgrade_contract_db.py` enforces this.
- Preserve CSRF, auth-before-body reads, exact OIDC identity matching,
  source-aware deletion, and graceful routing/geocoding fallback.
- Keep portable format compatibility and release image identity intact.

Use plain ASCII hyphens in new text. Do not add credentials, private hostnames,
or environment-specific operational details.

## Licensing

Contributions are licensed under AGPL-3.0, the same license as the project.
