Feature: usage CSV export.
- GET /v1/usage/export returns the caller's usage rollups (customer_id, metric, window_start, total_quantity) as CSV.
- Existing API-key auth applies. No behavior change to any existing endpoint.
Design source: none.
