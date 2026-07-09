# tests/integration/

## Purpose

Integration tests that exercise the app against real dependencies: Postgres and
Redis via testcontainers, a live out-of-process uvicorn for perf/load tests, and
Alembic migrations run against a scratch database. Complements the fast unit
suite in `tests/` (no live DB/Redis there).

## Modules

| File | Responsibility |
|---|---|
| `conftest.py` | Shared fixtures: testcontainer Postgres/Redis setup, the FastAPI test client, seeded `api_keys` rows. |
| `test_concurrency.py` | 50 concurrent same-`idempotency_key` POSTs create exactly one `events` row (idempotency under concurrency). |
| `test_data_protection.py` | `api_keys.secret_hash` is persisted as an Argon2id hash, never plaintext; the normal auth path still verifies end to end. |
| `test_events_endpoint.py` | `POST /v1/events` integration tests (create, replay, validation, auth). |
| `test_events_quota_enforcement.py` | `POST /v1/events` quota-check integration tests: unlimited-without-a-quota, under/at/over-limit, replay-bypasses-quota, mid-window quota change, throttle-vs-quota distinct error codes. |
| `test_health_readiness.py` | `/health/ready` against a live, migrated Postgres. |
| `test_migrations.py` | Migration round-trip (`0001`/`0002`) on a prod-shaped seeded dataset, own scratch testcontainer. |
| `test_perf_k6_load.py` | Sustained k6 load (via Docker) against a real out-of-process uvicorn; drives both the no-quota baseline and the quota-active scenario at equal worker budget for the AC20 relative p95 comparison. |
| `test_perf_smoke.py` | Smoke-sized perf check (no k6/Locust dependency) against a live uvicorn process. |
| `test_quota_concurrency.py` | N concurrent distinct-`idempotency_key` posts against a quota cap `L` never drive the rollup total above `L` (`FOR UPDATE` row-lock serialization proof). |
| `test_quota_migration.py` | Migration `0003` (`quotas` table + `api_keys.scope`) round-trip and constraint re-enforcement. |
| `test_quotas_endpoint.py` | `PUT /v1/quotas` integration tests: create/replace, auth, admin-scope enforcement, validation, tenant isolation, rate limiting, OpenAPI exposure. |
| `test_quotas_rls_backstop.py` | Adversarial proof that the `quotas_tenant_isolation` RLS policy alone (connecting as a `NOBYPASSRLS` role, primary `api_key_id` filter removed) still confines reads/updates to the caller's own tenant, and fails closed when the tenant context is unset. |
| `test_rate_limit.py` | Tier-2 (`api_key_id`-keyed) rate limiting: two principals on one IP get independent buckets; one principal across two IPs shares one bucket. |
| `test_seed_api_key_script.py` | `scripts/seed_api_key.py` provisions a key against a real database, including the `--admin` flag setting `scope='admin'` (default `scope='ingest'`). |
| `test_usage_endpoint.py` | `GET /v1/usage` integration tests (aggregation, tenant isolation). |
| `test_usage_export_endpoint.py` | `GET /v1/usage/export` integration tests: header-only-CSV on an empty result, per-filter narrowing, deterministic ordering, two-tenant isolation, row-cap 422 with no partial body, formula-injection escape end-to-end, response headers (content-type/filename/nosniff/no-store), streaming-not-buffered (multi-chunk) sanity, OpenAPI-schema exposure (AC19), the seeded DAST key calling the export via `test_export_seeded_dast_key_can_call_the_export` (AC20), and the fail-closed pre-flight 500 on a forced `COUNT` error (AC22). |
| `test_usage_export_perf.py` | Real-Postgres-testcontainer p95 timing sanity check for the export at the 100,000-row cap (AC16) — a 10-sample stable measurement asserting the human-confirmed `p95 <= 3,000ms` bound (not a k6 load test). |
| `k6/` | k6 load-test scripts driven by `test_perf_k6_load.py` — see `k6/README.md`. |

## Relationships

- All DB-backed tests share the Postgres/Redis testcontainer fixtures from `conftest.py`,
  except `test_migrations.py` and `test_quota_migration.py`, which own a scratch
  testcontainer independently (they drive the migration lifecycle itself: down to a
  prior revision and back up, which the shared fixture's already-migrated database
  can't support).
- `test_perf_k6_load.py` and `k6/load_events_quota.js` work as a pair: the Python test
  starts uvicorn, seeds fixture data (including quota rows for the quota-active run),
  invokes the k6 script via Docker, and asserts against the JSON summary it produces.
- Docstring AC references (`AC7`, `AC13`, `AC16`, `AC20`, etc.) trace to
  `.pipeline/acceptance.md`.
