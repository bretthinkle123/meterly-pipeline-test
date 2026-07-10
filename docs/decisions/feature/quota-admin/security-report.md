---
status: clean
ran_at: 2026-07-10T01:55:58Z
scope: diff
since_commit: ef3fb3f686649d0ee0d37707dd61013b5e1d28f4
critical_count: 0
warning_count: 2
fixed_count: 1
total_findings: 3
semgrep_findings: 0
osv_findings: 2
checkov_findings: 0
trivy_findings: 0
stride_mechanisms_verified: 10
stride_mechanisms_missing: 0
stride_new_threats: 0
asvs_reconciled: true
---

# Security report — quota administration (GET /v1/quotas list + DELETE /v1/quotas)

Branch `feature/quota-admin`. Scope = diff against `HEAD` (ef3fb3f). **No critical findings.**
The implementation is well-hardened: parameterized SQL, anchored allowlist validation, admin-scope
gate, per-tenant `api_key_id` filtering with FORCE RLS backstop, minimal response projection, and a
fail-closed generic-500 error boundary.

## Tools run this pass

| Tool | Scope | Result |
|---|---|---|
| Semgrep (auto + p/secrets + p/owasp-top-ten + p/python) | 7 changed code/test files | 0 findings, 0 errors |
| Gitleaks (dir) | full tree | 0 in change set (128 out-of-scope, see below) |
| OSV Scanner | poetry.lock | 1 CVE (2 alias ids), dev-only, below gate floor |
| Trivy fs (vuln,secret,misconfig) | src/ | 0 findings |
| lockfile-check | change set | clean (no manifest/lockfile drift) |
| asvs-sast (Tier-1 deterministic) | change set | 0 critical |
| ast-grep (structural, advisory) | changed src | clean (see 6d) |
| generate-sbom | project | wrote .pipeline/sbom.cdx.json, 65 components |

Checkov not run: **no `infra/` `.tf` file changed** in this diff (the only untracked `infra/` entries
were the local `.terraform/` provider cache — see fix below). Trivy `config`/`image` not run: no
Dockerfile or built image in the change set. Both legitimately omitted from `scan_artifacts`.

## Change-set diff-scope

Tracked: `PROJECT.md`, `.gitignore` (fixed this pass), `src/api/middleware.py`,
`src/api/routes/quotas.py`, `src/api/schemas/quotas.py`, `src/repositories/quotas_repo.py`,
`src/services/quota_service.py`, `tests/test_schemas_quotas.py`. Untracked:
`tests/integration/test_quotas_list_delete.py`, `infra/.terraform.lock.hcl` (the provider lock —
legitimately committable, kept).

## Complete findings inventory

| # | source | id | severity | exploitable | location | disposition |
|---|---|---|---|---|---|---|
| 1 | osv | CVE-2025-71176 (PYSEC-2026-1845 / GHSA-6w46-j5rx-g56g) | Medium (CVSS ~6.8) | no | poetry.lock — pytest 8.3.4 | action-required |
| 2 | manual-6a | terraform-provider-binaries-not-gitignored | Low | no | .gitignore / infra/.terraform/ | fixed |
| 3 | manual-6d | tier1-throttle-ip-keying-behind-alb (efficacy, pre-existing, out of diff-scope) | Low | no | src/api/middleware.py:118 | reported-only |

`total_findings: 3`. `osv_findings: 2` in the status JSON is the reconcile convention (unique alias
id count) for the single logical pytest CVE (row 1).

## Fixes applied

**[fixed] Row 2 — Terraform provider binaries were not gitignored.** `.gitignore` lacked a
`.terraform/` entry, so `git ls-files --others` pulled ~108 MB of vendored HashiCorp provider
executables (`terraform-provider-aws_v5.100.0_x5.exe`, null, random) plus the module cache into the
diff-scoped change set — a repo-hygiene / minor supply-chain surface (large signed third-party
binaries entering the tree the pipeline hashes and would commit).
- **Before:** `.gitignore` lines 17–18 = `*.tfstate` / `*.tfvars` only.
- **After:** added `.terraform/` (line 19). Verified `git check-ignore` now excludes the provider
  `.exe`s; the change set drops from 8 untracked entries to 2 (test file + `.terraform.lock.hcl`).
- The provider **lock** `infra/.terraform.lock.hcl` is deliberately **not** ignored (pinning provider
  versions/hashes is good supply-chain practice) — it stays committable.

## Action required (report-only, not auto-fixed)

**Row 1 — pytest 8.3.4 has a vulnerable tmpdir-handling CVE (CVE-2025-71176).**
- Package: `pytest 8.3.4` (PyPI), a **dev/test-only** dependency (not on the request path, not in the
  runtime container).
- CVSS 3.1: `AV:L/AC:L/PR:N/UI:N/S:C/C:L/I:L/A:L` ≈ **6.8 (Medium)** — **below** the deploy gate's
  ≥7.0 High/Critical floor, so it does **not** block. `osv_max_cvss: 6.8`, `osv_waiver: null`.
