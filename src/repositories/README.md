# src/repositories/

## Purpose

Data access layer: SQL queries with parameterization, row-level scoping (by authenticated `api_key_id`), and minimal-field projections. All database I/O is encapsulated here.

## Modules

| File / Module | Responsibility |
|---|---|
| `events_repo.py` | `insert_event_if_new(session, *, api_key_id, customer_id, metric, quantity, idempotency_key, window_start)` — `INSERT ... ON CONFLICT (api_key_id, idempotency_key) DO NOTHING`, returning `None` on a duplicate. `find_event_by_idempotency_key(session, *, api_key_id, idempotency_key)` — reads back the original row on a duplicate replay. `increment_usage_rollup(session, *, api_key_id, customer_id, metric, window_start, quantity)` — the rollup upsert, called only after `insert_event_if_new` returns a row. |
| `usage_repo.py` | `find_usage_rollup(api_key_id, customer_id, metric, window_start)` — SELECT from `usage_rollup` scoped by `api_key_id`. Returns None if missing (caller handles zero conversion). `count_usage_rollups(api_key_id, customer_id=None, metric=None, window_from=None, window_to=None)` — the export's pre-flight row-cap count over the same optional filters. `stream_usage_rollups(api_key_id, ..., limit)` — the export's server-side-cursor stream (`session.stream(...)`, yields `UsageRollupExportRecord`), ordered by a **fixed literal** `ORDER BY window_start, customer_id, metric` (never client-derived) with `LIMIT`. The count and stream queries share a `_export_filter_clause_and_params` helper so their WHERE clauses can never drift apart. |
| `api_keys_repo.py` | `find_active_key_by_key_id(key_id)` — SELECT from `api_keys` by public `key_id` (used by auth cache miss); returns the full row including `scope`. `create_api_key(..., scope="ingest")` — the only key-provisioning path. |
| `quotas_repo.py` | `upsert_quota(...)` — `INSERT ... ON CONFLICT (api_key_id, customer_id, metric) DO UPDATE ... RETURNING (xmax = 0) AS inserted`, reporting create-vs-replace in one round-trip. `read_tenant_quota_state_locked(...)` — the atomic check-then-decide: locks the quota row (`FOR UPDATE`), then reads the current-window rollup total as a **separate, fresh** statement (see *Notes* below for why). Returns `None` when no quota exists (unlimited, no lock taken). |

## Relationships

**Public surface:**
- Imported by `src/services.{events_service, usage_service, usage_export_service, quota_service}` to execute queries.
- `api_keys_repo` is also imported by `src/auth.api_key` (credential lookup on cache miss).
- `quotas_repo` is imported by both `quota_service` (the upsert) and `events_service` (the read-and-decide).

**Dependencies:**
- All functions are async; they receive an `AsyncSession` from the service layer (obtained via `src/db.session_context`).
- Queries use SQLAlchemy ORM with bound parameters (no string concatenation); all user input is parameterized.
- RLS is set per-transaction by `src/db.session_context` before the first query runs (sets the `app.current_api_key_id` PostgreSQL context variable).

**Row-level security:**
- Every query includes the authenticated `api_key_id` in the WHERE clause (primary application-layer control).
- E.g., `WHERE api_key_id = :auth_key AND customer_id = :customer_id` — no unscoped queries.
- PostgreSQL RLS policy (`USING (api_key_id = current_setting(...))`) is the backstop; the app role has no `BYPASSRLS`.
- If a query accidentally omits the `api_key_id` filter, RLS blocks it at the database level (defense-in-depth).

**Field projections (minimal surface):**
- `insert_event_if_new` / `find_event_by_idempotency_key` return an `EventRecord{id, api_key_id, customer_id, metric, quantity, idempotency_key, window_start}` — the service layer maps this to the minimal public response shape (not the full row is a service-layer concern, not a repo-layer projection).
- `find_usage_rollup` returns only `{total_quantity, event_count}` — not internal bookkeeping (`updated_at`, `api_key_id`).
- `stream_usage_rollups` yields `UsageRollupExportRecord{customer_id, metric, window_start, total_quantity}` — exactly the four exported columns, never `event_count`, `updated_at`, or `api_key_id`.
- `api_keys_repo.get_by_key_id` returns the full row (needed by auth for `secret_hash`, `rate_limit_per_sec`, `revoked_at`).

**Constraint enforcement:**
- `insert_event_if_new` relies on the `UNIQUE (api_key_id, idempotency_key)` constraint plus `ON CONFLICT DO NOTHING` (not a caught `IntegrityError`) — a duplicate insert returns no row, evaluated atomically by PostgreSQL. If no row is returned, the service calls `find_event_by_idempotency_key` to read the original row instead.
- `quantity > 0` and other domain constraints are enforced by CHECK constraints in the schema (verified at insert time).

## Notes

**ON CONFLICT pattern:**
- The idempotency mechanism uses `INSERT ... ON CONFLICT (api_key_id, idempotency_key) DO NOTHING RETURNING id, ...`.
- If the unique constraint is violated, the INSERT returns no rows.
- The service layer checks `if result` — if a row was returned, this request won the insert; if not, a duplicate is detected.

**RLS context setup:**
- Before any query, `src/db.session_context` sets `SET LOCAL app.current_api_key_id = :id` in the transaction.
- This context variable is read by the PostgreSQL RLS policy on every table access.
- The `SET LOCAL` is per-transaction (automatically cleared at commit), so no context leakage across requests.

**Quota lock-then-read pattern (why it's two statements, not one):**
- `read_tenant_quota_state_locked` deliberately issues the `FOR UPDATE` lock on `quotas` and the `usage_rollup` read as two separate statements, not a single `LEFT JOIN ... FOR UPDATE OF q` query.
- PostgreSQL's `FOR UPDATE` only forces a fresh re-check (`EvalPlanQual`) of the *locked* row itself when a blocked waiter unblocks after the holder commits — it does **not** force a fresh snapshot for another table read in the same statement, including a joined one.
- A single combined statement lets a waiter that queued before the holder committed read `usage_rollup` from its own stale pre-wait snapshot — verified empirically during implementation: every concurrent waiter read the same stale total and all got admitted, silently breaking the cap.
- Issuing the `usage_rollup` read as its own statement *after* the lock is acquired gives it a fresh READ COMMITTED snapshot that does include everything the previous holder just committed — this is what makes `current_total` accurate for every waiter, not just the first uncontended caller (`tests/integration/test_quota_concurrency.py`).

**Async pattern:**
- All functions are async; they use `await session.execute(...)` for queries.
- They receive an `AsyncSession` that's already connected and in a transaction (setup by `src/db.session_context`).
- No explicit connection management in the repo; the session is provided by the caller.
