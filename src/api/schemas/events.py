"""Validation contract for `POST /v1/events`.

Every field the client may set is bounded by an anchored allowlist regex or a
strict numeric range (`code-standards` input-validation invariant); `api_key_id`,
`event_time`, and `window_start` are deliberately absent from this schema — the
server sets them from the authenticated principal and the receive time, never
from client input (ASVS 15.3.3 mass-assignment protection, threat T2/E3 in the
plan's STRIDE model).
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, condecimal, constr

CustomerId = constr(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
Metric = constr(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
IdempotencyKey = constr(pattern=r"^[A-Za-z0-9_-]{1,200}$")
Quantity = condecimal(gt=Decimal(0), le=Decimal("1e12"), max_digits=20, decimal_places=6)


class EventCreateRequest(BaseModel):
    """The client-supplied body of `POST /v1/events`.

    `extra='forbid'` rejects any unknown field outright — this is the
    mass-assignment defense: even if a client sends `api_key_id` or
    `event_time`, the request is rejected rather than silently ignored, so
    there is no ambiguity about what the server actually used.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    metric: Metric
    quantity: Quantity
    idempotency_key: IdempotencyKey


class EventResponse(BaseModel):
    """The minimal response field set for an event (ASVS 15.3.1) — never a raw ORM row."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    customer_id: str
    metric: str
    quantity: Decimal
    window_start: str
    idempotent_replay: bool
