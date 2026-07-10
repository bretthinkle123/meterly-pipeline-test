# Plan — quota administration (list + delete)

Authoritative brief: `.pipeline/requirements.md` (operator-elicited; Resolved / Open / Out-of-scope).
Every Resolved item below is turned into scope + an acceptance criterion; the brief has **zero Open
items** (the operator confirmed the defaults explicitly), and the Out-of-scope items are hard
exclusions that are **not** planned. Design source: none (backend HTTP API — no UI to build).

## Summary

Meterly already lets an admin-scoped key create-or-replace a per-customer, per-metric usage cap via
`PUT /v1/quotas` (feature "metric-quotas", shipped). This feature completes the quota-admin CRUD by
adding the two read/remove surfaces on the **same path**, gated by the **same** admin dependency
chain and scoped to the **same** tenant model:

1. **`GET /v1/quotas`** (new, admin-scoped) — returns the full, unpaginated list of the caller's own
   tenant's quotas, deterministically ordered by `(customer_id, metric)`, each row the minimal
   `{customer_id, metric, limit_per_window}` field set (identical to the existing `QuotaResponse`).
2. **`DELETE /v1/quotas?customer_id=X&metric=Y`** (new, admin-scoped) — removes the cap for one
   `(customer_id, metric)` in the caller's tenant. **204 No Content** on success; **404** (standard
   error envelope) when no such quota exists in the caller's tenant (explicit, not silently
   idempotent). Query parameters only, no request body.

The core approach is **extend, don't reinvent**: reuse the `_require_admin_and_throttled` dependency
(auth → Tier-2 throttle → admin-scope gate), reuse the exact anchored `CustomerId`/`Metric`
allowlists PUT already validates against, reuse `scoped_transaction` (which sets the RLS session
setting), and reuse the `quotas` table and its `quotas_tenant_isolation` FORCE-RLS policy that
migration `0003` already installed. No new table, **no new Alembic migration**, no new cloud
infrastructure, no new credential, and — critically — **no change to `POST /v1/events`**: deleting a
quota removes only the cap, never touching recorded `usage_rollup` usage. The scanner/auth context
stays the existing `Authorization: Bearer mtr_live_<key_id>_<secret>` split-token.

Why this is a genuine micro-change and not a rewrite: every layer already exists and already isolates
by `api_key_id`. GET adds one ordered `SELECT`; DELETE adds one parameterized `DELETE … RETURNING`;
both hang off the existing route module and its existing admin dependency. The only net-new
correctness surfaces are (a) the DELETE-not-found → 404 decision, (b) the query-param validation
contract for DELETE, and (c) safe-error / fail-closed handling on the two new DB-I/O paths (the
generic `500` envelope + transaction rollback) — all three covered explicitly below and in the threat
model.

## Stack notes

Assessed each default against this project; every choice **endorses the existing Meterly stack**
recorded in `CLAUDE.md`. This is a brownfield increment onto a shipped feature, so consistency with
the established stack is the overriding consideration and each default already fits — there is no
divergence for the checkpoint to ratify, only the confirmation below.

- **Language / framework:** Python 3.12 + FastAPI + async SQLAlchemy `text()` repositories — endorsed
  (matches every existing module and the sibling `PUT /v1/quotas` code this extends).
- **Database:** PostgreSQL (RDS) — endorsed; the feature reads and deletes rows in the existing
  `quotas` table using boring, proven primitives (`SELECT … ORDER BY`, `DELETE … RETURNING`). DDIA
  note: no new storage/messaging surface, so no replication/partitioning/consistency question arises
  — this is single-node transactional CRUD on an existing indexed table.
- **Migrations:** Alembic — endorsed, **but not exercised**: no schema change, so **no new revision**.
  The `quotas` table, its PK `(api_key_id, customer_id, metric)`, its `CHECK`, and its FORCE-RLS
  policy all already exist from revision `0003`. Adding a migration here would be dead DDL.
- **Auth:** existing API-key facade + the `scope` column (`ingest`/`admin`) — endorsed and reused
  unchanged. Both new routes reuse the existing `_require_admin_and_throttled` dependency verbatim; no
  new scope, provider, token type, or MFA path.
- **Cloud / IaC:** AWS + Terraform — **no change**. The feature provisions nothing (`iac-conventions`
  not triggered — no `infra/` resource). It reads/deletes rows in the existing RDS instance.
- **Observability / logging:** structlog facade + CloudWatch/X-Ray + Sentry — endorsed; the feature
  emits **one** new structured event (`quota.delete`) and reuses the existing `quota.forbidden`.
- **Runtime secrets:** unchanged — no new credential consumed (`secrets-management` not triggered).

## Backend

### `GET /v1/quotas` — the tenant's full quota list

- *What:* a new handler on the existing `src/api/routes/quotas.py` router,
  `@router.get("/v1/quotas", response_model=list[QuotaResponse])`, depending on the existing
  `_require_admin_and_throttled`. It calls a new service `list_tenant_quotas(principal)` that opens a
  `scoped_transaction(principal.api_key_id)` and calls a new repository `list_quotas(session,
  api_key_id)`, returning `list[QuotaResponse]`.
- *Why full/unpaginated (not paginated or filtered):* the brief resolves this explicitly — per-tenant
  quota counts are expected to stay small (bounded by the admin's own `PUT` calls, one row per
  `(customer, metric)`), so pagination/filtering would be speculative machinery for a set that fits in
  one response. Pagination is a hard out-of-scope exclusion. Rejected alternative — a
  `limit`/`offset` or cursor scheme — adds an input surface, an ordering-stability contract across
  pages, and tests, for zero benefit at the expected cardinality.
