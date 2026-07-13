---
status: clean
ran_at: 2026-07-12T06:46:08Z
scope: diff
since_commit: 077014d47c98c898e077bc6b8ed0d47e5afb3872
critical_count: 0
warning_count: 1
fixed_count: 0
total_findings: 11
semgrep_findings: 0
osv_findings: 0
gitleaks_findings: 1
checkov_findings: null
trivy_findings: null
stride_mechanisms_verified: 5
stride_mechanisms_missing: 0
stride_new_threats: 0
asvs_reconciled: true
---

# Security report — enforce row-level security on the `events` table

Feature branch `feature/events-force-rls`. Scope: the working-tree change set
(diff since `077014d`) — 4 feature files. `.claude/settings.json` and `PROJECT.md`
also show modified in the tree but are pre-existing, out-of-feature changes and
were correctly excluded per `diff-scoping-conventions`.

Change set scanned:
- `alembic/versions/0005_force_rls_events.py` (NEW) — `ALTER TABLE events FORCE ROW LEVEL SECURITY`
- `tests/integration/test_events_rls_backstop.py` (NEW) — owner-role RLS efficacy test
- `tests/integration/test_quotas_list_delete.py` (modified) — migration-set assertion
- `alembic/README.md` (modified) — docs

## Result

**Clean.** No unmitigated High/critical finding remains. This change is a
schema/security migration whose purpose *is* the tenant-isolation backstop; it
tightens an existing data-layer authz boundary (closes the owner-bypass gap in the
`events_tenant_isolation` RLS policy from `0001`) and introduces no new attack
surface. Remediation performed: 8 Semgrep false positives on non-parameterizable
test-only DDL were triaged and suppressed through the committed `# nosemgrep`
channel so the CI merge gate stays green (they would otherwise block CI, which runs
`semgrep --config auto --severity ERROR --error` full-tree).

## Tools run this pass

