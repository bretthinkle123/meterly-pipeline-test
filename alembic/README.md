# alembic/

## Purpose

Database schema migrations using Alembic (SQLAlchemy's migration tool): version-controlled schema changes, automated upgrade/downgrade paths, and multi-environment deployment hooks.

## Modules

| File / Module | Responsibility |
|---|---|
| `versions/0001_create_api_keys_and_events.py` | Migration: create `api_keys` and `events` tables with constraints, indexes, and RLS policies (initial schema creation). Down path: drop both tables in FK-safe order. |
| `versions/0002_create_usage_rollup_backfill.py` | Migration: create `usage_rollup` table and backfill aggregates from existing `events` rows (expand + backfill pattern). Down path: drop the table and restore original `events` data if needed. |
| `versions/0003_create_quotas_and_api_key_scope.py` | Migration: create the `quotas` table (`PK (api_key_id, customer_id, metric)`, `CHECK (limit_per_window >= 1)`, RLS policy `quotas_tenant_isolation` + `FORCE ROW LEVEL SECURITY`) and add `api_keys.scope` (`'ingest'` default, `'admin'` elevated). Expand-only (add-table + add-column); down path drops `quotas` and the `scope` column. |
| `versions/0004_force_rls_usage_rollup.py` | Migration: security remediation — `ALTER TABLE usage_rollup FORCE ROW LEVEL SECURITY`, making the pre-existing `usage_rollup_tenant_isolation` policy (from `0002`) effective for the table owner (`meterly_app`), which otherwise bypasses non-FORCE RLS regardless of `NOBYPASSRLS`. Mirrors `0003`'s FORCE on `quotas`. Pure grant-semantics toggle (no DDL on rows/columns); down path is `NO FORCE ROW LEVEL SECURITY`. |
| `versions/0005_force_rls_events.py` | Migration: security remediation — `ALTER TABLE events FORCE ROW LEVEL SECURITY`, making the pre-existing `events_tenant_isolation` policy (from `0001`) effective for the table owner (`meterly_app`), which otherwise bypasses non-FORCE RLS regardless of `NOBYPASSRLS`. Mirrors `0004`'s FORCE on `usage_rollup`. Pure grant-semantics toggle (no DDL on rows/columns); down path is `NO FORCE ROW LEVEL SECURITY`. |
| `env.py` | Alembic runtime configuration: detects whether to run in "offline" (SQL script generation) or "online" (live DB) mode; configures the SQLAlchemy engine and metadata. |
| `script.py.mako` | Template for auto-generated migration files (not typically modified). |

## Relationships

**Public surface:**
- Run at deployment time: `alembic upgrade head` (applies all pending migrations).
- Run locally after pulling schema changes: `alembic upgrade head`.
- Migrations are immutable once committed (Alembic enforces this via the version table).

**Dependencies:**
- Migrations reference the SQLAlchemy models (not directly, but through the metadata).
- `env.py` imports SQLAlchemy and the config module to get `DATABASE_URL`.

**Schema evolution:**
- 0001 creates the initial schema (api_keys, events tables).
- 0002 extends the schema with the usage_rollup derived table and backfills it from events.
- 0003 creates the `quotas` table (per-tenant, per-customer, per-metric usage caps, RLS
  `FORCE`d so the app-role table owner can't bypass it) and expands `api_keys` with a
  `scope` column (`'ingest'` default, `'admin'` elevated), both in one expand-only revision.
- 0004 is a security-remediation grant-semantics toggle: `FORCE`s the `usage_rollup_tenant_isolation`
  RLS policy (already created in `0002`) so it also binds the table owner — no table/column/row
  change. It was added for the `usage-daily` feature as a sanctioned deviation from that
  feature's plan (which declared no new migration); see `.pipeline/pr-description.md` for
  the full disclosure.
- 0005 is the same grant-semantics toggle applied to `events`: `FORCE`s the
  `events_tenant_isolation` RLS policy (already created in `0001`) so it also binds the
  table owner — no table/column/row change. It is the dedicated remediation for this
  feature ("enforce row-level security on the events table"), closing the gap `0004`
  deliberately left open for `events`.
- Future migrations follow the same pattern: version number increments, only forward/backward are specified.

**Remediated:** the `events` table (`0001`) carried the same `ENABLE ROW LEVEL
SECURITY`-without-`FORCE` gap `usage_rollup` had — `meterly_app` owns `events` too, so its
`events_tenant_isolation` policy was equally inert for the app role. `0004` fixed only
`usage_rollup` (the table the `usage-daily` feature reads); `0005` now closes the `events`
gap that was tracked in the finding ledger.

## Notes

**Migration strategy:**
- **Create migrations (0001):** initial schema. Reversible as "schema + constraints" (rows survive, schema is recreated).
- **Expand migrations (0002):** add a new table, backfill from existing data, then contract-ready for a future cutover (e.g., if we want to sunset the events table and go read-only on the rollup).
- Create migrations are always reversible (down = drop). Backfill migrations are reversible if the data can be re-derived (as in 0002, usage_rollup is derived from events, so recreating it is deterministic).

**Reversibility (down paths):**
- 0001 down: drops events and api_keys (in FK-safe order: events first, then api_keys).
- 0002 down: drops usage_rollup (the table can be recreated by a future up run since the events data is intact).
- 0003 down: drops `quotas` (schema-only reversibility, no row-survival claim — a
  create-migration's down) and drops the `ck_api_keys_scope` constraint + `scope` column
  (pre-existing `api_keys` rows and their other columns survive; `scope` resets to its
  `'ingest'` default on a subsequent `up`, per the add-column expand/contract contract).
- 0004 down: `NO FORCE ROW LEVEL SECURITY` on `usage_rollup` — restores the pre-remediation
  owner-bypass binding; no rows or schema are touched, so `up -> down -> up` is a no-op on
  data and restores the policy's binding state identically.
- 0005 down: `NO FORCE ROW LEVEL SECURITY` on `events` — restores the pre-remediation
  owner-bypass binding; no rows or schema are touched, so `up -> down -> up` is a no-op on
  data and restores the policy's binding state identically.

**Testing:**
- `tests/integration/test_migrations.py` runs the migration roundtrip (up → down → up) against a live test database.
- Verifies that schema is restored and constraints re-enforced (0001) and that backfilled data survives and is re-derived identically (0002).
- These tests are part of the acceptance criteria (AC10, AC11).

**Deployment workflow:**
- Before deploying a new app version, the deployment pipeline runs `alembic upgrade head` on the target database.
- The app does not auto-migrate on startup; migrations are run explicitly (manual trigger or CI step).
- This gives operators control and ensures rollback-safety (if the app crashes after an upgrade, the migration is still committed, but the app can be rolled back to a prior version that understands the new schema).

**Local development:**
- `alembic upgrade head` syncs your local database schema to the latest migration.
- `alembic downgrade -1` rolls back the last migration (useful for testing down paths).
- `alembic current` shows the current revision.
- `alembic history` shows all revisions.
