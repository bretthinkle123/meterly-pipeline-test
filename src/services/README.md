# src/services/

## Purpose

Business logic layer: event ingestion (with idempotency and counter atomicity), usage aggregation, and time-window calculations. Orchestrates repositories and enforces domain invariants.

## Modules

| File / Module | Responsibility |
|---|---|
| `events_service.py` | `create_event(principal, payload)` — inserts an event, checks the caller's quota (if any) on the winning-insert branch, and increments the hourly counter, all in one transaction; idempotent on `idempotency_key`. Raises `QuotaExceededError` (429 `quota_exceeded`) when `R + Q > L`, which rolls back the event insert. Returns the created/replayed event. |
| `usage_service.py` | `read_usage(api_key_id, customer_id, metric, window)` — reads the aggregated counter for a time bucket; returns zeros if the bucket is empty (never 404). |
| `quota_service.py` | `upsert_tenant_quota(principal, payload)` — create-or-replaces the caller's cap for `(customer_id, metric)` in one transaction; maps the repository's insert-vs-replace signal to 201/200 and logs the `quota.upsert` audit event. |
| `time_windows.py` | `window_start_utc(ts: datetime)` — floors a timezone-aware timestamp to the hour (UTC); used by all three services. |

## Relationships

**Public surface:**
- Imported by `src/api.routes.{events, usage, quotas}` to execute the core business operations.

**Dependencies:**
- `events_service` imports `src/repositories.{events_repo, quotas_repo}` to insert the event, check the quota, and upsert the counter.
- `usage_service` imports `src/repositories.usage_repo` to read the rollup.
- `quota_service` imports `src/repositories.quotas_repo` to upsert the cap.
- All three services receive an authenticated `AuthenticatedPrincipal` from the route handler (set by `require_api_key` guard); `principal.scope` gates `PUT /v1/quotas` at the route layer before `quota_service` ever runs.
- `time_windows` is imported by `events_service` and `usage_service` to floor timestamps.

**Transaction boundary:**
- `create_event` wraps the event insert, the quota check, and the rollup upsert in one PostgreSQL transaction (via `src/db/session_context`).
- This atomicity ensures that if the insert wins (no existing `idempotency_key`) and the quota check passes, the counter increments exactly once.
- If the insert loses (duplicate key), the quota is never consulted and neither the counter nor any side effect happens — a no-op.
- If the quota check rejects (`R + Q > L`), raising `QuotaExceededError` propagates out of the transaction context and rolls it back — the event insert is undone and the counter is never touched (no partial write).
- `upsert_tenant_quota` wraps the quota upsert in its own transaction; a single `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING (xmax = 0)` reports create-vs-replace with no second round-trip.

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
