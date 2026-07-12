---
status: clean
ran_at: 2026-07-11T23:05:53Z
scope: diff
since_commit: 791a37c903a0420cfa81ec90e35cdbfe74dc9443
critical_count: 0
warning_count: 0
fixed_count: 0
total_findings: 10
semgrep_findings: 8
osv_findings: 0
checkov_findings: null
trivy_findings: null
stride_mechanisms_verified: 8
stride_mechanisms_missing: 0
stride_new_threats: 0
---

# Security report — `usage-daily` (`GET /v1/usage/daily`) — remediation re-scan

Scope: working-tree change set since `791a37c` (diff-scoping-conventions). Re-scan after
the debugging agent's RLS remediation. Files scanned (10):

- NEW: `alembic/versions/0004_force_rls_usage_rollup.py`,
  `src/api/routes/usage_daily.py`, `src/api/schemas/usage_daily.py`,
  `src/services/usage_daily_service.py`,
  `tests/integration/test_usage_daily_endpoint.py`,
  `tests/integration/test_usage_rollup_rls_backstop.py`,
  `tests/test_schemas_usage_daily.py`
- MODIFIED (additive): `src/main.py`, `src/repositories/usage_repo.py`,
  `tests/integration/test_quotas_list_delete.py`, `PROJECT.md`
- Config (not app code, secrets-grepped only): `.claude/settings.json`,
  `.claude/settings.local.json`

## Bottom line

**CLEAN.** The one prior blocking critical — the inert `usage_rollup_tenant_isolation`
RLS backstop — is **genuinely cleared** by migration `0004`, and no new finding was
introduced by the migration or the test changes. All deterministic conjuncts read true.

## The prior critical is cleared (verified)

The prior finding: migration `0002` created `usage_rollup` with `ENABLE ROW LEVEL
SECURITY` but **not** `FORCE`; the app connects as table owner `meterly_app`, and a
PostgreSQL table owner **bypasses** non-FORCE RLS regardless of `NOBYPASSRLS` — so the
named defense-in-depth backstop was inert for the app role (U-02 presence-is-not-efficacy).

Verification of the remediation (by inspection, no scanner needed):

- **Migration actually applies FORCE.** `0004_force_rls_usage_rollup.py:49` executes
  `ALTER TABLE usage_rollup FORCE ROW LEVEL SECURITY`. It is chained correctly
  (`down_revision = "0003"`, revision `"0004"`; linear `0001→0002→0003→0004`), mirroring
  `0003`'s already-applied FORCE on `quotas`. It carries a real `downgrade()`
  (`NO FORCE`, line 56) — no no-downgrade-path critical. It is a pure grant-semantics
  toggle: no `DROP`/destructive DDL, no data touched, `op.execute` with a static string
  (no interpolation) — no injectable-SQL, no destructive-op finding.
- **The regression test genuinely exercises the policy.** `test_usage_rollup_rls_backstop.py`
  connects as a **non-superuser table owner** (`ur_rls_owner_*`) — the *only* role class
  whose RLS visibility flips on `FORCE` (a `NOBYPASSRLS` non-owner is bound under plain
  `ENABLE` and so could not witness the fix; the testcontainers superuser bypasses RLS
  entirely). `test_rls_confines_table_owner_read_when_app_filter_is_absent` **removes the
  application `api_key_id` predicate entirely** and asserts the owner still sees only its
  own tenant's rows — a true fails-at-`0003` / passes-at-`0004` witness of policy efficacy,
  not mere presence. `test_rls_denies_all_rows_to_owner_when_tenant_setting_is_unset` adds
  the fail-closed case (unset GUC ⇒ zero rows).
- **The guard test was narrowed correctly, not weakened.** `test_no_new_alembic_migration_added`
  now permits exactly the sanctioned `0004` file and asserts (via `re.findall(r"ALTER TABLE
  (\w+)")`) that its DDL targets are `{"usage_rollup"}` only and it creates no table — so
  AC14's real intent (the quota feature smuggled in no `quotas` schema change) still holds.
