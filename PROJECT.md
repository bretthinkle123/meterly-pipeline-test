# Meterly

## What this is
A usage-metering API: ingest metered events, aggregate them into per-customer/metric counters,
and query usage. A high-throughput ingestion primitive (think Stripe Metering / OpenMeter).

## First feature (this build only — keep scope here)
Two authenticated endpoints + their storage:
- POST /v1/events {customer_id, metric, quantity, idempotency_key} — record an event, increment the
  current-window counter; idempotent on idempotency_key (a duplicate key is a no-op, returns the
  original result).
- GET /v1/usage?customer_id=&metric=&window= — return aggregated usage for that customer/metric/window.
Storage: an append-only `events` table and an `usage_rollup` hourly-aggregate table, the latter added
by a SECOND migration that backfills from `events` (expand/contract).

## Explicitly out of scope for this build (later features)
Billing/invoice export, multi-tenant orgs & RBAC, a usage dashboard/UI, additional metric types,
webhooks. Do not build these now.

## Stack
- Cloud: AWS (ECS Fargate + RDS PostgreSQL + ALB), Terraform under infra/.
- Language/runtime: Python 3.12 (FastAPI), containerized (Docker).
- Data store: PostgreSQL (Alembic migrations).
- Auth: API keys (Argon2id-hashed at rest), per-key rate limiting.
- Packaging: container (justified — it deploys through the ECS canary path).
- Observability: CloudWatch + X-Ray + Sentry (release-tagged).

## Frontend design source
- Design source: none (API only).

## Non-functional / acceptance signals
- AC-PERF: POST /v1/events p95 < 50 ms measured UNDER 500 req/s sustained (not serial); throughput
  sustains >= 475 req/s. GET /v1/usage p95 < 100 ms.
- AC-CONCURRENCY: 50 concurrent POSTs with the SAME idempotency_key create exactly ONE event row.
- AC-DATA-PROTECTION: the stored API-key value is an Argon2id hash, never plaintext.
- AC-MIGRATION: the usage_rollup backfill migration round-trips (up→down→up) preserving seeded rows.
- AC-SLO: define availability 99.9% and ingest p95 < 50 ms as SLOs.

## What "done" means
- Smoke check passes; both endpoints return correct output for a sample input.
- Input validation in place; security report clean; ASVS L1/L2 met; data-protection gate satisfied.
- Tests pass at >= 85% lines; the perf/concurrency/migration criteria above are covered.
- Docs updated; PR description written.
