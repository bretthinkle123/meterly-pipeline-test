# src/api/schemas/

## Purpose

Pydantic request/response models — the validation boundary for every route in
`src/api/routes/`. Every client-settable field is bounded by an anchored
allowlist regex or a strict numeric/temporal range; `extra="forbid"` on every
model rejects unknown fields outright (mass-assignment defense, ASVS 15.3.3).
Identity fields (`api_key_id`, `scope`) are never accepted from the client —
the server resolves them from the authenticated principal.

## Modules

| File / module | Responsibility |
|---|---|
| `events.py` | `EventCreateRequest`/`EventResponse` for `POST /v1/events`. Defines the shared `CustomerId`, `Metric`, `IdempotencyKey`, `Quantity` `constr`/`condecimal` allowlists that `quotas.py`, `usage.py`, `usage_export.py`, and `usage_daily.py`'s sibling helpers (or their own copies) build on. |
| `usage.py` | `UsageQueryParams`/`UsageResponse` for `GET /v1/usage`. `window` is an `AwareDatetime` bounded to `[now-90d, now+1h]` via `_window_within_supported_range`. |
| `usage_export.py` | `UsageExportQueryParams` for `GET /v1/usage/export` — optional `customer_id`/`metric`/`from`/`to` filters, the same `[now-90d, now+1h]` bound applied to a range via `_validate_window_bounds`. No dedicated response model (the route returns a `StreamingResponse`; its OpenAPI shape is declared inline in `usage_export.py`'s route module instead). |
| `quotas.py` | `QuotaPutRequest`/`QuotaResponse`/`QuotaDeleteParams` for `/v1/quotas`. Its own `CustomerId`/`Metric` `constr` instances (same pattern as `events.py`'s, kept separate so this schema stays a self-contained boundary contract) plus `LimitPerWindow` (`conint(ge=1, le=10**15)`, BIGINT-safe). |
| `usage_daily.py` | `DailyUsageQueryParams`/`DailyUsageResponse`/`DailyMetricCount` for `GET /v1/usage/daily`. `date` is a loosely-typed `str \| None` (not `datetime.date`) so a missing/malformed/out-of-range value is validated imperatively by `parse_daily_date`, which raises `HTTPException(400)` directly rather than falling through to FastAPI's automatic Pydantic-422 mapping — the one deliberate 400-not-422 deviation from `usage.py`/`usage_export.py`'s convention (an *undeclared* query param still rides the house 422 path via `extra="forbid"`). `day_window_for` is the pure date-arithmetic helper (half-open `[day_start, day_end)` UTC window) `parse_daily_date` calls once a `date` string passes validation. |

## Relationships

**Public surface:** each module's models are imported by exactly one sibling
route module in `src/api/routes/` (e.g. `usage_daily.py` here <-> `usage_daily.py`
in `routes/`) and, where the schema exposes validated values a service needs,
by the matching module in `src/services/` (e.g. `usage_daily_service.py`
imports `parse_daily_date`, `DailyMetricCount`, `DailyUsageQueryParams`,
`DailyUsageResponse` from here).

**Shared allowlists:** `CustomerId`/`Metric` originate in `events.py` and are
imported directly by `usage.py` and `usage_export.py` (same identifiers, same
SQL sinks downstream). `quotas.py` defines its own copies rather than
importing, to stay a self-contained boundary contract; `usage_daily.py` has no
`customer_id`/`metric` field at all — scope is resolved solely from the
authenticated principal, which removes that class of IDOR parameter entirely.

**Validation boundary discipline:** no schema in this directory ever builds
SQL or touches the database — every validated field travels onward only as a
plain Python value (or, for `usage_daily.py`, the `DailyDateWindow` dataclass)
that the service/repository layers bind as a query parameter.