- **Safe version: upgrade `pytest` to `>= 9.0.3`.** Per policy, dependency manifests/lockfiles are not
  modified here; a maintainer bumps `pyproject.toml`/`poetry.lock`. Not routed to debugging (below the
  ≥7.0 gate floor); the human decides.

## Could not remediate

None. No finding required a design decision or escalation.

## Manual security checks (steps 6a–6g)

**6a — Secrets / API-key exposure:** clean. No hardcoded credential literal in any changed file
(grep + Semgrep p/secrets + Gitleaks all clean on the change set). `.env` is gitignored and untracked;
`PROJECT.md` diff is only the feature description; no secret in config. The server sets `api_key_id`
from the authenticated principal and never accepts a client-supplied `scope`/`api_key_id`
(`extra='forbid'` on both request schemas — mass-assignment defense).

**6b — Row-level security:** clean. Every `quotas` query is scoped by `api_key_id` first —
`list_quotas` (`WHERE api_key_id = :api_key_id`), `delete_quota`
(`WHERE api_key_id = :api_key_id AND customer_id = … AND metric = …`), `upsert_quota`
(`ON CONFLICT (api_key_id, customer_id, metric)`). Backstop verified: migration `0003` sets
`ALTER TABLE quotas FORCE ROW LEVEL SECURITY` + policy `quotas_tenant_isolation`, and
`scoped_transaction` sets `app.current_api_key_id` via `SET LOCAL set_config(...)`
(`src/db/session.py:58-85`). Efficacy-correct (U-02): the app role owns the table, so FORCE is
required and present — a non-FORCE policy would be inert.

**6c — Input / output sanitization:** clean.
- DELETE query params validated by `QuotaDeleteParams` (anchored `constr` allowlists
  `^[A-Za-z0-9_.:-]{1,128/64}$`, ReDoS-safe, `extra='forbid'`) before the handler — an injection
  payload (`' OR 1=1--`) is rejected at the boundary (422), never reaching the sink (AC9).
- GET accepts no body/query/path parameter — no injectable input.
- All new SQL uses SQLAlchemy `text()` with bound parameters only — no string interpolation. ast-grep
  structural check for `session.execute(text(f"…"))` in the repo: **0 hits** (confirms AC15).
- Output: `response_model=list[QuotaResponse]` projects only `{customer_id, metric, limit_per_window}`
  (no `api_key_id`/timestamps). JSON API, no HTML/JS/URL sink — no output-encoding exposure.

**6d — STRIDE mechanism verification (10/10 present + efficacy-correct, 0 missing):**
1. Spoofing → `_require_admin_and_throttled` = `require_api_key` (Argon2id) + `scope=='admin'`→403 — `routes/quotas.py:35-52`. ✓
2. Tampering/SQLi → anchored `constr` + parameterized `text()` — `schemas/quotas.py:15-16`, `quotas_repo.py:205-214`. ✓
3. Tampering/mass-assignment → `ConfigDict(extra='forbid')`, server-set `api_key_id` — `schemas/quotas.py:60`. ✓
4. Repudiation → `quota.delete` INFO (userId, action, customer_id[redacted], metric, requestId) — `quota_service.py:116-122`. ✓
5. Info-disclosure (GET) → minimal `response_model` + `api_key_id`-scoped SELECT + FORCE RLS — `routes/quotas.py:68`, `quotas_repo.py:161-187`. ✓
6. Info-disclosure (safe-error) → `handle_unexpected_error` returns generic `{code:internal}` 500, detail logged server-side only; `scoped_transaction` rolls back (fail-closed, no partial DELETE) — `errors.py:97-118`, `db/session.py:65`. ✓ (AC19)
7. DoS (flood) → Tier-1 IP throttle + Tier-2 per-`api_key_id` bucket — `middleware.py`, `rate_limit.py:136-154`. ✓ (efficacy: see below)
8. EoP (function-level) → `scope=='admin'`→403, `quota.forbidden` logged — `routes/quotas.py:44-51`. ✓
9. EoP (IDOR/BOLA) → `api_key_id` filter + FORCE RLS → cross-tenant DELETE matches 0 rows → 404 — `quotas_repo.py`, `db/session.py`, `0003`. ✓
10. Tampering (`usage_rollup` integrity) → `delete_quota` issues **only** a DELETE on `quotas`, never touches `usage_rollup` — `quotas_repo.py:205-214`. ✓

Efficacy questions (U-02) answered:
- **Async runtime:** no CPU-hard KDF or blocking sync SDK call on the event loop in the changed handlers.
  ast-grep for `argon2.*`/`bcrypt.*` in the changed src: **0 hits**; the one `boto3.client` in the tree
  is `src/config/secrets.py` (pre-existing, startup-time, not in this change set). All handler DB I/O
  goes through the async session. ✓
