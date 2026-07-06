"""Shared pytest fixtures.

Unit tests (schemas, crypto, auth parsing, time flooring) need no external
services. Integration tests under `tests/integration/` spin up a real
PostgreSQL via `testcontainers` — the correctness guarantees the plan calls
out (ON CONFLICT concurrency, RLS, migration round-trips) are only real
against actual Postgres, not a mock.
"""

import os

# Ensure the app never tries to reach a real AWS account during tests: the
# secrets facade falls back to this env var when Secrets Manager is
# unreachable (see src/config/secrets.py).
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://meterly:meterly@localhost:5432/meterly_test")
