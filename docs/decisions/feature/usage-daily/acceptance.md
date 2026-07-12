---
feature: "GET /v1/usage/daily — per-metric daily usage summary"
criteria_total: 15
delegated_criteria: []
input_sources:
  - "GET /v1/usage/daily (query param: date) — validation = AC3+AC4+AC5+AC6; rate-limit = AC13"
---

# Acceptance criteria — GET /v1/usage/daily

Each criterion has a stable id. Implementation builds to these; testing maps each id to a
named test (`criteria_covered`). No criterion is delegated (all are test-verifiable).

The single input source (the `GET /v1/usage/daily` route, `date` query param) carries its
two required input-surface controls: the **validation** criterion is the group
**AC3+AC4+AC5+AC6** (the `date` value contract + undeclared-param rejection); the
**rate-limit** criterion is **AC13**. Validation is not waived (untrusted internet input).

| ID | Criterion | File / layer | How verified |
|---|---|---|---|
| AC1 | Happy path: events across multiple metrics (and multiple `customer_id`s and multiple hour-buckets) on the given UTC day return 200 with correct per-metric summed `event_count`, ordered by `metric`. | `routes/usage_daily.py` → `services/usage_daily_service.py` → `usage_repo.aggregate_daily_event_counts` | Integration: seed events via `POST /v1/events` under two metrics / two customer_ids in the same UTC day; assert `GET /v1/usage/daily` returns the summed counts per metric in `metric` order (`test_usage_daily_endpoint.py::test_daily_counts_aggregate_per_metric`). |
| AC2 | Empty day (tenant has no events on that date) returns **200 with `metrics: []`**, never 404. | `services/usage_daily_service.py`, `schemas/usage_daily.py` | Integration: query a date with no events; assert 200 and `metrics == []` (`::test_empty_day_returns_200_empty_list`). |
| AC3 | **Missing** `date` returns **400** `bad_request` (not 422, not 200). | `schemas/usage_daily.py::parse_daily_date`, `api/errors.py` | Integration + unit: call with no `date`; assert 400 + envelope `code=="bad_request"` (`::test_missing_date_returns_400`; `test_schemas_usage_daily.py::test_missing_date_raises_400`). |
| AC4 | **Malformed** `date` returns **400** — covers `2026-7-1`, `2026/07/11`, `2026-13-40`, `2026-02-30`, `not-a-date`, empty string, trailing junk. | `schemas/usage_daily.py::parse_daily_date` | Unit (parametrized over the malformed forms) + one integration wire-check (`test_schemas_usage_daily.py::test_malformed_date_raises_400`; `test_usage_daily_endpoint.py::test_malformed_date_returns_400`). |
| AC5 | **Out-of-range** (well-formed) `date` — older than `today_utc−90d` or later than `today_utc+1d` — returns **400**. | `schemas/usage_daily.py::parse_daily_date` | Unit: a date 200 days in the past and a date 5 days in the future each raise 400 (`test_schemas_usage_daily.py::test_out_of_range_date_raises_400`). |
| AC6 | An **unknown/extra query param** (e.g. `?date=…&api_key_id=9`) returns **422** `validation_failed` (`extra="forbid"`), so no undeclared param is silently ignored. | `schemas/usage_daily.py::DailyUsageQueryParams` | Integration: assert 422 on an extra param (`test_usage_daily_endpoint.py::test_unknown_query_param_rejected_422`). |
| AC7 | **Cross-tenant isolation (IDOR):** tenant B querying the same date after tenant A ingests sees B's own result (empty), never A's counts. | `usage_repo.aggregate_daily_event_counts` (`api_key_id`-first) + RLS `usage_rollup_tenant_isolation` | Integration two-tenant test: A ingests, B queries same date, assert B's `metrics == []` (`::test_cross_tenant_cannot_see_others_counts`). |
| AC8 | **Auth required:** no `Authorization` header, or an invalid/malformed key, returns **401**. | `auth/require_api_key` (reused) via `routes/usage_daily.py` | Integration: request with no key and with a bogus key each return 401 (`::test_requires_api_key`). |
| AC9 | **Customer-scoped, NOT admin:** an ingest-scoped (non-admin) key calls the endpoint successfully (200); no admin scope is required. | `routes/usage_daily.py` (`require_api_key` only, no admin gate) | Integration: default (ingest) key gets 200 (`::test_ingest_key_allowed_not_admin_gated`). |
| AC10 | **UTC day-boundary correctness:** an event at `23:xxZ` of date D is counted for D; an event at `00:00Z` of D+1 is not (half-open `[day_start, day_end)`). | `schemas/usage_daily.py` day-bounds; `usage_repo.aggregate_daily_event_counts` | Integration: seed at the D-seam and the D+1 seam, assert only D's is counted (`::test_utc_day_boundary_is_half_open`); unit day-bounds test incl. month rollover + leap year (`test_schemas_usage_daily.py::test_day_bounds_utc`). |
| AC11 | **No regression to existing endpoints:** the existing `POST /v1/events` and `GET /v1/usage` suites pass unchanged, and the diff is additive-only (new sibling modules; `main.py` gains one `include_router`; `usage_repo.py` gains one new function). | `main.py`, `usage_repo.py`, existing test suite | Full existing suite green in pipeline-ci; a sanity integration assertion that `POST /v1/events` + `GET /v1/usage` still behave as before after the new route is mounted (`::test_existing_usage_endpoint_unchanged`). |
| AC12 | **Structured read log:** a `usage.daily.read` event is emitted with `userId=api_key_id`, `action="read"`, `resource="usage_rollup"`, `date`, and **no `customer_id` values** (no PII). | `services/usage_daily_service.py` | Integration/unit with a log capture: assert one `usage.daily.read` record with the required fields and no `customer_id` value present (`::test_emits_usage_daily_read_log`). |
| AC13 | **Rate-limit (input-surface):** the route is behind the **Tier-2 per-`api_key_id`** token-bucket throttle (post-auth, principal-keyed) plus the **Tier-1** IP+route edge throttle — reused, not reinvented. Key dimension = `api_key_id`; limit = `principal.rate_limit_per_sec`; window = token bucket. | `routes/usage_daily.py` (`enforce_tier2_rate_limit`), `main.py` (`Tier1EdgeThrottleMiddleware`) | The route's dependency chain includes `_require_authenticated_and_throttled` (auth→Tier-2), identical to the sibling routes; the existing two-principals-one-IP Tier-2 test (`tests/integration/test_rate_limit.py`) proves the principal-keyed mechanism. Assert the daily route resolves the Tier-2 dependency (`test_usage_daily_endpoint.py::test_route_behind_tier2_throttle`). |
| AC14 | **DAST-readiness:** the served OpenAPI schema includes `GET /v1/usage/daily` with its `DailyUsageResponse` model; the endpoint reuses the existing seeded DAST test user + `Bearer mtr_live_<key_id>_<secret>` auth context (no new auth context needed). | `routes/usage_daily.py` (`response_model=DailyUsageResponse`), plan.md (auth context) | Assert `/openapi.json` contains the path + response schema (`test_usage_daily_endpoint.py::test_openapi_documents_daily_route`); `test_dast_context_documented` continues to pass (plan documents `mtr_live` + `Bearer`). |
| AC15 | **Safe-error / fail-closed (ASVS-DET T2-2):** an unexpected internal error on `GET /v1/usage/daily` (e.g. a forced failure inside `aggregate_daily_event_counts`, simulating a DB/connection drop) returns the **generic 500 `internal`** envelope (`{code:"internal", message, requestId}`) — never a stack trace, SQL, exception type/message, `api_key_id`, or a partial body. The route/service add no error-swallowing try/except; the single central boundary owns the 500. | `api/errors.py` (`handle_unexpected_error` catch-all, reused), `services/usage_daily_service.py`, `routes/usage_daily.py` | Integration: monkeypatch `aggregate_daily_event_counts` to raise `RuntimeError`, call with a valid key through an `ASGITransport(raise_app_exceptions=False)` client, assert 500 + `error.code=="internal"` + `requestId` present + the raised message and `"Traceback"` absent from the body (`test_usage_daily_endpoint.py::test_daily_forced_repo_error_returns_generic_500`, mirroring the sibling `test_usage_export_endpoint.py::test_export_forced_pre_flight_count_error_returns_generic_500`). |