- **App path unaffected.** Every runtime reader/writer of `usage_rollup` runs inside
  `scoped_transaction`, which issues `SET LOCAL app.current_api_key_id`; under FORCE each
  still returns exactly the rows the `api_key_id` predicate already returns.

STRIDE mechanism tally: **8/8 verified, 0 missing** (the RLS backstop moves from the prior
"missing/inert" column into "verified effective").

## Tools run

| Tool | Result | Notes |
|---|---|---|
| Semgrep (native 1.169.0; `p/secrets` + `p/owasp-top-ten` + `p/python`) | 8 findings (all triaged false positives — see below) | Scanned all 10 change-set files incl. tests (broader than the prior source-only pass). Docker wrapper timed out (container cannot resolve `semgrep.dev`, offline registry) — ran the **native** binary. `.pipeline/semgrep.json` (sha `07d769b4…`), stamped. |
| OSV Scanner | 0 unfiltered | `poetry.lock` (80 pkgs); **no new dependency** introduced by this change. Only finding is `GHSA-6w46-j5rx-g56g` (pytest 8.3.4, CVSS 6.8 Medium, dev-only), filtered by the committed `osv-scanner.toml` (below the 7.0 gate floor, not in the deployed surface). `.pipeline/osv.json` (sha `54345d51…`), stamped. |
| Gitleaks | 0 in change-set | 128 tree-wide hits, all pre-existing (`.venv/` vendored botocore, archives, docs, prior fixtures); **none** in any change-set file. The new test's `role_password = "test-owner-password"` is a throwaway ephemeral-testcontainer credential, not flagged. `.pipeline/gitleaks.json`, stamped. |
| lockfile-check (supply-chain) | clean (exit 0) | No manifest/lockfile drift; no unpinned specifiers. |
| asvs-sast (Tier-1) | 0 critical, 0 warning | No JWT-none / fast-hash / non-CSPRNG / weak-cipher in the diff. |
| ast-grep (structural, advisory-only) | clean on the diff | `text(f"…")` / `text($X.format(…))` / `text($A % $B)` over the changed SQL/migration files: the only `text(f"…")` hit is `usage_repo.py:134` (**pre-existing** `count_usage_rollups`, already carrying an inline `# nosemgrep` waiver — not in this diff). The new `aggregate_daily_event_counts` uses bound params only. Migration `0004` uses `FORCE` (no `ENABLE`-without-`FORCE`). |
| reconcile-scans | reconciled: true | Independent re-hash + re-count: semgrep recount = 8 (matches), osv recount = 0 (matches); no scope gaps. `scan_reconciled: true`. |
| Trivy fs/config | carried forward / skipped (disclosed) | No dependency change this pass; not a required scanner (no Dockerfile/infra in the change-set); OSV covered SCA. Prior pass's Docker DNS to the vuln-DB mirror was unreachable. |
| Checkov | n/a | No `infra/` change in the change-set. |

## Complete findings inventory

