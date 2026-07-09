# src/api/

## Purpose

HTTP API layer: FastAPI route handlers, request/response schemas (Pydantic v2), error envelope boundary, and edge-concern middleware (request ID, security headers, CORS, body-size guards).

## Modules

| File / Module | Responsibility |
|---|---|
| `routes/events.py` | `POST /v1/events` handler; calls `events_service.create_event`. |
| `routes/usage.py` | `GET /v1/usage` handler; calls `usage_service.read_usage`. |
| `routes/quotas.py` | `PUT /v1/quotas` handler; composes auth -> Tier-2 throttle -> an `admin`-scope gate (403 `forbidden` otherwise) -> `quota_service.upsert_tenant_quota`. |
| `routes/health.py` | `GET /health` (liveness, no dependencies) and `GET /health/ready` (readiness, DB + migration check). |
| `schemas/events.py` | Pydantic models for POST /v1/events request and response; `extra='forbid'` contract enforcement. |
| `schemas/usage.py` | Pydantic models for GET /v1/usage request (query parameters) and response. |
| `schemas/quotas.py` | Pydantic models for PUT /v1/quotas request (`customer_id`, `metric`, `limit_per_window`, `extra='forbid'`) and response (echo shape). |
| `middleware.py` | Request-ID / trace-ID assignment, security headers (HSTS, CSP, X-Frame-Options), CORS (allows GET/POST/PUT). |
| `errors.py` | Error-envelope boundary: catches all unhandled exceptions and returns `{error:{code,message,requestId}}` with no stack/secret leakage. `AppError` lets a raised exception carry an explicit `app_code` (e.g. `quota_exceeded`) that overrides the default status->code map — needed because 429 is shared by two distinct codes (`rate_limited` vs `quota_exceeded`). |

## Relationships

**Public surface:**
- Imported by `src/main.py` to register routes with FastAPI.
- Exposes `get_app()` function which returns the fully-configured FastAPI instance.

**Dependencies:**
- `middleware.py` and `errors.py` are registered once at app construction (middleware stack + exception handler).
- Each route handler uses `require_api_key` guard from `src/auth` to enforce authentication.
- Route handlers call `src/services.{events_service, usage_service}` to execute business logic.
- Schemas validate request bodies/parameters; malformed input raises Pydantic `ValidationError` → 422.
- Middleware and error boundary run for all routes, ensuring every response has proper headers and envelope shape.

**Middleware ordering (in `main.py`):**
1. Request-ID / trace (outermost) — sets correlation context for all downstream logging.
2. Security headers — applied to every response.
3. CORS — explicit allowlist from config.
4. Body-size guard — reject Content-Length > 8 KiB.
5. Tier-1 edge throttle (pre-auth) — IP+route keyed.
6. `require_api_key` guard — auth.
7. Tier-2 per-key throttle (post-auth) — api_key_id keyed.
8. Route handler (innermost) — wrapped by error-envelope boundary.

## Notes

**Contract enforcement:**
- Request bodies are validated against Pydantic schemas; `extra='forbid'` rejects unknown fields.
- All string fields are bounded length (e.g., `customer_id` ≤128, `metric` ≤64).
- `quantity` is a decimal > 0 (no zero or negative); `idempotency_key` is alphanumeric + underscore/dash.
- GET `/v1/usage` requires a timezone-aware ISO-8601 `window` parameter within 90 days past to 1 hour future (naive datetimes rejected).
- PUT `/v1/quotas`: `limit_per_window` is an integer `>= 1` (`<= 1e15`, BIGINT-safe); `customer_id`/`metric` reuse the same anchored allowlists as events; unknown fields (including a client-supplied `api_key_id`/`scope`) are rejected outright.

**Error handling:**
- Authentication failures (missing/invalid key) → 401/403 before handlers run.
- `PUT /v1/quotas` from a non-`admin`-scoped key → 403 `forbidden` (function-level authorization, checked in the route's composed dependency).
- Rate-limit exceeded (Tier-1 or Tier-2) → 429 `rate_limited` + Retry-After header.
- `POST /v1/events` over its quota → 429 `quota_exceeded` (distinct code, same status) + Retry-After to the next hour boundary; no event/rollup written (transaction rolled back).
- Schema validation failure → 422 with Pydantic error detail.
- Database errors (constraint violation, connection drop) → caught by error envelope → 500 (generic message, safe).
- Any unhandled exception → error envelope → 500, safe, no leak.

**Security headers (all responses):**
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` (production only; localhost omitted).
- `X-Content-Type-Options: nosniff` — disable MIME sniffing.
- `X-Frame-Options: DENY` — no embedding in iframes.
- `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` (API, not HTML).