- *Why `response_model=list[QuotaResponse]` (the minimal field set):* the response echoes only
  `{customer_id, metric, limit_per_window}` — never `api_key_id`, `created_at`, or `updated_at`. This
  is the same minimal-exposure contract the brief pins and that `QuotaResponse` already encodes
  (ASVS 14.3.x / minimal response — threat **I1**). Returning the raw `quotas` row would leak the
  internal `api_key_id` and timestamps. FastAPI serializes the list through `QuotaResponse`, so the
  field projection is enforced by the framework, not by hand.
- *Why deterministic `ORDER BY customer_id, metric`:* the brief requires deterministic ordering. The
  repository issues `SELECT customer_id, metric, limit_per_window FROM quotas WHERE api_key_id =
  :api_key_id ORDER BY customer_id, metric`. `ORDER BY` on the two PK-prefix columns gives a stable,
  repeatable order every call (and is index-friendly — the PK `(api_key_id, customer_id, metric)`
  already orders exactly this way, so the sort is effectively free). Rejected alternative — relying on
  physical/heap order — is non-deterministic and would make the "deterministic" requirement
  unverifiable. (Collation note in Open Question 2.)
- *Empty set → `200 []`, never 404:* a caller with no quotas gets an empty JSON array with 200, the
  same "absence is not an error" semantics `GET /v1/usage` already uses (a zeroed bucket returns 200,
  not 404). A missing *collection* is an empty list; only a missing *addressed resource* (the DELETE
  target) is a 404.

### `DELETE /v1/quotas` — remove one cap, explicitly

