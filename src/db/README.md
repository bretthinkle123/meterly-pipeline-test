# src/db/

## Purpose

Database connectivity and session management: async SQLAlchemy session factory, connection pooling, transaction scope, and per-transaction RLS context setup.

## Modules

| File / Module | Responsibility |
|---|---|
| `session.py` | `session_context(api_key_id)` — async context manager that opens a transactional AsyncSession, sets the RLS context variable, and yields the session. Used by services to run queries with both transaction and RLS guarantees. |

## Relationships

**Public surface:**
- `session_context` is imported by `src/services.{events_service, usage_service}` to create transactional scopes for queries.

**Dependencies:**
- Imports `src.config.settings` for the `DATABASE_URL` (async connection string).
- Imports SQLAlchemy's async engine/session machinery.

**RLS mechanism:**
- At the start of each transaction (entered via `async with session_context(api_key_id)`), the context manager executes `SET LOCAL app.current_api_key_id = :api_key_id`.
- This PostgreSQL session variable is read by the RLS policy on every table access within the transaction.
- The `SET LOCAL` is per-transaction, so the context is automatically cleared at commit (no cleanup needed).
- This ensures that even if a repository query accidentally omits the `api_key_id` filter, RLS blocks the access (defense-in-depth).

## Notes

**Connection pooling:**
- The async engine uses a connection pool (default: 5-20 connections).
- At startup (lifespan in `src/main.py`), the pool is created and warmed.
- At shutdown, all connections are drained and closed (graceful shutdown).

**Transaction scope:**
- Every service call wraps its queries in a `session_context` block, ensuring all queries are in one transaction.
- For `create_event`, both the event insert and the rollup upsert happen in the same transaction (atomicity).
- For `read_usage`, the single SELECT is in its own transaction (isolation).

**Async pattern:**
- The session is async; all I/O operations (execute, fetch, etc.) are awaited.
- The context manager is async (uses `async with`).
- Services must be async to use the context manager (all routes in `src/api` are async).

**Database connection string:**
- Expected in the `DATABASE_URL` environment variable.
- Must be an async URL (e.g., `postgresql+asyncpg://...`).
- Credentials are fetched by `src/config.secrets.fetch_db_credentials()` from AWS Secrets Manager at startup (or from `DATABASE_URL` if that's set).
