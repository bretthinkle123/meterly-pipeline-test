# tests/

## Purpose

Unit and fast component tests (no live Postgres/Redis) for the `src/` package,
plus the `tests/integration/` suite (real Postgres/Redis via testcontainers,
and real HTTP/k6 load). Mirrors `src/`'s layering: schema/crypto/time-window
unit tests here, endpoint/migration/concurrency/perf tests under `integration/`.

## Modules

| File | Responsibility |
|---|---|
| `conftest.py` | Shared fixtures for the non-integration suite. |
| `test_app_smoke.py` | App-construction-level tests needing no DB/Redis: liveness, OpenAPI exposure, unauthenticated-denial/error-envelope shape. |
| `test_auth_api_key.py` | Split-token parsing and the in-process verification cache (Argon2id-vs-p95 cache mechanism). |
| `test_crypto.py` | `src/crypto` facade: Argon2id hash/verify, constant-time compare. |
| `test_dast_context_documented.py` | Asserts the DAST scanner auth context (header + token shape) is documented somewhere a DAST job's config can read it. |
| `test_db_session_isolation.py` | Regression guard for the `READ COMMITTED` isolation pin in `src/db/session.py` — the dependency the quota lock-then-read atomicity in `src/repositories/quotas_repo.py::read_tenant_quota_state_locked` relies on. |
| `test_dependency_pins.py` | Security-regression guard against a starlette CVE-carrying downgrade (pinned version stays at the patched release). |
| `test_logging_redaction.py` | A raw `customer_id` (or other sensitive field) must never reach the rendered log line regardless of call site. |
| `test_rate_limit_fail_open.py` | Regression test for the Tier-2 rate-limit logging facade in `src/auth/rate_limit.py`. |
| `test_schemas_events.py` | Validation-contract tests for `EventCreateRequest`. |
| `test_schemas_quotas.py` | Validation-contract tests for `QuotaPutRequest` (anchored `customer_id`/`metric` allowlists, `limit_per_window` bounds, injection rejection). |
| `test_schemas_usage.py` | Validation-contract tests for `UsageQueryParams`. |
| `test_slo_alarms_defined.py` | Static assertion that the declared SLO burn-rate + canary CloudWatch alarms exist in the Terraform source with the exact names `deploy.yml` expects. |
| `test_time_windows.py` | Tests for the UTC hour-flooring helper (`window_start_utc`) shared by `events_service`, `usage_service`, and `quota_service`. |
| `integration/` | Tests requiring a real Postgres/Redis (testcontainers) or a live HTTP process — see `tests/integration/README.md`. |

## Relationships

- Unit tests here import directly from `src.*` modules (schemas, crypto, auth, time
  windows) with no network/DB dependency — fast, run on every commit.
- `integration/` depends on Docker (testcontainers for Postgres/Redis, and for
  `test_perf_k6_load.py`, the `grafana/k6` image) and is slower; both suites are
  collected together by `pytest --cov=src --cov-branch` (per `CLAUDE.md`).
- Test IDs referenced in docstrings (`AC1`, `AC-PERF`, etc.) trace to
  `.pipeline/acceptance.md`'s acceptance criteria for the feature that introduced them.