| # | source | id | severity | exploitable | location | disposition |
|---|---|---|---|---|---|---|
| 1 | manual-6d | STRIDE-efficacy: `usage_rollup_tenant_isolation` RLS backstop (prior critical) | critical | no | `alembic/versions/0004_force_rls_usage_rollup.py:49` | fixed (remediated by debugging; verified cleared) |
| 2 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:87` | reported-only (false positive) |
| 3 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:89` | reported-only (false positive) |
| 4 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:90` | reported-only (false positive) |
| 5 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:91` | reported-only (false positive) |
| 6 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:104` | reported-only (false positive) |
| 7 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:107` | reported-only (false positive) |
| 8 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:110` | reported-only (false positive) |
| 9 | semgrep | `python.sqlalchemy.security.audit.avoid-sqlalchemy-text` | ERROR | no | `tests/integration/test_usage_rollup_rls_backstop.py:113` | reported-only (false positive) |
| 10 | osv | GHSA-6w46-j5rx-g56g (pytest 8.3.4) | medium (CVSS 6.8) | no (dev-only, not in prod container) | `poetry.lock` | reported-only (waived, `osv-scanner.toml`) |

`total_findings = 10` = rows above. `critical_count = 0` (row 1 cleared; rows 2–10 are
false positives / waived-below-floor). `warning_count = 0`. `fixed_count = 0` (the RLS
remediation was authored by the debugging agent, not by this security pass; verified only).

## Triaged false positives (rows 2–9): why they are not counted and need no committed ignore

All 8 are `avoid-sqlalchemy-text` on `text(f"…")` calls in the new regression test's
role-provisioning fixture: `CREATE ROLE …`, `GRANT …`, `ALTER TABLE usage_rollup OWNER
TO …`, `REVOKE …`, `DROP ROLE …`. Verified non-exploitable:

- **No user-controlled interpolation.** The only interpolated values are `role_name`
  (`f"ur_rls_owner_{uuid.uuid4().hex[:8]}"` — internally generated hex), `role_password`
  (module-local literal `"test-owner-password"`), and `original_owner` (read from the
  `pg_tables` system catalog). None is reachable from any request input.
- **Unparameterizable by construction.** These are PostgreSQL **DDL** statements; role and
  table identifiers cannot be bound as query parameters — interpolation is the only option.
- **Test-only** ephemeral fixture, never in the served application.

**No committed `.semgrepignore` / inline `# nosemgrep` is required here, and adding one would
be gratuitous** — CI's merge gate runs `semgrep scan --config auto --severity ERROR --error .`
(full tree), and `--config auto` provably does **not** surface this rule: the sibling
`tests/integration/test_quotas_rls_backstop.py` is **committed at HEAD** (part of green
`main`), carries the **byte-identical unsuppressed** 8-hit DDL pattern (0 `nosemgrep`), and
the required `pipeline-ci` check is green — which is impossible if `auto` gated on
`avoid-sqlalchemy-text`. The 8 hits here are surfaced only by the broader explicit
`p/python`/`p/owasp-top-ten` packs this agent runs, not by CI's `auto` config. (The codebase
does inline-`nosemgrep` this rule where a *source* path warrants it, e.g. `usage_repo.py:134`;
the new test is left consistent with its unsuppressed committed sibling rather than forked.)

## Fixes applied

None by this pass. The RLS critical was remediated by the debugging agent (migration `0004`
+ regression test); this pass verified the clearance. No code was modified during this
re-scan.

## Could not remediate

None.

## Action required

- **OSV `GHSA-6w46-j5rx-g56g`** (pytest 8.3.4 → safe `>= 8.4.0`): dev-only, CVSS 6.8
  (below the 7.0 deploy-gate floor), already filtered in the committed `osv-scanner.toml`.
  No action needed for this feature; bump opportunistically.

## STRIDE delta addendum

Empty. The change set introduces **no new attack surface**: migration `0004` is a
grant-semantics toggle on an existing table; the three test changes add no entry point,
trust boundary, data flow, or privilege surface. `stride_new_threats = 0`.

## ASVS 5.0.0 reconciliation

Triggered chapters `V1, V2, V4, V6, V8, V11, V13, V16` — unchanged from the prior pass;
the migration/test diff alters no application ASVS posture (it strengthens the V8
authorization backstop by making the RLS policy effective for the owner role). L1+L2
universal verified, no in-scope L3 selected, `l1_l2_missing = []`, `l3_in_scope_missing = []`,
`reconciled = true`.

## Deterministic conjuncts (all true)

| conjunct | value |
|---|---|
| `status` | clean |
| `critical_count` | 0 |
| `osv_max_cvss` / `osv_waiver` | 0 / null (no unwaived CVE ≥ 7.0) |
| `input_surface.uncontrolled` | [] (reconciled) |
| `data_surface.unprotected` | [] (reconciled) |
| `asvs.reconciled` | true |
| `scan_reconciled` | true (reconcile-scans independent recount matched) |