## Notes

- **Derivation:** `PROJECT.md` states the feature's cases explicitly (happy path,
  empty-day-200-not-404, 400 malformed/missing, no-regression, pipeline-ci green); the
  security/tenant-isolation, boundary, logging, rate-limit, DAST, and safe-error criteria
  are derived from the applicable skills (`api-edge-conventions`, `logging-conventions`,
  `dast-conventions`) and the STRIDE model in `plan.md`.
- **AC15 (safe-error) — parity with the sibling endpoint:** added in the single bounded
  revision pass to close the one `[material]` plan-audit flag (ASVS-DET T2-2). The
  `usage_export` sibling already carries this exact fail-closed assertion
  (`test_export_forced_pre_flight_count_error_returns_generic_500`); `/v1/usage/daily` has
  the same server-side error surface (a DB aggregate call) and now the same criterion.
  It is **test-verifiable, not delegated** — hence `delegated_criteria: []` is unchanged.
- **No perf-budget AC:** no perf-sensitive path is introduced — the query is a bounded,
  single-day aggregate over the pre-aggregated `usage_rollup`, staying within the API's
  existing p95 targets (no new budget invented, per the guidance to omit when nothing is
  perf-sensitive).
- **No data-protection / migration / audit-trail AC:** the endpoint stores nothing, adds
  no migration, and reads non-sensitive operational metering data (rationale in
  `plan.md` → *Data* and *Skills consulted*).
