"""Service for `PUT /v1/quotas` — the admin-scoped upsert + 201/200 mapping.

Orchestrates one transaction per request: the `xmax` upsert in
`src/repositories/quotas_repo.py` reports create-vs-replace in a single
round-trip, so this layer only maps that to the HTTP status and emits the
audit-trail log (`quota.upsert`, plan §"Repudiation").
"""

from dataclasses import dataclass

from src.api.schemas.quotas import QuotaPutRequest, QuotaResponse
from src.auth.api_key import AuthenticatedPrincipal
from src.db.session import scoped_transaction
from src.logging import get_logger
from src.repositories.quotas_repo import upsert_quota

logger = get_logger(service="meterly")


@dataclass(frozen=True)
class QuotaUpsertOutcome:
    """The result of processing a `PUT /v1/quotas` request: the stored quota
    and the HTTP status the route should return (201 create, 200 replace)."""

    customer_id: str
    metric: str
    limit_per_window: int
    http_status: int


async def upsert_tenant_quota(
    principal: AuthenticatedPrincipal, payload: QuotaPutRequest
) -> QuotaUpsertOutcome:
    """Create-or-replace the cap for `(customer_id, metric)` under the
    authenticated principal's own `api_key_id` — the server, never the
    client, sets `api_key_id`, which is the mass-assignment protection
    (ASVS 15.3.3) against writing another tenant's cap.
    """
    async with scoped_transaction(principal.api_key_id) as session:
        record = await upsert_quota(
            session,
            api_key_id=principal.api_key_id,
            customer_id=payload.customer_id,
            metric=payload.metric,
            limit_per_window=payload.limit_per_window,
        )

    action = "create" if record.inserted else "replace"
    logger.info(
        "quota.upsert",
        userId=principal.api_key_id,
        action=action,
        customer_id=record.customer_id,
        metric=record.metric,
        limit_per_window=record.limit_per_window,
    )
    return QuotaUpsertOutcome(
        customer_id=record.customer_id,
        metric=record.metric,
        limit_per_window=record.limit_per_window,
        http_status=201 if record.inserted else 200,
    )


def to_response(outcome: QuotaUpsertOutcome) -> QuotaResponse:
    """Map the service outcome to the minimal public response shape (ASVS 15.3.1)."""
    return QuotaResponse(
        customer_id=outcome.customer_id,
        metric=outcome.metric,
        limit_per_window=outcome.limit_per_window,
    )