| Tool | Config | Result | Liveness |
|---|---|---|---|
| Semgrep 1.169.0 (host-native) | `auto` + `p/secrets` + `p/owasp-top-ten` + `p/python`; 323 rules on 4 files | 8 findings pre-triage → **0 after committed suppressions** | stamped (native; Docker wrapper's container has no DNS — registry unreachable, so ran host-native per the ruleset-guide Linux path) |
| OSV Scanner (via `osv-scan.sh`) | `scan .` over `poetry.lock` (80 pkgs) | **0** after committed `osv-scanner.toml` filter (2 below-floor filtered) | stamped, exit 0 |
| Gitleaks (via `gitleaks-scan.sh`) | `dir .` full tree | 1 finding — **out-of-change-set false positive** (see below) | stamped, exit 1 |
| ast-grep (via `ast-grep-scan.sh`) | `rls-without-force` + `kdf-call` rules over the 3 code files (F-M4-5: diff touches SQL/migrations + async) | **clean, no hits** (advisory only) | stamped |
| asvs-sast.sh (Tier-1 deterministic) | JWT-none / pw-KDF / CSPRNG / cipher | **0 critical, 0 warning** | ran |
| lockfile-check.sh | supply-chain integrity | **clean** (no manifest/lock touched) | exit 0 |
| generate-sbom.sh | CycloneDX provenance | **65 components** written to `.pipeline/sbom.cdx.json` (non-gating) | ran |
| egress-check.sh | default-deny proxy log | no proxy provisioned — no-op | n/a |

**Not triggered (correctly):** Checkov (no `infra/` in the change set) and Trivy
config (no Dockerfile/image in the change set). **Trivy filesystem** (SB
belt-and-suspenders second SCA opinion) was **attempted and skipped** — its Docker
container cannot download the vulnerability DB (`mirror.gcr.io` DNS unreachable
in-container, same offline condition as the Semgrep Docker wrapper). OSV is the
primary SCA of record and ran cleanly host-native; SCA coverage is not degraded.

## Complete findings inventory

| # | source | id | severity | exploitable | location | disposition |
|---|---|---|---|---|---|---|
| 1 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:90 | false-positive (suppressed via committed `# nosemgrep`) |
| 2 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:92 | false-positive (suppressed via committed `# nosemgrep`) |
| 3 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:93 | false-positive (suppressed via committed `# nosemgrep`) |
| 4 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:94 | false-positive (suppressed via committed `# nosemgrep`) |
| 5 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:107 | false-positive (suppressed via committed `# nosemgrep`) |
| 6 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:110 | false-positive (suppressed via committed `# nosemgrep`) |
| 7 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:113 | false-positive (suppressed via committed `# nosemgrep`) |
| 8 | semgrep | avoid-sqlalchemy-text | ERROR | no | tests/integration/test_events_rls_backstop.py:116 | false-positive (suppressed via committed `# nosemgrep`) |
| 9 | gitleaks | generic-api-key | HIGH | no | tests/test_schemas_events.py:18 | false-positive, out-of-change-set (reported-only) |
| 10 | osv | GHSA-6w46-j5rx-g56g (pytest) | MEDIUM (CVSS 6.8) | no | poetry.lock (pytest, dev-only) | reported-only (committed `osv-scanner.toml` waiver; below 7.0 gate floor, dev-only) |
| 11 | manual-6a | test-role password literal | informational | no | tests/integration/test_events_rls_backstop.py:78 | reported-only (ephemeral testcontainer role password — not a real credential) |

`total_findings = 11`. `critical_count` after remediation = **0**. `warning_count = 1`
(the below-floor pytest CVE, #10). `fixed_count = 0` (no real vulnerability was
present to fix; #1–8 were triaged false positives, closed through the committed
suppression/waiver channel, which is triage — not a code fix).

## Fixes applied (triage/suppression — no exploitable defect)

**Semgrep `avoid-sqlalchemy-text` ×8 — committed `# nosemgrep` suppressions**
(`tests/integration/test_events_rls_backstop.py`).

- **What was flagged:** eight `text(f"…")` statements in the `events_owner_role`
  fixture — `CREATE ROLE`, `GRANT`/`REVOKE`, `ALTER TABLE events OWNER TO …`,
  `DROP ROLE` (lines 90/92/93/94/107/110/113/116).
- **Why it is a false positive (not exploitable):** the only interpolated values are
  a **UUID-derived role name** (`f"ev_rls_owner_{uuid.uuid4().hex[:8]}"`), a **static
  test literal password**, and a **DB-internal `pg_tables.tableowner`** identifier —
  none user- or attacker-controlled. These are role/grant/ownership **DDL**
  statements, where PostgreSQL cannot bind identifiers as parameters, run only against
  a disposable ephemeral testcontainer. No user input reaches the interpolation; there
  is no injection vector. This is the identical, already-shipped pattern in the sibling
  backstop tests (`test_usage_rollup_rls_backstop.py`, `test_quotas_rls_backstop.py`)
  and mirrors the accepted `# nosemgrep` triage in `src/repositories/usage_repo.py`.
- **Change:** appended `# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text`
  to each flagged line, plus a triage-rationale comment block above the fixture DDL.
  This is the committed, human-reviewable waiver channel (M4P-12/13) — a prose-only
  triage would leave CI's full-tree `semgrep --severity ERROR --error` red.
- **Confirmed by consolidated re-scan:** under CI's exact config
  (`--config auto --severity ERROR --error`) the file now reports **0 findings**; the
  full-pack re-scan over the whole change set reports **0 findings** (323 rules, 4 files).

## Could not remediate

None.

## Action required (human)

- **OSV `GHSA-6w46-j5rx-g56g` (pytest, CVSS 6.8, Medium)** — already covered by the
  committed, CI-honored `osv-scanner.toml` waiver: below the 7.0 deploy-gate floor and
  dev-only (`[tool.poetry.group.dev]`, never shipped in the production container). Safe
  version is **pytest 9.0.3**, but that is a major bump (8→9) with
  pytest-asyncio/-cov compatibility risk; tracked for a deliberate dev-dependency
  upgrade (revisit by the `ignoreUntil` 2026-10-09). No action needed for this feature.
- **Gitleaks `idem-key-abc123` (tests/test_schemas_events.py:18)** — a pre-existing
  idempotency-key **test fixture value**, committed in the first feature (`faabe9d`),
  **not part of this change set**. Clear false positive (a fixture literal, not a
  credential); no remediation taken here as it is out of scope for this feature's diff.

## AC6 reconciliation — RLS tenant-isolation backstop (delegated in acceptance.md)

The purpose of this change is the tenant-isolation backstop. Reconciling the
implemented control against the plan's STRIDE model and ASVS:

**Control implemented:** `ALTER TABLE events FORCE ROW LEVEL SECURITY` (migration
`0005`, upgrade), making the pre-existing `events_tenant_isolation` policy (from
`0001`: `USING (api_key_id = current_setting('app.current_api_key_id', true)::bigint)`)
bind the **table owner** `meterly_app`, which previously bypassed the non-`FORCE`
policy.

**Efficacy — verified, not merely present (U-02 "presence is not efficacy"):**
- **App role owns the table?** Yes — `meterly_app` owns `events` (migration job runs
  as `meterly_app`). This is the exact condition that made the plain-`ENABLE` policy
  inert. → `FORCE` is *required*, and is now applied.
- **Role class `FORCE` binds?** Yes — `meterly_app` is `NOBYPASSRLS`
  (`infra/modules/data/main.tf:89`, `CREATE ROLE meterly_app … NOBYPASSRLS`). An owner
  under `FORCE` with `NOBYPASSRLS` is fully bound by the policy (ASVS 13.2.2 met).
- **GUC enabling condition present on every runtime path?** Yes — all `events`
  readers/writers (`events_service`, `usage_service`, `usage_daily_service`,
  `usage_export_service`) execute inside `scoped_transaction`, which issues
  `SET LOCAL app.current_api_key_id` (`src/db/session.py`). So under `FORCE` every
  legitimate query still returns exactly the application-`api_key_id`-scoped rows —
  behavior-preserving — while a filter regression is now caught by the backstop.
- **Fail-closed?** Yes — with the GUC unset the policy returns zero rows (proven by
  `test_rls_denies_all_rows_to_owner_when_tenant_setting_is_unset`), not default-open.

This is exactly the class of inert-backstop defect the M3 series shipped; here the
`FORCE` toggle closes it, and the owner-role efficacy test is a genuine
fails-before-`0005` / passes-after-`0005` witness.

## 6d — STRIDE mechanism verification (against plan.md Step 2)

Plan has 6 STRIDE threats; Repudiation is explicitly out-of-scope (no audit-trail
change, no mechanism → skipped). The remaining 5 all carry a named mechanism, all
verified present **and effective**:

| # | Threat (sev) | Mechanism | Verified |
|---|---|---|---|
| 1 | Elevation of Privilege (H) | `ALTER TABLE events FORCE ROW LEVEL SECURITY` + `events_tenant_isolation` policy + GUC | ✓ `0005:upgrade` (FORCE) · `0001` (policy) · `src/db/session.py` (SET LOCAL). Owner now bound. |
| 2 | Information Disclosure (H) | Same FORCE + `USING` predicate confines reads | ✓ same evidence; efficacy test confines owner reads to own tenant |
| 3 | Tampering (M) | Same FORCE; `USING` reused as `WITH CHECK` confines cross-tenant writes | ✓ `0001` policy has no explicit `WITH CHECK` → PG reuses `USING`; FORCE binds writes too |
| 4 | Spoofing (L) | GUC set server-side from authenticated `principal.api_key_id`, never client input | ✓ `scoped_transaction(principal.api_key_id)` in `events_service.py:86` |
| 5 | Denial of Service (L) | Every path sets GUC via `scoped_transaction`; fail-closed-on-unset is correct | ✓ all readers/writers use `scoped_transaction`; metadata-only `ALTER`, no table rewrite |

`stride_mechanisms_verified = 5`, `stride_mechanisms_missing = 0`.

## 6f — STRIDE delta / attack-surface reconciliation

`surface-delta.md` declares **no new surface**, and the diff confirms it (diff is the
source of truth): no new HTTP route/CLI/consumer/webhook, no new outbound call or
dependency, no new subprocess/SSRF, no new table/column/field/cache/log sink, no new
privilege surface. The single effect is a **strict narrowing** of privilege (the app
owner is now bound by a predicate it previously bypassed). `stride_new_threats = 0`;
threat-model addendum is empty.

## 6g — ASVS 5.0.0 verification (ENFORCING)

Plan's `## ASVS Compliance`: triggered **V8** (Authorization / multi-tenant),
**V13** (Configuration / least-privilege DB role). All other chapters `n/a`
(data-layer-only change: no new HTTP surface → V4 n/a; no new inputs → V1/V2 n/a
beyond existing; no crypto/token change → V9/V11 n/a; no new logging → V16 unchanged).
L1+L2 verified universally on the triggered chapters; no in-scope L3.

| ASVS ID | Level | Requirement | Verified |
|---|---|---|---|
| 8.2.2 | L1 | Data-level access restricted (IDOR/BOLA) | ✓ `events_tenant_isolation` `USING` predicate + `FORCE` confine row access to the session tenant |
| 8.4.1 | L2 | Multi-tenant cross-tenant isolation for all DML | ✓ `FORCE` binds SELECT/INSERT/UPDATE/DELETE for the owner; efficacy test proves confinement |
| 13.2.2 | L2 | Least-privilege service account | ✓ `meterly_app` is `NOBYPASSRLS` (`infra/modules/data/main.tf:89`) — the role class `FORCE` binds |

`reqs_verified = 3`; `l1_l2_missing = []`; `l3_in_scope_missing = []`;
`doc_advisory = []`; `waivers = []`; **`asvs.reconciled = true`**. No unwaived
code/config item is unmet. (6g↔V8 overlaps the 6b RLS check — recorded once, cited by
ASVS ID here.)

## 6a–6c, 6e — other manual checks

- **6a Secrets:** only literal in the change set is `role_password = "test-owner-password"`
  (ephemeral testcontainer role password, identical to the shipped sibling test —
  confirmed **not** a real credential). `.env` is gitignored and untracked. No secrets
  in README/config. No finding.
- **6b Row-level security:** this change *is* the RLS strengthening; no query lacks a
  scoping predicate (see AC6 reconciliation). No finding.
- **6c Input/output sanitization:** no new HTTP inputs; migration and test use static
  or non-user-derived SQL; the `text()` DDL FPs handled above. No finding.
- **6e Log-sink safety:** no logging calls in the change set. No finding.

## Reconciliation / liveness

Every counted scanner has a THIS-pass execution stamp in `.pipeline/scan-log.jsonl`
with a non-empty artifact: `semgrep` (exit 0, `.pipeline/semgrep.json`, count 0),
`osv` (exit 0, `.pipeline/osv.json`, count 0). `scan_artifacts` sha256s match the
counted artifacts; per-tool counts (`semgrep_findings: 0`, `osv_findings: 0`) equal
`.results | length` of those artifacts. `checkov_findings`/`trivy_findings` are
`null` (not triggered / offline-skipped — disclosed, never defaulted to 0). ast-grep
findings are advisory and excluded from all counts per its contract.
