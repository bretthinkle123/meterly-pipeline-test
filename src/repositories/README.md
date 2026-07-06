# src/repositories/

## Purpose

Data access layer: SQL queries with parameterization, row-level scoping (by authenticated `api_key_id`), and minimal-field projections. All database I/O is encapsulated here.

## Modules

| File / Module | Responsibility |
|---|---|
| `events_repo.py` | `create_or_replay_event(api_key_id, customer_id, metric, quantity, idempotency_key, window_start)` — INSERT ... ON CONFLICT ... DO NOTHING (idempotency). Also `upsert_rollup()` (called only on insert success). |
| `usage_repo.py` | `read_usage(api_key_id, customer_id, metric, window_start)` — SELECT from `usage_rollup` scoped by `api_key_id`. Returns None if missing (caller handles zero conversion). |
| `api_keys_repo.py` | `get_by_key_id(key_id)` — SELECT from `api_keys` by public `key_id` (used by auth cache miss). Returns the full row (id, key_id, secret_hash, rate_limit_per_sec, revoked_at). |

## Relationships

**Public surface:**
- Imported by `src/services.{events_service, usage_service}` to execute queries.
- `api_keys_repo` is also imported by `src/auth.api_key` (credential lookup on cache miss).

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
- `create_or_replay_event` returns only `{event_id, customer_id, metric, quantity, window_start, idempotent_replay}` — not the full row (which includes `created_at`, `event_time`, `api_key_id`).
- `read_usage` returns only `{customer_id, metric, window_start, total_quantity, event_count}` — not internal bookkeeping (`updated_at`, `api_key_id`).
- `api_keys_repo.get_by_key_id` returns the full row (needed by auth for `secret_hash`, `rate_limit_per_sec`, `revoked_at`).

**Constraint enforcement:**
- `create_or_replay_event` relies on the `UNIQUE (api_key_id, idempotency_key)` constraint at the DB level to detect duplicates.
- If a duplicate key is encountered, PostgreSQL raises an `IntegrityError` (caught by service layer) → the service reads the original row instead.
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

**Async pattern:**
- All functions are async; they use `await session.execute(...)` for queries.
- They receive an `AsyncSession` that's already connected and in a transaction (setup by `src/db.session_context`).
- No explicit connection management in the repo; the session is provided by the caller.
