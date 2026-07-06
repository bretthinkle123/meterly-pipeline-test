# Meterly — system architecture (first feature)

## Overview

Meterly ingests metered usage events and serves aggregated per-customer/metric
counters. Two authenticated HTTP endpoints, one PostgreSQL database, one Redis
rate-limit store, running as a Docker container on ECS Fargate behind an ALB.

```
Client --HTTPS+API key--> ALB --> FastAPI (ECS Fargate, >=2 tasks)
                                     |-- Tier-1 (IP) / Tier-2 (api_key_id) throttle --> Redis
                                     |-- bound-parameter SQL, RLS-scoped -------------> RDS PostgreSQL
                                     |-- DB credential fetch ------------------------> Secrets Manager
                                     |-- traces/logs/errors -------------------------> CloudWatch/X-Ray/Sentry
```

## Request flow — `POST /v1/events`

1. Request-id/trace assigned, security headers queued (`src/logging/middleware.py`, `src/api/middleware.py`).
2. Tier-1 IP+route throttle (Redis token bucket; fails open on a Redis outage).
3. `require_api_key` — split-token parse, in-process verification-cache check,
   falling back to a DB lookup + Argon2id verify on a cache miss
   (`src/auth/__init__.py`, `src/auth/api_key.py`).
4. Tier-2 per-`api_key_id` throttle (`src/auth/rate_limit.py`).
5. Pydantic schema validation (`src/api/schemas/events.py`) — anchored
   allowlists, `extra='forbid'`.
6. `events_service.create_event` — one transaction:
   `INSERT ... ON CONFLICT (api_key_id, idempotency_key) DO NOTHING`, and only
   if a row was inserted, `INSERT ... ON CONFLICT ... DO UPDATE` the
   `usage_rollup` counter (`src/services/events_service.py`,
   `src/repositories/events_repo.py`).
7. Error-envelope boundary catches anything unhandled and returns the generic
   `{error:{code,message,requestId}}` shape (`src/api/errors.py`).

## Request flow — `GET /v1/usage`

Same auth/throttle/error stack; the service floors `window` to the UTC hour
and reads a single `usage_rollup` row scoped by the caller's `api_key_id`
(`src/services/usage_service.py`, `src/repositories/usage_repo.py`). A missing
bucket returns zeros with 200, never 404.

## Data model

- `api_keys` — the tenant/credential table (migration 0001). `secret_hash` is
  Argon2id; `key_id` is the public split-token lookup handle.
- `events` — append-only ingest log (migration 0001). `UNIQUE (api_key_id,
  idempotency_key)` is the idempotency guarantee; RLS policy
  `events_tenant_isolation` is the application-scoping backstop.
- `usage_rollup` — derived hourly aggregate (migration 0002, expand +
  backfill from `events`). Composite PK `(api_key_id, customer_id, metric,
  window_start)`; RLS policy `usage_rollup_tenant_isolation`.

## Auth

Split-token API keys (`mtr_live_<key_id>_<secret>`), Argon2id-hashed at rest.
An in-process, TTL-bounded verification cache (keyed on the public `key_id`,
guarded by a constant-time digest comparison) avoids paying the Argon2id cost
on every request while the durable store stays Argon2id-only. See
`src/auth/__init__.py` for the full tradeoff writeup and
`scripts/seed_api_key.py` for the only key-provisioning path (no HTTP
endpoint in this build's scope).

## Rate limiting

Two Redis token-bucket tiers (`src/auth/rate_limit.py`): Tier-1 pre-auth,
keyed on IP+route; Tier-2 post-auth, keyed on the authenticated
`api_key_id`. Both fail open (log a warning, allow the request) on a Redis
connection error rather than failing every request — an availability
tradeoff recorded in `.pipeline/surface-delta.md`.

## Observability

Structured JSON logs (structlog) to stdout -> CloudWatch, with a centralized
redaction processor (`src/logging/__init__.py`). OTel traces -> ADOT sidecar
-> X-Ray; Sentry for release-tagged error tracking with a `before_send`
PII/secret scrubber (`src/observability/`). SLO burn-rate alarms and the
minimum-three canary alarms are declared in `infra/modules/observability`.

## Infrastructure

Terraform under `infra/`: `modules/{network,compute,data,observability,edge}`
hold the real resources; `envs/{staging,prod}/main.tf` are the two
self-contained deploy roots (own provider + backend, since Terraform only
permits a backend configuration in a root module) that instantiate the same
modules at different scale. See each module's file header comments for the
specific resources and the security-baseline items they satisfy
(encryption at rest, private subnets, least-privilege IAM, no `BYPASSRLS`).

## Known deviations / accepted risks

See `.pipeline/surface-delta.md` for the rate-limit fail-open behavior and the
OTel/Sentry wiring-at-construction-time change vs. the plan's original
lifespan-hook placement. See `plan.md`'s "Open questions" section for the
accepted risks around key-revocation latency, `api_key_id`-as-tenant-scope,
and the Postgres app-role bootstrap's CI-network-reachability prerequisite.
