# Contributing

Thanks for your interest in Community Pool. This page explains how to set up a
development environment, run the checks and send changes.

## Development environment

The pool is Python 3.12 and PostgreSQL. The test suite needs a disposable
PostgreSQL database whose name ends in `_test`; it drops and recreates the
`pool_v2` schema on every test.

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/v2/requirements.txt
.venv/bin/python -m playwright install --with-deps --only-shell chromium   # browser flow tests

docker run -d --name pool-test-db -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB=innopool_v2_test -p 127.0.0.1:5432:5432 postgres:18

export POOL_V2_TEST_DSN="host=127.0.0.1 port=5432 user=postgres dbname=innopool_v2_test"
export POOL_V2_WORKER_CHECKOUT=/path/to/community-pool-worker   # paired worker API tests
export POOL_V2_BROWSER_TESTS=1
.venv/bin/python -m unittest discover -s tests/v2 -v
```

Run a single file with `-p test_name.py`. The `pool-baseline` check in CI runs
the same suite against a clean database with the paired worker commit pinned
in `.github/workflows/ci.yml`.

## Making changes

- Branch from `main` and open a pull request against it. `main` is protected:
  the baseline check must pass and the branch must be up to date.
- Keep database migrations additive and numbered in sequence under
  `pool_manager/pool_v2/migrations/`. Financial tables are append-only and
  guarded by triggers; never edit or delete recorded rows, add a correction.
- Money is handled in integer base units (18 decimals) and every movement is a
  balanced journal. New spending paths must respect the recorded spending cap.
- Changes to the worker installer or the paired-release manifest need a new
  paired release, because the installer's checksum is recorded in the manifest.
- Add or extend tests next to the code you change. The suite is the
  specification for the money and protocol behaviour.
- Write documentation for operators and members in `docs/` in the same change.

## Commit messages and pull requests

Describe what changed and why, in plain language. Mention the evidence you
have: which tests you ran and against what. Pull requests that touch money,
authentication or the member protocol get a closer review.

## Reporting problems

Use GitHub issues for bugs and feature requests. For anything security
related, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
