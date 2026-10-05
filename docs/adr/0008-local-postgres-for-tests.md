# 0008 Tests run against local Postgres, not testcontainers

## Context

The spec lists `testcontainers[postgres]` for tests. Much of the behaviour under test is SQL:
trigram resolution, partial unique indexes, `SKIP LOCKED`, advisory locks, JSONB undo data. It has
to run against real Postgres. A container per test session adds a Docker dependency and startup
time to every local run.

## Decision

Tests connect to a Postgres that is already running: `TEST_DATABASE_URL`, default
`postgresql+asyncpg://localhost/houseagent_test`. `tests/conftest.py` refuses to start unless the
database is named exactly `houseagent_test`, checks `current_database()` again before resetting,
then rebuilds the schema from migration `0001` once per session and truncates between tests. CI
provides the database as a GitHub Actions service container. `testcontainers` is not a dependency.

## Consequences

- The full suite runs in about ten seconds with no Docker requirement locally.
- A developer needs local Postgres 15+ with `pg_trgm` and `pgcrypto` available.
- The name guard is the only thing between the test suite and a real database, so it is checked
  twice and must not be loosened.
