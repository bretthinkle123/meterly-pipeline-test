Feature: daily usage summary — GET /v1/usage/daily?date=YYYY-MM-DD
- Returns per-metric event counts for the authenticated customer for the given UTC day.
- Existing API-key auth applies (customer-scoped, not admin).
- 400 on a malformed or missing date; an empty metrics list (not 404) when the day has no events.
- No behavior change to POST /v1/events or any existing endpoint.
- The pipeline-ci workflow must be green on the PR (required merge check).
Design source: none.
