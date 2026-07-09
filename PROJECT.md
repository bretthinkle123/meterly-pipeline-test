Feature: per-customer metric quotas.
- PUT /v1/quotas (admin-scoped API key) sets {customer_id, metric, limit_per_window}.
- POST /v1/events returns 429 with a quota error envelope when the current-window rollup for that customer/metric would exceed the quota. Customers without a quota are unlimited (no behavior change).
- One expand-only migration: a quotas table.
- Existing perf budget applies: p95 < 50 ms under load on POST /v1/events.
Design source: none.
