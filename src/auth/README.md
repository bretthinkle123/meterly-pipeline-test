# src/auth/

## Purpose

Authentication and authorization layer: API key verification (split-token parsing and Argon2id credential check), in-process verification cache for performance, and Redis-backed rate limiting (Tier-1 pre-auth and Tier-2 post-auth, both fail-open).

## Modules

| File / Module | Responsibility |
|---|---|
| `__init__.py` | `require_api_key` FastAPI dependency guard: parses the `Authorization: Bearer` header, checks the verification cache, falls back to DB lookup + Argon2id verify on cache miss, sets the authenticated `api_key_id` in request state. |
| `api_key.py` | Split-token key parsing (`mtr_live_<key_id>_<secret>`), verification logic (Argon2id check), cache TTL management, revocation status check. |
| `dashboard_reader.py` | `get_dashboard_reader_principal()` — server-held dashboard-reader principal resolution and memoization. Fetches the `dashboard-reader` credential from AWS Secrets Manager via `get_secret`, resolves it to an `AuthenticatedPrincipal` via `verify_api_key`, and caches it in-process with a short TTL to avoid repeated Argon2id costs on the fan-out reads. Used by `dashboard_service` to drive the BFF data path. |
| `rate_limit.py` | Tier-1 (pre-auth, IP+route keyed) and Tier-2 (post-auth, api_key_id keyed) Redis token-bucket limiters; both fail open (log warning, allow request) on Redis connection error. |

## Relationships

**Public surface:**
- `require_api_key` is imported by `src/api.routes` as a FastAPI dependency (guard). When used, it populates `request.state.api_key_id` with the authenticated principal's ID.
- Tier-1 and Tier-2 rate limiters are called from `src/api.middleware` (Tier-1) and `src/api.routes.{events, usage}` (Tier-2).

**Dependencies:**
- `api_key.py` imports `src.crypto` for Argon2id verify and `src.db.session_context` for credential lookup on cache miss.
- `api_key.py` imports `src.logging.get_logger` for debug/warning logs (e.g., revocation, cache miss).
- `rate_limit.py` imports `src.config.settings` for rate limits (per-key `rate_limit_per_sec`) and `src.redis_client` for token-bucket state.
- Both modules are async (return coroutines for use in async context).

**Request flow:**
1. **Tier-1 throttle** (IP+route keyed) runs first in middleware, pre-auth. Checks Redis, fails open.
2. **`require_api_key`** parses and verifies the split token, caches verified keys.
3. **Tier-2 throttle** (api_key_id keyed) runs post-auth. Checks Redis with the now-authenticated principal's ID, fails open.
4. If any throttle rejects (rate limit exceeded), the handler never runs; 429 is returned.

## Notes

**Verification cache:**
- In-process TTL-bounded cache (default: 5 minutes) keyed on the public `key_id` (not the secret).
- On cache hit, constant-time comparison of the cached secret against the request secret (no Argon2id cost).
- On cache miss, DB lookup + Argon2id verify (expensive, ~100ms), then store in cache for future hits.
- Cache is per-process; horizontal scale-out (multiple Fargate tasks) means cache hit rates vary per task (not a correctness issue, just a performance variance).

**Revocation:**
- API key lookup checks the `revoked_at` column; a non-null value means the key is revoked.
- Revoked keys are rejected with 403 and not cached.
- Cache invalidation is not real-time (revocations take up to 5 minutes to propagate across all tasks).
- Recorded in `.pipeline/plan.md` as an accepted risk (noted for future key-revocation SLA if tighter bounds are needed).

**Rate limiting — fail-open behavior:**
- If Redis is unavailable, a Redis `ConnectionError` is caught and logged as a warning.
- The request is allowed through (traffic is not dropped).
- Recorded in `.pipeline/surface-delta.md` as an availability tradeoff: prefer dropping rate limits over dropping user requests.

**Rate limiting — Tier-1 pre-auth:**
- Keyed on IP + route (e.g., `ratelimit:tier1:POST:/v1/events:192.0.2.1`).
- Limits apply per route (different budgets can be configured per route if needed).
- Protects against flooding *before* we pay an Argon2id cost.
- Runs in middleware; all routes inherit the same policy.

**Rate limiting — Tier-2 post-auth:**
- Keyed on `api_key_id` (e.g., `ratelimit:tier2:123`).
- Limit is the key's `rate_limit_per_sec` column (default: 100 req/s).
- Per-customer isolation: two customers sharing one client IP each get independent buckets (discriminating test in `tests/integration/test_rate_limit.py`).
- Runs after auth in the route handler; only affects authenticated requests.
