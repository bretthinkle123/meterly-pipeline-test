"""Validation contract for `PUT /v1/quotas`.

`customer_id`/`metric` reuse the exact anchored allowlists `src/api/schemas/events.py`
already validates against (the same identifiers, same sinks) — kept as separate
`constr` instances here rather than a cross-module import so this schema stays a
self-contained boundary contract (plan's validation-contract table). `api_key_id`
and `scope` are deliberately absent: the server sets `api_key_id` from the
authenticated principal and never accepts a client-supplied `scope`, which is
the mass-assignment defense against writing another tenant's cap or
self-elevating privilege (ASVS 15.3.3, threat T3/E2 in the plan's STRIDE model).
"""

from pydantic import BaseModel, ConfigDict, conint, constr

CustomerId = constr(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
Metric = constr(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
# BIGINT-safe upper bound: guards against an absurd/overflowing value reaching
# the BIGINT column or the R+Q>L comparison (plan's DoS row on limit_per_window).
LimitPerWindow = conint(ge=1, le=10**15)


class QuotaPutRequest(BaseModel):
    """The client-supplied body of `PUT /v1/quotas`.

    `extra='forbid'` rejects any unknown field outright, including a
    client-supplied `api_key_id` or `scope` — the create-or-replace identity
    is exactly `(customer_id, metric)`, resolved against the authenticated
    principal's own `api_key_id` by the service layer, never the request body.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    metric: Metric
    limit_per_window: LimitPerWindow


class QuotaResponse(BaseModel):
    """The minimal response field set for a quota (ASVS 15.3.1) — echoes only
    what the caller supplied, never the full `quotas` row (`api_key_id`,
    `created_at`, `updated_at` stay internal)."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    metric: str
    limit_per_window: int
