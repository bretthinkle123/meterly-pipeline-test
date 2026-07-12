# src/services/

## Purpose

Business logic layer: event ingestion (with idempotency and counter atomicity), usage aggregation, and time-window calculations. Orchestrates repositories and enforces domain invariants.

## Modules

| File / Module | Responsibility |
|---|---|
| `events_service.py` | `create_event(principal, payload)` — inserts an event, checks the caller's quota (if any) on the winning-insert branch, and increments the hourly counter, all in one transaction; idempotent on `idempotency_key`. Raises `QuotaExceededError` (429 `quota_exceeded`) when `R + Q > L`, which rolls back the event insert. Returns the created/replayed event. |
| `usage_service.py` | `get_usage(principal, query)` — reads the aggregated counter for the `(customer_id, metric, window)` bucket in `query` (a `UsageQueryParams`); returns zeros if the bucket is empty (never 404). |
| `usage_export_service.py` | `prepare_export(principal, params)` — the pre-flight row-cap check: a `COUNT(*)` in its own transaction, raising a plain 422 `HTTPException` over `MAX_EXPORT_ROWS` (100,000) *before* any response byte; any other (non-cap) error propagates uncaught to the error-envelope boundary (fail-closed 500, no stream ever started). `stream_export_csv(principal, params)` — the `StreamingResponse` body generator: opens its own tenant-scoped transaction (Starlette pulls it only after the route returns), yields the header row first always (so an empty result is a 200 header-only CSV, never 404), then one CSV-encoded row per streamed rollup at constant memory (a reused `io.StringIO`, drained and reset per row), and logs one `usage.export` audit event in a `finally` block (fires even on a client disconnect or a mid-stream error). |
| `quota_service.py` | `upsert_tenant_quota(principal, payload)` — create-or-replaces the caller's cap for `(customer_id, metric)` in one transaction; maps the repository's insert-vs-replace signal to 201/200 and logs the `quota.upsert` audit event. `list_tenant_quotas(principal)` — opens a `scoped_transaction`, calls `quotas_repo.list_quotas`, and maps the rows to `list[QuotaResponse]`; an empty tenant gets an empty list (no dedicated business log — a plain read of the caller's own config, mirroring `usage_service.get_usage`). `delete_tenant_quota(principal, params)` — opens a `scoped_transaction`, calls `quotas_repo.delete_quota`; raises `HTTPException(404)` if no row matched, otherwise logs the `quota.delete` audit event. |
| `usage_daily_service.py` | `get_daily_usage(principal, query)` — validates `query.date` via `parse_daily_date`, opens a `scoped_transaction`, calls `usage_repo.aggregate_daily_event_counts`, logs the `usage.daily.read` event (`userId`, `action="read"`, `resource="usage_rollup"`, `date`, `metricCount` — never `customer_id`), and returns a `DailyUsageResponse` (empty `metrics` list, never 404, for a day with no events). Adds no error-swallowing try/except of its own — an unexpected repository failure propagates uncaught to the central `handle_unexpected_error` fail-closed boundary, the same posture as `usage_export_service`. |
| `time_windows.py` | `floor_to_hour_utc(timestamp: datetime)` — floors a timezone-aware timestamp to the hour (UTC); used by `events_service` and `usage_service`. |

## Relationships

**Public surface:**
- Imported by `src/api.routes.{events, usage, usage_export, usage_daily, quotas}` to execute the core business operations.

**Dependencies:**
- `events_service` imports `src/repositories.{events_repo, quotas_repo}` to insert the event, check the quota, and upsert the counter.
- `usage_service` imports `src/repositories.usage_repo` to read the rollup.
- `usage_export_service` imports `src/repositories.usage_repo` (`count_usage_rollups`, `stream_usage_rollups`) and `src/api/csv_export.py` (the column contract + formula-escape facade) — it owns the `csv.writer`/`io.StringIO` streaming mechanics, `csv_export.py` owns only the encoding/escaping contract.
- `usage_daily_service` imports `src/api/schemas/usage_daily.py` (`parse_daily_date`, `DailyMetricCount`, `DailyUsageQueryParams`, `DailyUsageResponse`) and `src/repositories.usage_repo.aggregate_daily_event_counts` to sum the day's hour-buckets per metric.
- `quota_service` imports `src/repositories.quotas_repo` to upsert, list, and delete quota rows.
- All services receive an authenticated `AuthenticatedPrincipal` from the route handler (set by `require_api_key` guard); `principal.scope` gates `PUT`/`GET`/`DELETE /v1/quotas` at the route layer (via the shared `_require_admin_and_throttled` dependency) before `quota_service` ever runs (the export and daily-usage reads have no scope gate — any authenticated key may read/export its own tenant's data).
- `time_windows` is imported by `events_service` and `usage_service` to floor timestamps.

**Transaction boundary:**
- `create_event` wraps the event insert, the quota check, and the rollup upsert in one PostgreSQL transaction (via `src/db/session_context`).
- This atomicity ensures that if the insert wins (no existing `idempotency_key`) and the quota check passes, the counter increments exactly once.
- If the insert loses (duplicate key), the quota is never consulted and neither the counter nor any side effect happens — a no-op.
- If the quota check rejects (`R + Q > L`), raising `QuotaExceededError` propagates out of the transaction context and rolls it back — the event insert is undone and the counter is never touched (no partial write).
- `upsert_tenant_quota` wraps the quota upsert in its own transaction; a single `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING (xmax = 0)` reports create-vs-replace with no second round-trip.
- `list_tenant_quotas` and `delete_tenant_quota` each wrap their query in their own `scoped_transaction`; an unexpected exception inside either propagates to the route's centralized `handle_unexpected_error` boundary and rolls the transaction back — for `delete_tenant_quota` this means a failed DELETE leaves the target row intact (fail-closed, no partial delete).

## Notes

**Idempotency guarantee:**
- The unique constraint `UNIQUE (api_key_id, idempotency_key)` on the `events` table is the source of truth.
- Exactly one of N concurrent identical requests will succeed; the rest get a duplicate-key constraint violation (caught, no exception raised to handler) and fall through to a replay branch.
- The replay branch reads the original event from the table and returns it, so the caller sees a consistent result across retries.
- Recorded in the plan as the **AC-CONCURRENCY** mechanism: 50 concurrent same-key POSTs produce exactly one row and 49 get the replay response (200 vs. 201).

**Counter atomicity:**
- The event insert and the rollup upsert are in one transaction, so both commit or both roll back.
- This means a counter never gets incremented without the event being persisted (no partial writes).
- Also means a duplicate event never increments the counter (no double-counting).
- The counter uses `INSERT ... ON CONFLICT ... DO UPDATE` with `total_quantity + EXCLUDED.total_quantity` to handle the increment (PostgreSQL's row-level conflict resolution, atomic at the SQL level).

**Time-window semantics:**
- A timestamp is floored to its hour (UTC) to get the `window_start`. E.g., `2026-07-06T14:35:42Z` → `2026-07-06T14:00:00Z`.
- The floor is done explicitly in Python (`ts.replace(minute=0, second=0, microsecond=0)`) rather than in the database, because PostgreSQL's `date_trunc('hour', ts)` is STABLE (depends on session timezone), not IMMUTABLE, so it cannot be used in a stored generated column.
- All windows are in UTC; clients must provide timezone-aware ISO-8601 timestamps on GET.

**Scoping by api_key_id:**
- Both services receive the authenticated `api_key_id` from the route guard (set in `request.state`).
- Both services pass it to the repository layer, which includes it in the WHERE clause (e.g., `WHERE api_key_id = :auth_key AND customer_id = :customer_id`).
- This is the primary RLS mechanism; the PostgreSQL RLS policy is the backstop.
