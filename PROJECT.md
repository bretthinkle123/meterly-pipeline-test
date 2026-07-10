Feature: quota administration — list and delete.
- GET /v1/quotas (admin-scoped API key) lists quotas.
- DELETE /v1/quotas removes the quota for {customer_id, metric}.
- Existing API-key auth applies. No behavior change to POST /v1/events when no quota exists.
- The pipeline-ci workflow must be green on the PR (it is now a required merge check).
Design source: none.