- *What:* a new handler `@router.delete("/v1/quotas",
  status_code=status.HTTP_204_NO_CONTENT)` taking the two identifiers as **query parameters** through
  a new `QuotaDeleteParams` schema (`Annotated[QuotaDeleteParams, Query()]`, mirroring
  `GET /v1/usage`'s `UsageQueryParams`), depending on `_require_admin_and_throttled`. It calls a new
  service `delete_tenant_quota(principal, params)` → new repository `delete_quota(session, api_key_id,
  customer_id, metric)`. On success the handler returns `Response(status_code=204)` (empty body).
- *Why query params, not a body (DELETE semantics):* the brief resolves this. `DELETE` addresses a
  resource; the resource identity is `(customer_id, metric)`. Putting the identity in the query string
  keeps `DELETE` bodyless (many proxies/clients drop or ignore DELETE bodies) and mirrors how
  `GET /v1/usage` already addresses a `(customer_id, metric, window)` resource via query params. The
  params reuse the **exact same** anchored allowlists PUT uses (`CustomerId`, `Metric` already defined
  at module scope in `src/api/schemas/quotas.py`) — same identifiers, same sinks, same rejection
  behavior (422). Rejected alternative — a JSON body on DELETE — diverges from the `GET /v1/usage`
  addressing pattern and relies on body support that DELETE clients do not guarantee.
- *Why 404-on-absent, not idempotent-204 (the one real decision here):* the brief resolves that a
  delete of a non-existent quota returns **404 with the standard error envelope**, not a silent 204 —
  "deletes are explicit, not silently idempotent." The repository's `DELETE … WHERE api_key_id = :…
  AND customer_id = :… AND metric = :… RETURNING customer_id` returns a row iff exactly one matched;
  the service maps "no row returned" → `HTTPException(404, "quota not found")`, which the error facade
  renders as `{"error":{"code":"not_found", …}}` (404 is already in `_STATUS_TO_CODE`). No new error
  code is needed. Tradeoff accepted: a client that retries a successful DELETE gets a 404 on the
  second call — that is the brief-sanctioned explicit-delete contract, and it is the honest signal
  ("there was nothing here to delete"), distinguishable from a wrong-tenant delete only in that both
  correctly refuse to act.
- *Why 204 No Content on success:* the resource is gone; there is nothing meaningful to return, and
  echoing the deleted cap would invite treating DELETE as a read. 204 with an empty body is the
  idiomatic REST result and what the brief pins.
- *Tenant confinement is structural, not a special case:* the `DELETE` `WHERE` clause filters
  `api_key_id = :principal.api_key_id` (the primary control), and it runs inside `scoped_transaction`,
  which `SET LOCAL app.current_api_key_id` so the `quotas_tenant_isolation` **FORCE** RLS policy
  (migration `0003`) is the backstop. Because the policy's `USING` predicate hides other tenants'
  rows, a cross-tenant DELETE matches **zero** rows and therefore returns 404 — a wrong-tenant delete
  is indistinguishable from a truly-absent quota, which is exactly the desired BOLA-safe behavior
  (threat **E2** — DELETE cannot even *see* another tenant's row to remove it).

### DELETE racing in-flight `POST /v1/events` (explicitly no extra synchronization)

The brief resolves that a DELETE racing an in-flight ingest enforcement is **acceptable** and requires
no special synchronization. Concretely: `POST /v1/events` reads the quota with `SELECT … FOR UPDATE OF
q` inside its own transaction. A DELETE and an ingest for the same `(customer, metric)` contend on
that same row lock, so PostgreSQL serializes them — one commits first. If the DELETE commits first,
subsequent ingests read "no quota" and are unlimited; ingests that had already read the (old) cap
before the DELETE committed may still be enforced against it. This is the **same replacement race PUT
already has** and is correct: the delete takes effect for everything that reads after it commits. We
add **no** advisory lock, no serialization barrier, and no usage reset — all three are hard
out-of-scope exclusions. The DELETE's own row lock (implicit in the `DELETE`) plus the ingest's
`FOR UPDATE` are sufficient for a consistent commit order; there is no torn state.

## Data / migrations

**No migration, no schema change, no new stored field.** GET reads existing rows; DELETE removes
existing rows. The `quotas` table, its constraints, and its FORCE-RLS policy already exist (revision
`0003`). Therefore:

- No file is added under `alembic/versions/` (asserted negatively by AC14).
- **No `data_protection` criterion is emitted.** No field is *stored* by this feature; the rows it
  reads/deletes were already classified **non-sensitive operational config** in the metric-quotas plan
  (`customer_id`/`metric`/`limit_per_window` are opaque operational identifiers/config, already at
  rest under RDS SSE). Recorded explicitly so security's `data_surface` reconciliation sees the
  waiver: `data_protection_waiver: this feature stores no new field; the quota rows it lists/deletes
  are pre-classified non-sensitive operational config already covered by RDS SSE (storage-level) — no
  field-level KDF/KMS applies`.
- No migration-reversibility criterion is emitted (no migration to reverse).

## Auth

No auth change. Both new routes reuse the **existing** `_require_admin_and_throttled` dependency in
`src/api/routes/quotas.py` verbatim — the same composed chain PUT uses: `require_api_key` (Argon2id
split-token) → `enforce_tier2_rate_limit` (per-`api_key_id` throttle) → the `scope == 'admin'` gate
(→ `HTTPException(403)` + `quota.forbidden` log). This is **function-level authorization** (ASVS
8.2.1 — the admin gate) layered over **data-level** tenant isolation (ASVS 8.2.2/8.4.1 — the
`api_key_id` scoping + FORCE RLS on every `quotas` query). No new scope, provider, token, or MFA path;
`auth-patterns`' third-party IdP flows remain out of scope (this project uses API keys per
`CLAUDE.md`). GET is admin-only by the brief (no ingest-scoped read path — a hard exclusion).

## Infrastructure

No infrastructure change. The feature provisions no AWS service, IAM role, security group, or `infra/`
resource, and introduces no runtime secret. It reads/deletes rows in the existing RDS PostgreSQL
instance. `iac-conventions` is not triggered.

## Logging

Via the existing `structlog` facade (`get_logger(service="meterly")`), OTel trace-id already
propagated (`logging-conventions`). The facade already redacts `customer_id` (personal-data class) and
strips control characters centrally — call sites never redact ad hoc.

- **`quota.delete` (INFO)** — new; emitted by `delete_tenant_quota` on a successful 204:
  `userId=<admin api_key_id>`, `action="delete"`, `customer_id` (auto-redacted to `***redacted***` by
  the facade), `metric`, `requestId`. This is the **audit trail** for who removed which cap when —
  un-capping a customer is a security-relevant business action (it re-enables overage), so it must be
  attributable (repudiation / ASVS 16.2.1). This mirrors the existing `quota.upsert` field shape.
- **`quota.forbidden` (WARNING)** — reused unchanged; a non-admin key hitting GET or DELETE trips the
  existing scope gate, which already logs this (`userId`, `action="deny"`,
  `reason="insufficient_scope"`) — ASVS 16.3.2.
- **`request.unhandled_error` (ERROR)** — reused unchanged; an unexpected exception on either DB-I/O
  path is caught by the existing `handle_unexpected_error` boundary (`src/api/errors.py`), which logs
  the full detail server-side (`error.type`, `error.message`, `exc_info`) and returns the generic
  `500` envelope — the server-side half of the safe-error contract (AC19; ASVS 16.5.1).

**GET listing is deliberately not given a dedicated business log** — it is a read of the caller's own
configuration, following the `GET /v1/usage` precedent (plain reads emit no business audit event; the
request-context middleware already carries the per-request trace). Flagged as Open Question 1 for the
checkpoint to confirm. No log line carries usage totals or a raw `customer_id` (the facade guarantees
the latter).

## API edge

`api-edge-conventions` applied to the two new HTTP surfaces:

- **Rate limiting:** both routes are **per-owner resource** endpoints, so they inherit the
  **principal-keyed post-auth Tier-2** token bucket (via `_require_admin_and_throttled` →
  `enforce_tier2_rate_limit`, keyed on `api_key_id`) — not the IP-keyed Tier-1 (that stays the
  pre-auth edge shed, inherited by mounting). The two-principals-one-IP discriminating shape proves
  the key dimension (AC12).
- **CORS:** `configure_cors` currently allows `["GET", "POST", "PUT"]`; add `"DELETE"` for correctness
  (`GET` is already allowed). As with the PUT addition, this is a server-to-server API with an empty
  origin allowlist, so CORS is effectively inert — the change is for completeness, not a live control.
- **Error envelope:** DELETE-not-found raises a plain `HTTPException(404)`, which the existing
  `handle_http_exception` facade maps to `{"error":{"code":"not_found","message":…,"requestId":…}}`.
  No `AppError`/`app_code` extension is needed (unlike the quota `429`) because 404 already has a
  distinct code in `_STATUS_TO_CODE`. The 204 success path returns an empty body through the FastAPI
  `Response`.
- **Safe-error handling / fail-closed (AC19 — ASVS-DET T2-2):** both new handlers do live DB I/O
  (`list_quotas`' `SELECT`, `delete_quota`'s `DELETE … RETURNING`) that can raise server-side (driver
  error, connection drop, constraint surprise). Any such unexpected exception propagates to the
  **existing centralized `handle_unexpected_error` catch-all** in `src/api/errors.py`, which logs the
  detail server-side and returns the generic `{"error":{"code":"internal", "message":"an internal
  error occurred", "requestId":…}}` `500` — never a stack trace, SQL fragment, exception type, or
  internal path (ASVS 16.5.1/16.5.3). And because both service functions run their query inside
  `scoped_transaction` (`session.begin()`), an exception propagating out of that context **rolls the
  transaction back**: a DELETE that fails mid-operation leaves the target row **intact** (fail-closed —
  no partial/half delete), and GET is read-only so there is nothing to unwind. This is the exact
  shape feature 1 already proves for `POST /v1/events`
  (`test_forced_internal_error_returns_safe_envelope_and_fails_closed`); this feature reuses the same
  boundary and gets the same guarantee — AC19 verifies it on both new verbs so a forced DB exception
  is not an unverified leak/partial-state hole in the 19-criterion set.
- **Body-size guard / Tier-1 throttle / security headers:** inherited unchanged by being mounted.

## DAST readiness

`dast-conventions`: the running app serves these two new HTTP surfaces, so they must arrive scan-ready.

- **Served OpenAPI schema:** FastAPI auto-generates `/openapi.json` from the routes + Pydantic schemas;
  `GET /v1/quotas` and `DELETE /v1/quotas` (with `QuotaDeleteParams` and the `list[QuotaResponse]` /
  `204` responses) appear automatically. AC17 asserts both verbs are present under `/v1/quotas` in the
  served schema so the scanner's spec matches the implemented routes (not hand-drifted).
- **Seeded non-prod test user + auth context:** both endpoints are authenticated **and** admin-scoped,
  so the DAST auth context needs the **admin** test key. `seed_api_key.py --admin` (existing)
  provisions it in staging/local; the auth context stays `Authorization: Bearer
  mtr_live_<key_id>_<secret>` — the existing DAST-3 context, documented here so
  `test_dast_context_documented.py` stays green (it greps the plan/docs for `mtr_live` + `Bearer`).

## Input surface (enumerated for security's `input_surface` reconciliation)

Every input source the feature exposes, with its planned controls:

1. **`GET /v1/quotas`** — accepts **no** body/query/path parameter (full list, no filter/pagination —
   a hard exclusion). Its only input is the reused, unchanged `Authorization` header. **Validation
   AC: N/A** (no request parameter to validate). **Rate-limit:** Tier-2 per-`api_key_id` (reused) →
   AC12. Authorization/scope covered by AC4; auth by AC5.
2. **`DELETE /v1/quotas` query parameters** (new): `customer_id`, `metric`; unknown query params
   rejected (`extra='forbid'`). **Validation AC (non-waivable):** AC9. **Rate-limit:** Tier-2
   per-`api_key_id` (reused) → AC12.
3. **`Authorization` header** (unchanged): the existing anchored split-token regex in
   `src/auth/api_key.py` — reused, not modified.

No queue/message consumer, file/CSV ingest, form, or webhook receiver is introduced.

## Files affected

Modify (source + tests — the implementation change set):
- `src/api/schemas/quotas.py` — add `QuotaDeleteParams` (query-param contract; reuse the module-level
  `CustomerId`/`Metric`; `ConfigDict(extra='forbid')`). `QuotaResponse` reused unchanged for GET rows.
- `src/api/routes/quotas.py` — add the `GET` (list) and `DELETE` handlers; both depend on the existing
  `_require_admin_and_throttled`.
- `src/services/quota_service.py` — add `list_tenant_quotas(principal)` (read + map to
  `list[QuotaResponse]`) and `delete_tenant_quota(principal, params)` (delete, raise 404 if absent,
  emit `quota.delete`).
- `src/repositories/quotas_repo.py` — add `list_quotas(session, api_key_id)` (ordered, `api_key_id`-
  scoped `SELECT`) and `delete_quota(session, api_key_id, customer_id, metric)` (parameterized
  `DELETE … RETURNING`, returns whether a row matched).
- `src/api/middleware.py` — add `"DELETE"` to the CORS `allow_methods` list.
- `tests/test_schemas_quotas.py` — unit tests for `QuotaDeleteParams` (allowlist accept/reject,
  injection/oversized rejection, `extra='forbid'`).

Create:
- `tests/integration/test_quotas_list_delete.py` — integration tests for GET (order, minimal fields,
  empty→`200 []`, tenant isolation, ingest-key 403, no-auth 401) and DELETE (204 + row removed, 404
  absent, cross-tenant→404 + victim row intact, validation 422, ingest-key 403, no-auth 401,
  `quota.delete` logged), the Tier-2 two-principals-one-IP shape, the no-usage-reset / no-events-change
  guarantee, the safe-error / fail-closed 500 path on GET and DELETE (AC19), and the OpenAPI presence
  of both verbs.

Docs (handled at the documentation stage, per the "docs updated for touched directories"
done-criterion — not part of the implementation change set):
- `docs/system_architecture.md` — add the GET/DELETE quota-admin surface (also carries the
  `mtr_live`/`Bearer` DAST-3 context string).
- `src/api/README.md`, `src/services/README.md`, `src/repositories/README.md` — per-directory updates
  for the touched modules.
- `CLAUDE.md` — note `GET`/`DELETE /v1/quotas` alongside the existing `PUT` in the auth line.

**Task decomposition:** the implementation change set is **7 files** (6 modified + 1 created source/
test files above; docs are a separate stage). That is below the ≥8-file threshold, so **no
`tasks.md`** is emitted — this builds single-shot from `plan.md`, extending existing modules with a
repo function pair, a service function pair, two route handlers, and one query-param schema.

## Test strategy

**`pyramid`** (the project default). The unit tier carries the boundary logic that needs no database:
`QuotaDeleteParams` validation (allowlist, injection/oversized rejection, `extra='forbid'`) in
`tests/test_schemas_quotas.py`, and the 404-vs-204 decision is a thin mapping best proven end-to-end.
A **focused integration tier** against the real Postgres + Redis testcontainers (existing `conftest`)
covers the guarantees that are only real against actual Postgres — deterministic ordering over a
seeded multi-row set, the empty-list case, tenant isolation for both GET and DELETE (the FORCE-RLS
backstop + `api_key_id` filter), the DELETE 204/404 contract, the safe-error / fail-closed 500 path
(forced internal error → generic envelope, no partial delete), the two-principals-one-IP throttle
dimension, and the "delete removes the cap but never resets `usage_rollup`, and `POST /v1/events`
un-caps cleanly" cross-behavior. E2E stays minimal (the route round-trips are the integration tier).
This is a bias toward exercising the real datastore for the tenancy/ordering guarantees, still under
the unchanged `>= 85%` branch-coverage gate — not a relaxation of it.

## Open questions

1. **GET list read-logging.** The plan emits **no** dedicated business-audit log for `GET /v1/quotas`
   (a read of the caller's own config; mirrors `GET /v1/usage`, which logs no business event). DELETE
   *is* logged (`quota.delete`). *Proposed default:* keep GET unlogged. Confirm at the checkpoint, or
   add a light `quota.list` INFO (count only, no `customer_id`) if admin-read auditing is desired.
2. **Ordering collation.** `ORDER BY customer_id, metric` yields a **deterministic** order every call
   (the requirement), using the RDS default text collation. *Proposed default:* do not add
   `COLLATE "C"` — the requirement is *deterministic* order (guaranteed by `ORDER BY`), not
   byte-identical order across differing collations, and there is a single RDS collation in this
   deployment. Confirm, or add `COLLATE "C"` if cross-environment byte-stable ordering is later
   required.

## Threat Model

Method: STRIDE (Shostack), scoped to **this feature** (GET list + DELETE). Each credible threat names
a **concrete mechanism** (library/config/SQL construct + the file it lives in) and the **ASVS 5.0.0**
requirement it satisfies. Most controls are **reused, already-verified** mechanisms from the shipped
metric-quotas feature; the net-new surfaces are the DELETE query-param contract, the DELETE-not-found
path, and the safe-error / fail-closed handling on the two new DB-I/O paths.

### Assets and trust boundaries

Assets: the `quotas` rows (now *readable* via GET and *removable* via DELETE), the `api_keys.scope`
authorization attribute, the `usage_rollup` counters (which DELETE must **not** touch), the admin API
key (Argon2id-hashed secret), and the `GET`/`DELETE /v1/quotas` endpoints. Trust boundaries crossed:
**client ↔ app** (public internet → ALB → FastAPI: unauthenticated bytes become an authenticated,
scope-bearing principal) and **app ↔ RDS** (the scoped transaction that lists or deletes a tenant's
rows). No new service↔service or cloud-control-plane boundary is introduced.

### Validation contracts (per boundary input)

| Input | Contract (type + bound + allowlist) | Sink it protects | Lives in |
|---|---|---|---|
| `customer_id` (DELETE query) | `constr(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')` (reused module `CustomerId`); anchored allowlist, ReDoS-safe | the `DELETE … WHERE customer_id = :customer_id` SQL | `src/api/schemas/quotas.py` |
| `metric` (DELETE query) | `constr(pattern=r'^[A-Za-z0-9_.:-]{1,64}$')` (reused module `Metric`); anchored allowlist | the `DELETE … WHERE metric = :metric` SQL | `src/api/schemas/quotas.py` |
| unknown DELETE query params | `ConfigDict(extra='forbid')` on `QuotaDeleteParams` | mass-assignment / no client-set `api_key_id` | `src/api/schemas/quotas.py` |
| `GET /v1/quotas` | no request parameter accepted (no body/query/path) — no injectable input | n/a (only the `api_key_id`-scoped list `SELECT`) | `src/api/routes/quotas.py` |
| `Authorization` header | existing anchored `^mtr_live_<key_id>_<secret>$` regex | `api_keys` lookup | `src/auth/api_key.py` (unchanged) |

All new SQL (the list `SELECT` and the `DELETE`) uses SQLAlchemy `text()` with **bound parameters
only** — no string interpolation (ASVS 1.2.4).

### STRIDE table

| Category | Asset / Boundary | Attack vector | Severity | Mitigation (mechanism + file) | ASVS req(s) |
|---|---|---|---|---|---|
| **Spoofing** | GET/DELETE `/v1/quotas` / client↔app | Unauthenticated or non-admin caller lists or deletes caps | High | Reused `_require_admin_and_throttled`: `require_api_key` (Argon2id split-token) then a `scope == 'admin'` assertion raising `HTTPException(403)` — `src/api/routes/quotas.py` + `src/auth/api_key.py` | 6.2.x, 8.2.1 |
| **Tampering** | DELETE SQL / client↔app | SQLi via `customer_id`/`metric` query params into the new `DELETE` | High | Anchored allowlist `constr` on `QuotaDeleteParams` (`src/api/schemas/quotas.py`) rejects at the boundary (422) + parameterized `text()` bind params in `delete_quota` (`src/repositories/quotas_repo.py`) — payload never reaches the sink | 1.2.4, 2.2.1 |
| **Tampering** | DELETE params / mass-assignment | Client injects an extra query param (e.g. `api_key_id`) to widen the delete | Medium | `ConfigDict(extra='forbid')` on `QuotaDeleteParams`; `api_key_id` is set server-side from the principal, never a query param — `src/api/schemas/quotas.py`, `src/services/quota_service.py` | 15.3.3, 8.2.3 |
| **Repudiation** | admin delete / audit | Admin denies removing a cap (un-capping a customer re-enables overage) | Medium | Structured `quota.delete` INFO (`userId`, `action="delete"`, `customer_id` [facade-redacted], `metric`, `requestId`) via the structlog facade; `requestId` ties the 204/404 response to the log line — `src/services/quota_service.py` | 16.2.1 |
| **Info Disclosure** | GET response / client↔app | List leaks internal fields (`api_key_id`, timestamps) or another tenant's caps | High | `response_model=list[QuotaResponse]` projects only `{customer_id, metric, limit_per_window}` (FastAPI-enforced); the list `SELECT` filters `api_key_id = :principal.api_key_id` + FORCE-RLS backstop — `src/api/routes/quotas.py`, `src/repositories/quotas_repo.py`, `alembic/versions/0003_*` | 14.3.x, 8.2.2 |
| **Info Disclosure** | GET/DELETE DB I/O / app↔RDS | A forced/unexpected server-side error (DB driver/connection exception mid-`SELECT` or mid-`DELETE`) leaks a stack trace, SQL fragment, exception type, or internal path in the response; or a DELETE half-applies leaving inconsistent state | Medium | Reused centralized `handle_unexpected_error` catch-all returns the generic `{code:internal}` `500` envelope (detail logged server-side via `request.unhandled_error`, **never** in the response body) — no stack/SQL/type/path leak; both service paths run inside `scoped_transaction`, so a raised exception rolls the transaction back (**fail-closed — no partial DELETE; GET is read-only**). Verified by AC19, mirroring feature 1's `POST /v1/events` precedent — `src/api/errors.py`, `src/services/quota_service.py`, `src/db/session.py` | 16.5.1, 16.5.3 |
| **Info Disclosure** | DELETE query string | `customer_id`/`metric` in the URL query string may land in ALB/proxy access logs | Low (accepted) | Mirrors the existing `GET /v1/usage` query-param contract (the caller's *own* opaque identifiers); app-side structured logs **redact** `customer_id` via the logging facade; ALB access-log exposure of a caller's own id is a pre-existing, accepted risk — `src/logging/__init__.py` | 14.3.x |
| **Denial of Service** | both endpoints | Request flood spends Argon2id verifies / DB work | Medium | Reused Tier-1 IP+route pre-auth throttle (`Tier1EdgeThrottleMiddleware`, `/health` exempt, client IP from `request.client` behind the ALB) + Tier-2 per-`api_key_id` post-auth token bucket (`enforce_tier2_rate_limit`) on both routes — `src/api/middleware.py`, `src/auth/rate_limit.py` | 2.4.1, 4.x |
| **Denial of Service** | GET list size | Unpaginated list grows expensive if a tenant accumulates many quotas | Low (accepted) | Per-tenant quota counts are bounded by admin `PUT` volume (brief: "expected to stay small"); the `SELECT` is a PK-prefix-ordered index scan scoped to one `api_key_id`; Tier-2 throttle bounds call rate — `src/repositories/quotas_repo.py` | 4.x |
| **Elevation of Privilege** | GET/DELETE (function-level) | `ingest`-scoped key calls the admin-only routes | High | Reused `scope == 'admin'` gate → `HTTPException(403)`; `quota.forbidden` logged — `src/api/routes/quotas.py` | 8.2.1 |
| **Elevation of Privilege** | `quotas` (data-level / IDOR/BOLA) | A key lists or **deletes** another tenant's cap | High | Every `quotas` query filters `api_key_id = :principal.api_key_id`; `quotas_tenant_isolation` **FORCE** RLS policy via `SET LOCAL app.current_api_key_id` in `scoped_transaction`. **Enabling condition (U-02):** `quotas` is owned by the `meterly_app` role, so `FORCE ROW LEVEL SECURITY` (already set in migration `0003`) is required and present — a table owner bypasses non-FORCE RLS. A cross-tenant DELETE therefore matches zero rows → 404 (cannot see or remove another tenant's row) — `src/repositories/quotas_repo.py`, `src/db/session.py`, `alembic/versions/0003_*` | 8.2.2, 8.4.1 |
| **Tampering** | `usage_rollup` integrity | DELETE resets/corrupts recorded usage, or races ingest into torn state | Medium (accepted) | `delete_quota` issues **only** a `DELETE` on `quotas` — it never touches `usage_rollup` (no usage reset, a hard exclusion). The DELETE's implicit row lock and the ingest's `SELECT … FOR UPDATE OF q` serialize on the same quota row, giving a consistent commit order without added synchronization (brief-accepted) — `src/repositories/quotas_repo.py` | 2.3.x |

### Accepted risks / out of scope

- **DELETE-vs-ingest race** is accepted with **no** added synchronization (brief): the shared row lock
  yields a consistent commit order; enforcement stops for everything that reads after the delete
  commits, matching PUT-replacement semantics. Not a concurrency-integrity guarantee this slice.
- **`customer_id` in the DELETE query string** (Low): consistent with the existing `GET /v1/usage`
  query-param design; app logs redact it; ALB access-log exposure of a caller's own id is accepted.
- **Unpaginated GET list size** (Low): accepted on the brief's "counts stay small" assumption; Tier-2
  throttle + index scan bound it.
- **Quota data at rest** relies on RDS SSE (non-sensitive classification) — no field-level crypto;
  this feature stores no new field.
- **Hard exclusions not modeled:** pagination/filtering on GET, ingest-scoped read of quotas,
  including usage/remaining in the list, usage reset on delete, any `POST /v1/events` behavior change,
  and blocking/serializing delete against ingest — all out of scope per the brief.

### Threat-model diagram (Mermaid DFD)

```mermaid
flowchart TD
    admin[Admin API client]:::ext
    subgraph edge[Trust boundary: public edge - ALB to FastAPI]
        t1(Tier-1 IP throttle + body guard)
        auth(require_api_key + Tier-2 throttle)
        scope(admin-scope gate):::warn
        getq(GET /v1/quotas handler)
        delq(DELETE /v1/quotas handler):::warn
        err(handle_unexpected_error - generic 500 envelope)
    end
    subgraph rds[Trust boundary: app to RDS Postgres - RLS FORCE]
        keys[(api_keys + scope)]
        quotas[(quotas)]
        rollup[(usage_rollup - untouched)]
    end
    redis[(Redis token buckets)]

    admin -- HTTPS Bearer mtr_live_key -->|DELETE query customer_id,metric| t1
    t1 -- rate ok --> auth
    auth -. verify Argon2id .-> keys
    auth -. token bucket .-> redis
    auth --> scope
    scope -- scope=admin --> getq
    scope -- scope=admin --> delq
    getq -- SELECT api_key_id-scoped ORDER BY --> quotas
    delq -- DELETE api_key_id-scoped RETURNING --> quotas
    getq -. DB error .-> err
    delq -. DB error - txn rollback, no partial delete .-> err
    delq -. never writes .-x rollup

    classDef ext fill:#eee,stroke:#333;
    classDef warn fill:#ffe0b2,stroke:#b71c1c,stroke-width:2px;
```

⚠ high-risk nodes: the **admin-scope gate** (Spoofing/EoP) and the **DELETE handler** (cross-tenant
IDOR/BOLA — mitigated by the `api_key_id` filter + FORCE RLS, so a wrong-tenant delete matches zero
rows → 404). A forced DB error on either path lands at the generic-500 boundary (no leak, txn
rolled back — AC19).

### Copy-paste visualization prompt

```text
Build a threat-model visualization for this feature (Meterly quota administration: list + delete).

ASSETS: quotas rows (per-tenant caps: api_key_id, customer_id, metric, limit_per_window), now
readable via GET and removable via DELETE; api_keys.scope authorization attribute; usage_rollup
hourly counters (which DELETE must NOT touch); the admin API key (Argon2id-hashed split token); the
GET /v1/quotas and DELETE /v1/quotas HTTP endpoints.

TRUST BOUNDARIES: (1) client <-> app: public internet -> ALB -> FastAPI, where unauthenticated bytes
become an authenticated, admin-scope-bearing principal; (2) app <-> RDS PostgreSQL: the scoped
transaction that lists or deletes a tenant's quota rows, with a FORCE row-level-security policy
(quotas_tenant_isolation) as the backstop behind the explicit api_key_id filter.

STRIDE THREATS (threat; vector; severity; mitigation + concrete mechanism):
- Spoofing; unauthenticated/non-admin caller lists or deletes caps; HIGH; reused
  _require_admin_and_throttled = require_api_key (Argon2id split token) + admin-scope assertion
  raising HTTP 403.
- Tampering; SQL injection via customer_id/metric DELETE query params; HIGH; anchored allowlist
  constr on QuotaDeleteParams rejects at the boundary (422) + parameterized SQLAlchemy text() bind
  params only.
- Tampering; extra query param to widen the delete (mass-assignment); MEDIUM; Pydantic
  extra='forbid'; api_key_id set server-side from the principal, never a query param.
- Repudiation; admin denies removing a cap (un-capping re-enables overage); MEDIUM; structured
  quota.delete INFO log (userId, action=delete, customer_id [redacted], metric, requestId).
- Information disclosure; GET leaks internal fields or another tenant's caps; HIGH;
  response_model=list[QuotaResponse] minimal field set + api_key_id-scoped SELECT + FORCE RLS.
- Information disclosure / safe-error; a forced server-side error (DB driver/connection exception) on
  GET or DELETE leaks a stack trace/SQL/exception type/internal path, or a DELETE half-applies;
  MEDIUM; centralized handle_unexpected_error catch-all returns the generic {code:internal} 500
  envelope (detail logged server-side only, no leak); both paths run inside scoped_transaction so a
  raised exception rolls the transaction back (fail-closed, no partial DELETE; GET is read-only).
- Information disclosure; customer_id/metric in the DELETE query string may reach access logs; LOW
  (accepted); mirrors GET /v1/usage; app logs redact customer_id; ALB log exposure of own id
  accepted.
- Denial of service; request flood; MEDIUM; reused Tier-1 IP throttle + Tier-2 per-api_key_id token
  bucket + body-size guard.
- Denial of service; unpaginated GET list grows expensive; LOW (accepted); per-tenant counts stay
  small; PK-prefix index scan scoped to one api_key_id; Tier-2 throttle bounds call rate.
- Elevation of privilege; ingest-scoped key calls the admin-only routes; HIGH; scope=='admin' gate
  -> HTTP 403.
- Elevation of privilege; a key lists or deletes another tenant's cap (IDOR/BOLA); HIGH; every query
  filters api_key_id = principal.api_key_id; FORCE RLS quotas_tenant_isolation via SET LOCAL
  app.current_api_key_id (FORCE required because meterly_app owns the table) -> cross-tenant DELETE
  matches zero rows -> 404.
- Tampering; DELETE resets usage or races ingest into torn state; MEDIUM (accepted); delete_quota
  only DELETEs quotas, never touches usage_rollup; shared row lock with the ingest FOR UPDATE gives
  a consistent commit order without added synchronization.

Render this as an OWASP Threat Dragon diagram. Output either (a) valid Threat Dragon JSON importable
at app.threatdragon.com, or (b) a labeled data flow diagram with trust boundaries if JSON is not
feasible. No additional context is available beyond what is in this prompt.
```

## ASVS Compliance

OWASP ASVS 5.0.0. **L1 + L2 are universal** for every triggered chapter; in-scope **L3** is listed
below. Requirement IDs are cited per threat in the STRIDE table above.

**Triggered chapters:** V1 (encoding / parameterized SQL — the DELETE `text()` bind params), V2
(validation — the `QuotaDeleteParams` contract + anti-automation Tier-2 throttle), V4 (API surface —
two new verbs on `/v1/quotas`), V6 (API-key authentication — reused), V8 (authorization — the admin
gate + tenant isolation on read/delete; **the priority chapter for this slice** given DELETE's
cross-tenant BOLA risk), V12 (TLS — existing ALB/RDS TLS, unchanged), V13 (config — docs off in prod,
secrets in manager — existing), V14 (data protection — minimal GET response, no sensitive data
stored), V16 (logging + error handling — `quota.delete` audit, generic 404 envelope, and the generic
`500` **fail-closed catch-all** for unexpected DB-I/O errors on both new verbs — 16.5.1/16.5.3,
AC19).

**`n/a`:** V3 (server-to-server API, no browser HTML/cookies), V5 (no file handling), V7 (no
sessions — stateless API keys), V9 (no JWT/self-contained tokens), V10 (no OAuth/OIDC), V11 (no new
crypto surface — Argon2id key hashing unchanged), V15 (no new concurrency-integrity guarantee — the
DELETE-vs-ingest race is an explicitly accepted risk, not an enforced invariant this slice), V17 (no
WebRTC).

**In-scope L3:**
- **V8.2.x — object-level authorization rigor (IDOR/BOLA).** Justification: DELETE is a destructive
  cross-tenant-sensitive operation on a multi-tenant store; "a key can neither see nor delete another
  tenant's quota" is breach-critical for a metering product, delivered by the `api_key_id` filter +
  FORCE RLS and proven by the cross-tenant-DELETE→404 integration test (AC8).

**Waivers:** none for triggered L1/L2 code/config items (the feature's surface is covered by the
reused mechanisms above). Chapter `X.1` documentation items are advisory (org-level), not blocking.

## Acceptance criteria trace (PROJECT.md / CLAUDE.md "What done means")

- **Smoke check passes** → ✓ *Backend* / *Infrastructure*: no change to `/health` or app startup; the
  new handlers mount on the existing router; deferred DB/Redis init preserved.
- **Security report clean** → ✓ *Threat Model* + *ASVS Compliance* (parameterized DELETE SQL, admin
  gate, read/delete tenant isolation + FORCE RLS, minimal GET response, generic 404 envelope, and the
  safe-error / fail-closed generic 500 envelope on both DB-I/O paths); AC9, AC15, AC18, AC19.
- **Tests pass at >= 85% coverage** → ✓ *Test strategy* + `acceptance.md` (every AC maps to a named
  test; unit for the schema contract, integration for the Postgres-only ordering/tenancy guarantees).
- **Docs updated for touched directories** → ✓ *Files affected* (system_architecture.md + per-dir
  READMEs + CLAUDE.md, at the documentation stage).
- **PR description written** → handled by the documentation stage (out of planning's hands; noted).
- **pipeline-ci green (required merge check)** → ✓ *CI*: this feature adds no workflow and no new job;
  it must keep the existing `pipeline-ci.yml` jobs (lint, tests+coverage, security scan) green. The
  new tests run under the same suite; no CI configuration change is planned.

## Revision notes

Single plan-audit revision pass (`.pipeline/plan-audit.md`, `revision_recommended: true`). One
material flag, addressed:

- **[material] No safe-error-handling (ASVS-DET T2-2) criterion for GET/DELETE `/v1/quotas`.** Both
  handlers do live DB I/O that can raise server-side, and the project has an exact precedent
  (`tests/integration/test_events_endpoint.py::test_forced_internal_error_returns_safe_envelope_and_fails_closed`,
  feature 1's AC19). Resolved — **not** waived as N/A, because the flag is correct (a forced DB
  exception on GET/DELETE would otherwise be unverified for leak/partial-state). Changes made:
  1. Added **AC19** to `.pipeline/acceptance.md` (a real, test-backed criterion — *not* delegated):
     a forced internal error on the GET list read and on the DELETE path returns the generic
     `500 internal` envelope (no stack/SQL/type/path leak), and the DELETE **fails closed** (the
     `scoped_transaction` rollback leaves the target row intact). Named the two integration tests in
     `tests/integration/test_quotas_list_delete.py`, mirroring the events precedent shape
     (monkeypatch the repo fn to raise, `ASGITransport(raise_app_exceptions=False)`).
  2. Bumped `.pipeline/acceptance.md` frontmatter `criteria_total: 18 → 19`; `delegated_criteria`
     unchanged (`[AC18]` — AC19 is test-backed, not delegated). Added a notes line explaining AC19's
     provenance, and recorded ASVS-DET T2-2 in `derived_from`.
  3. Added plan coverage in **`## API edge`** (a new "Safe-error handling / fail-closed (AC19)"
     bullet naming the `handle_unexpected_error` catch-all + the `scoped_transaction` rollback), in
     **`## Logging`** (the reused `request.unhandled_error` server-side log), in the **`## Test
     strategy`** integration list, and in the **`## Files affected`** description of the new
     integration test.
  4. Added a **STRIDE** Information-Disclosure row (GET/DELETE DB I/O boundary; Medium; generic-500
     catch-all + txn rollback; ASVS 16.5.1/16.5.3), added the matching line to the **copy-paste
     visualization prompt**, annotated the **Mermaid DFD** with the error boundary + rollback edge,
     and extended the **ASVS V16** line to cite the 500 fail-closed catch-all.
  5. Updated the **Summary** net-new-surfaces list and the **Security report clean** acceptance trace
     to include AC19.

No other changes — all other audit dimensions read clean (completeness, ambiguity, proof-claim,
dependency), and scope was not expanded beyond resolving this flag.
