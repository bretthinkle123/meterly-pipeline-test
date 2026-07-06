# alembic/

## Purpose

Database schema migrations using Alembic (SQLAlchemy's migration tool): version-controlled schema changes, automated upgrade/downgrade paths, and multi-environment deployment hooks.

## Modules

| File / Module | Responsibility |
|---|---|
| `versions/0001_create_api_keys_and_events.py` | Migration: create `api_keys` and `events` tables with constraints, indexes, and RLS policies (initial schema creation). Down path: drop both tables in FK-safe order. |
| `versions/0002_create_usage_rollup_backfill.py` | Migration: create `usage_rollup` table and backfill aggregates from existing `events` rows (expand + backfill pattern). Down path: drop the table and restore original `events` data if needed. |
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
- Future migrations follow the same pattern: version number increments, only forward/backward are specified.

## Notes

**Migration strategy:**
- **Create migrations (0001):** initial schema. Reversible as "schema + constraints" (rows survive, schema is recreated).
- **Expand migrations (0002):** add a new table, backfill from existing data, then contract-ready for a future cutover (e.g., if we want to sunset the events table and go read-only on the rollup).
- Create migrations are always reversible (down = drop). Backfill migrations are reversible if the data can be re-derived (as in 0002, usage_rollup is derived from events, so recreating it is deterministic).

**Reversibility (down paths):**
- 0001 down: drops events and api_keys (in FK-safe order: events first, then api_keys).
- 0002 down: drops usage_rollup (the table can be recreated by a future up run since the events data is intact).

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
