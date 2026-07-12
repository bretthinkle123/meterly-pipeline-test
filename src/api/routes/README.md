# src/api/routes/

## Purpose

The HTTP boundary: one `APIRouter` module per resource, each wiring the shared
dependency chain (auth -> Tier-2 throttle -> optional admin-scope check ->
schema validation) to its corresponding service function. Mounted onto the
app in `src/main.py` via `app.include_router(...)`.

## Modules

| File / module | Responsibility |
|---|---|
| `health.py` | `GET /health` (liveness, no I/O) and `GET /health/ready` (readiness: DB reachability + Alembic migration-head match) via `liveness`/`readiness`. |
| `events.py` | `POST /v1/events` (`post_event`) — records a metered event, idempotent on `idempotency_key`. Auth -> Tier-2 throttle via the sibling `_require_authenticated_and_throttled` dependency, then `events_service.create_event`. |
| `usage.py` | `GET /v1/usage` (`get_usage_endpoint`) — the aggregated `{total_quantity, event_count}` for the caller's own `(customer_id, metric, window)` bucket. Same auth/throttle chain as `events.py`, its own sibling `_require_authenticated_and_throttled`. |
| `usage_export.py` | `GET /v1/usage/export` (`get_usage_export`) — streams the caller's own `usage_rollup` rows as RFC 4180 CSV. Two-phase: `prepare_export` (pre-flight 100,000-row cap check) then a `StreamingResponse` body from `stream_export_csv`. Own sibling `_require_authenticated_and_throttled` and explicit `_EXPORT_RESPONSES` OpenAPI metadata (a `StreamingResponse` has no `response_model` for FastAPI to introspect). |
| `usage_daily.py` | `GET /v1/usage/daily?date=YYYY-MM-DD` (`get_usage_daily_endpoint`) — the caller's own per-metric event counts for one UTC day, aggregated from `usage_rollup`. Customer-scoped, not admin-gated (same posture as `usage.py`); kept as its own module (not added to `usage.py`) so `GET /v1/usage`'s code path stays byte-for-byte unchanged. Own sibling `_require_authenticated_and_throttled`; delegates to `usage_daily_service.get_daily_usage`. |
| `quotas.py` | `/v1/quotas` — admin-scoped `PUT` (`put_quota`, create-or-replace), `GET` (`list_quotas_endpoint`, list the caller's caps), and `DELETE` (`delete_quota_endpoint`, remove one cap). All three share `_require_admin_and_throttled` (auth -> Tier-2 throttle -> `principal.scope == "admin"` check, 403 otherwise) — the only router here that adds a scope gate. |

## Relationships

**Public surface:** each module exports a module-level `router` (an
`APIRouter`), imported by `src/main.py` and mounted with `app.include_router(...)`.

**Dependency chain pattern:** every route composes its own sibling
`_require_authenticated_and_throttled` (or, for `quotas.py`,
`_require_admin_and_throttled`) function rather than importing a shared one —
each route's dependency chain stays independently readable, per the existing
per-route convention. All chains start with `require_api_key`
(`src/auth/__init__.py`) then `enforce_tier2_rate_limit`
(`src/auth/rate_limit.py`), keyed on the resulting `principal.api_key_id`.

**Scope:** every route here reads/writes only the authenticated caller's own
tenant data — validated request/query params (`src/api/schemas/`) go in,
service-layer calls (`src/services/`) do the work, and responses are the
service's return value directly (no route ever touches a repository or
constructs SQL itself).

**No behavior change to sibling routes:** `usage_daily.py` (like
`usage_export.py` before it) is a new sibling module specifically so adding
it cannot alter `events.py`/`usage.py`'s source — auditable in the diff.