- **DB privilege:** FORCE RLS present + app-role owns the table (verified above). ✓
- **Contract drift:** the safe-error scrubber (`handle_unexpected_error`) is a global catch-all that
  covers the two new DB-I/O paths (AC19); response body carries no stack/SQL/type/path. ✓
- **Topology (the one efficacy warning, row 3):** the Tier-1 pre-auth throttle keys its bucket on
  `request.client.host` (`middleware.py:118`) and no `--proxy-headers`/`forwarded-allow-ips` trust is
  configured, so **behind the ALB** Tier-1 would bucket per-ALB-node rather than per-real-client. This
  is **pre-existing** middleware **not introduced by this diff** (the only middleware change here is
  adding `"DELETE"` to CORS `allow_methods`), and for these admin routes the **effective** anti-abuse
  control is **Tier-2**, which keys on `str(principal.api_key_id)` (`rate_limit.py:153`) — topology-
  independent and proven per-principal by AC12. The LB readiness path `/health` is correctly exempt
  from Tier-1 (`_TIER1_EXEMPT_PATHS`). Recorded as a Low warning for the pre-existing Tier-1 IP-keying;
  not a blocker for this feature.

**6e — Log-sink safety:** clean. `quota.delete` / `quota.forbidden` use the structlog facade with
discrete fields; `customer_id` is facade-redacted (AC16) and the allowlist pattern precludes
newline/CR injection into log lines. No request body, password, token, or unredacted PII logged. The
error handler logs `error.message`/`error.type` **server-side only** (never in the response) — acceptable.

**6f — STRIDE delta / attack-surface reconciliation:** `stride_new_threats: 0`. The surface-delta hint
matches the diff exactly. New surfaces (GET /v1/quotas, DELETE /v1/quotas, the CORS `DELETE` method
addition) are each already covered by the plan's threat model — the plan was authored for this feature.
The CORS `DELETE` addition has no live effect (empty origin allowlist, server-to-server API). No new
outbound call, subprocess, deserialization, table, or un-modeled entry point. Addendum below is empty.

**6g — ASVS 5.0.0 reconciliation (AC18, delegated to security):** `reconciled: true`.
- **Triggered chapters** (from plan `## ASVS Compliance` + diff): V1, V2, V4, V6, V8, V12, V13, V14, V16.
- **L1 + L2 (universal)** verified on every triggered chapter; **in-scope L3** = V8.2.x object-level
  authorization (IDOR/BOLA), verified via the `api_key_id` filter + FORCE RLS + the cross-tenant-
  DELETE→404 test (AC8). Key requirement evidence: 1.2.4 (parameterized DELETE SQL), 2.2.1/2.4.1
  (validation + anti-automation Tier-2), 6.2.x (Argon2id API-key auth, reused), 8.2.1/8.2.2/8.2.3/8.4.1
  (admin gate + tenant isolation on read/delete), 14.3.x (minimal GET response, no sensitive store),
  15.3.1/15.3.3 (minimal field set + mass-assignment `extra='forbid'`), 16.2.1/16.5.1/16.5.3
  (`quota.delete` audit + generic 404/500 fail-closed envelope). `reqs_verified: 14`.
- **`n/a`** (correctly, per plan): V3, V5, V7, V9, V10, V11, V15, V17 — server-to-server API, no
  browser/cookies, no files, no sessions/JWT/OAuth, no new crypto, no new concurrency-integrity
  guarantee, no WebRTC.
- **No unwaived L1/L2/in-scope-L3 code/config item is unmet.** No `doc_advisory` items surfaced. No
  human waiver in `.pipeline/waivers.json` (file absent) — none needed. `l1_l2_missing: []`,
  `l3_in_scope_missing: []`.

## Input-surface reconciliation

`reconciled: true`. Two implemented input sources, both controlled: `DELETE /v1/quotas` has a
validation contract (`QuotaDeleteParams`) **and** a Tier-2 rate-limit (AC12); `GET /v1/quotas` accepts
no input parameter and has the Tier-2 rate-limit. `declared: 2`, `implemented: 2`, `uncontrolled: []`.

## Data-surface reconciliation

`reconciled: true`. The feature stores **no new field** — GET reads, DELETE removes existing rows. The
listed/deleted columns (`customer_id`, `metric`, `limit_per_window`) are pre-classified non-sensitive
operational config already at rest under RDS SSE (documented `data_protection_waiver` in
`acceptance.md`). `classified: 0`, `sensitive: 0`, `unprotected: []`.

## STRIDE delta addendum

_Empty — the diff introduces no attack surface the plan's threat model did not already cover._

## Out-of-scope / informational

- **Gitleaks 128 hits — all outside the change set.** 124 in gitignored `.venv/` (botocore example
  fixtures, vendored libs), 2 in `.pipeline/archive/` + prior-feature `docs/`, and 2 in **prior-feature**
  test fixtures (`tests/test_schemas_events.py`, `tests/integration/test_usage_export_endpoint.py` —
  merged in features 1/3, not in this diff). Zero hits in any quota change-set file. Not this feature's
  responsibility; noted for the record.
