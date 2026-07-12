---
audited_at: 2026-07-11T20:36:27Z
plan_sha256: ae8994d13267a6574f6b7f394286b05965a71ea9f06e5f0f1bf2c0e75e7825d2
flags_total: 1
material_flags: 1
critical_flags: 0
revision_recommended: true
dependencies_checked: 0
dependencies_unverified: 0
---

# Plan audit — GET /v1/usage/daily

Audited `.pipeline/plan.md` (sha256 `ae8994d1...5e7825d2`) against `.pipeline/acceptance.md`
(14 criteria), `PROJECT.md`, and `CLAUDE.md`. Also spot-checked the plan's factual claims
about the existing codebase against source (`src/repositories/usage_repo.py`,
`src/api/routes/usage.py`, `src/api/routes/usage_export.py`, `src/api/routes/quotas.py`,
`src/auth/rate_limit.py`, `src/db/session.py`, `src/services/time_windows.py`,
`alembic/versions/0002_create_usage_rollup_backfill.py`, `src/api/errors.py`) — every
concrete claim (RLS policy name and predicate, `_require_authenticated_and_throttled` /
`_require_admin_and_throttled` sibling-helper pattern, `enforce_tier2_rate_limit`,
`scoped_transaction`, `floor_to_hour_utc`, the 400/422 status→code map) checked out
verbatim against the real source. No `.pipeline/repomix-pack.xml` exists, so the receipt
check (F-M4-6) is not applicable.

## Focus here first

- **[material]** `acceptance.md` has no **safe-error / fail-closed criterion** (ASVS-DET
  T2-2) for `GET /v1/usage/daily`. The endpoint has a real server-side error surface (a
  DB aggregate call, untrusted `date` input reaching a parser) and its own sibling
  endpoint `usage_export` carries exactly this test
  (`tests/integration/test_usage_export_endpoint.py::test_export_forced_pre_flight_count_error_returns_generic_500`,
  "AC22"). The daily plan states it reuses the same central error envelope
  (`src/api/errors.py`) but neither `plan.md` nor `acceptance.md` adds the matching
  forced-error assertion for this route. Recommend adding one criterion (e.g.
  monkeypatch `aggregate_daily_event_counts` to raise, assert 500 generic envelope, no
  partial/leaked body) before the checkpoint — cheap to add given the precedent test
  already exists to copy.
- No other blocking concerns. Completeness, STRIDE-mechanism concreteness, the `date`
  validation contract, the 400-vs-422 split, the cross-tenant/auth-boundary/rate-limit
  criteria, the test-strategy declaration, and the dependency claim ("no new
  dependencies") all check out — see tables below.

## Completeness

| Dimension | Status | Missing item | Blocks which agent | material/advisory |
|---|---|---|---|---|
| Layer sections present | ✓ | — (Frontend/Infra correctly omitted: API-only, no infra change) | — | — |
| Acceptance criteria traced | ✓ | All 14 `acceptance.md` criteria trace to a named plan section; no orphan plan claims found untraced (grouping-granularity and lookback-bound decisions are carried in Open questions with proposed answers) | — | — |
| Task decomposition coverage | N/A | `tasks_md_emitted: false` (7 files < 8-file threshold), no `tasks.md` to check | — | — |
| STRIDE mechanisms named | ✓ | Every threat row names a concrete mechanism + file + enabling condition (verified against source, see above) | — | — |
| Input-surface controls complete | ✓ | Sole input (`date` query param) carries both a validation criterion (AC3+AC4+AC5+AC6) and a rate-limit criterion (AC13, principal-keyed post-auth Tier-2, confirmed against `enforce_tier2_rate_limit`) | — | — |
| Data-protection complete | N/A | Plan stores nothing; correctly reasoned as not-triggered | — | — |
| Object-level authorization tested | ✓ | AC7 asserts cross-tenant denial (empty result, not another tenant's counts) | — | — |
| Authentication boundary tested | ✓ | AC8 asserts 401 on missing/invalid key | — | — |
| Safe-error handling tested | gap | No forced-internal-error / generic-envelope / fail-closed criterion for this route, despite a real server-side error surface and an existing sibling-endpoint precedent test to mirror | testing (would build no such test), security (nothing to reconcile) | **material** |
| Security-property tests (T2-3…T2-6) | N/A | No tokens issued/consumed, no session management, no multi-write/money operation, no password path — none of the triggers apply | — | — |
| App-store submission criteria | N/A | No store target declared in `PROJECT.md`/`CLAUDE.md` | — | — |
| DAST readiness | ✓ | AC14 (served OpenAPI schema + reused seeded DAST auth context) | — | — |
| ASVS compliance scoped | ✓ | `## ASVS Compliance` block present with triggered chapters, in-scope L3 stated ("none newly introduced"), waivers stated ("none") | — | — |
| Test strategy declared | ✓ | `pyramid`, with rationale (validation/branch logic is local → unit-heavy; DB behavior → integration) | — | — |
| Files affected concrete | ✓ | 7 files, each with path + new/mod + one-line reason | — | — |

## Ambiguities

None found. No vague directives, undefined referents, unspecified concrete choices,
internal contradictions, or unresolved markers (`TODO`/`TBD`/`???`) in the plan body.

## Proof-claim verification

The plan makes one "provably/by-construction"-class claim: *"this is IDOR-proof by
construction"* (Backend → Tenant scoping). Invariant: the endpoint's input surface
contains no object identifier the client controls (no `customer_id`, no `api_key_id`,
no path id) — tenant scope is derived solely from `principal.api_key_id`. Enforcement
points, both present: (a) `DailyUsageQueryParams(extra="forbid")` structurally rejects
any additional param (422), so a client cannot smuggle a scope-widening field in; (b)
`aggregate_daily_event_counts`'s first predicate is `api_key_id = :api_key_id` bound
from the principal, backstopped by the RLS policy `usage_rollup_tenant_isolation`
(confirmed present in `alembic/versions/0002_create_usage_rollup_backfill.py`, predicate
`api_key_id = current_setting('app.current_api_key_id', true)::bigint`), and AC7 is the
test that would fail if either broke. Invariant is enforced, not just asserted —
no flag.

## Cross-feature data-flow trace

The feature reads `usage_rollup`, written by the existing `POST /v1/events` path (via
migration 0002's backfill and the existing ingest upsert). The reading principal
(`principal.api_key_id`, from `require_api_key` on the daily route) is the same key
dimension the write path keys rows under (`usage_rollup.api_key_id`), and it is the
same scope dimension `GET /v1/usage` already reads successfully in production. No
scope/key mismatch — the reading principal can always own the rows it needs. No flag.

## Dependency reality

No new dependencies. `new_dependencies: none` in the plan frontmatter and the "Skills
consulted" section ("**New dependencies:** none. (Nothing for `dependency-audit-policy`
to review.)") both hold up against the plan text and the file list — no new
`requirements.txt`/`pyproject.toml` line, no new import of a third-party package
referenced anywhere in Backend/Stack notes/Files affected. `dependency-audit-policy`
skill was not invoked (N/A path, per its own trigger condition).

## Version policy

No new dependencies — N/A, nothing to evaluate.

## Could not verify

Nothing — all factual claims in the plan that were spot-checked against the codebase
(RLS policy, sibling-helper pattern, rate-limit dependency, `scoped_transaction`,
`floor_to_hour_utc`, status→code map) resolved and matched. No network-dependent lookups
were required (no new dependencies to check against a registry).
