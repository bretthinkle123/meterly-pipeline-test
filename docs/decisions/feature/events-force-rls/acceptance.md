---
feature: enforce row-level security on the events table (F5-S1 follow-up)
criteria_total: 6
delegated_criteria: [AC6]
---

# Acceptance criteria

Downstream definition-of-done for "FORCE ROW LEVEL SECURITY on `events`".
Implementation builds to this file; testing maps each ID to a test
(`criteria_covered`); the deploy gate requires `criteria_covered.total == 6` and
every delegated id present in `delegated_criteria` (only `security` is a valid
delegate). Criteria derive from PROJECT.md's brief and CLAUDE.md's "What done
means"; each traces to a section of `.pipeline/plan.md`.

Process/gate items from CLAUDE.md's "done means" that are owned by dedicated
stages rather than the testing map — **pipeline-ci green on the PR** (CI/delivery
merge gate; it *runs* the tests backing AC1–AC5), **docs updated for `alembic/`**
(documentation stage → the `alembic/README.md` edits in the plan's Files-affected),
and **PR description written** (delivery/documentation) — are tracked by those
stages and are **not** counted rows here, so the `criteria_covered` denominator
stays test/security-verifiable.

| ID | Criterion | File / layer | How verified |
|---|---|---|---|
| AC1 | Migration `0005_force_rls_events.py` exists, `revision="0005"`, `down_revision="0004"`; `upgrade()` runs `ALTER TABLE events FORCE ROW LEVEL SECURITY`; `downgrade()` runs `ALTER TABLE events NO FORCE ROW LEVEL SECURITY`. Mirrors `0004`'s shape; adds no policy (uses the existing `events_tenant_isolation` from `0001`) and no DDL on rows/columns. | `alembic/versions/0005_force_rls_events.py` (Data/migrations) | Updated `test_no_new_alembic_migration_added` (expected set now includes `0005`) + a DDL-inspection assertion that `0005`'s `ALTER TABLE` targets `== {"events"}` and the file contains `FORCE ROW LEVEL SECURITY`. |
| AC2 | Owner-role efficacy test proves tenant isolation: connecting as a **non-superuser table owner** with the app `api_key_id` filter absent, the session sees only its own tenant's `events` rows. Constructed to **FAIL against the pre-`FORCE` schema** (owner bypasses the non-`FORCE` policy → sees both tenants) and **PASS after** `0005` (owner bound → own tenant only). | `tests/integration/test_events_rls_backstop.py` (Testing) | `test_rls_confines_table_owner_read_when_app_filter_is_absent` — seeds two tenants, connects as the `events`-owner role, `set_config('app.current_api_key_id', tenant_a)`, unfiltered `SELECT api_key_id FROM events`, asserts `seen_ids == {tenant_a}`. |
| AC3 | Fail-closed: as the table owner with `app.current_api_key_id` never set, the RLS policy returns **zero** `events` rows (does not default open). | `tests/integration/test_events_rls_backstop.py` (Testing) | `test_rls_denies_all_rows_to_owner_when_tenant_setting_is_unset` — owner role, GUC unset, asserts `rows == []`. |
| AC4 | No endpoint/behavior change; all existing tests stay green, including `POST /v1/events`, usage-read endpoints, and app smoke. The AC14 migration-set assertion in `test_quotas_list_delete.py` is updated to include `0005` (its DDL-target guard on `0004` unchanged). | `tests/integration/test_quotas_list_delete.py` (updated), full suite (Backend: no change) | `pytest --cov=src --cov-branch` green at ≥ 85% coverage; `test_no_new_alembic_migration_added`, `test_events_endpoint.py`, `test_app_smoke.py`, and the usage/migration suites all pass. |
| AC5 | Migration reversibility (grant-semantics-toggle kind — no rows/schema touched): `up → down → up` restores the policy binding-state identically with no error (owner bound under `FORCE`, bypasses under `NO FORCE`). No row-survival claim applies (there are no rows to survive). | `alembic/versions/0005_force_rls_events.py` (Data/migrations) | Symmetric `NO FORCE` downgrade present + existing chain round-trip tests `tests/integration/test_migrations.py` (`downgrade base` / `upgrade head`) traverse `0005` down and up without Alembic error. |
| AC6 | Security report clean: the `events` RLS tenant-isolation backstop is verified effective (owner-bound `FORCE` RLS), with no unmitigated High/critical finding, and the implemented control reconciles against ASVS 8.2.2 / 8.4.1 / 13.2.2. | Security stage | Delegated to the security stage (`delegated: security`) — security 6b/6g reconciles the implemented RLS control (`FORCE` + `events_tenant_isolation`) against the threat model; not a testing-map assertion. |
