"""Service for `/v1/quotas` — the admin-scoped upsert (`PUT`), list (`GET`),
and remove (`DELETE`) operations.

Each entry point orchestrates one transaction per request: the `xmax` upsert
in `src/repositories/quotas_repo.py` reports create-vs-replace in a single
round-trip for `PUT`; `GET` opens a read-only scoped transaction for the
ordered listing; `DELETE` maps "no row matched" to a 404 rather than a
silent success (plan §"DELETE — remove one cap, explicitly").
"""

from dataclasses import dataclass

from fastapi import HTTPException, status

from src.api.schemas.quotas import QuotaDeleteParams, QuotaPutRequest, QuotaResponse
from src.auth.api_key import AuthenticatedPrincipal
from src.db.session import scoped_transaction
from src.logging import get_logger
from src.repositories.quotas_repo import delete_quota, list_quotas, upsert_quota

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


async def list_tenant_quotas(principal: AuthenticatedPrincipal) -> list[QuotaResponse]:
    """Return the caller's own tenant's full, deterministically ordered quota
    list — an empty tenant gets an empty list, never a 404 (plan §"Empty set
    -> 200 [], never 404"). Deliberately not logged as a business event (open
    question 1, resolved default): a plain read of the caller's own
    configuration, mirroring `GET /v1/usage`.
    """
    async with scoped_transaction(principal.api_key_id) as session:
        rows = await list_quotas(session, api_key_id=principal.api_key_id)

    return [
        QuotaResponse(customer_id=row.customer_id, metric=row.metric, limit_per_window=row.limit_per_window)
        for row in rows
    ]


async def delete_tenant_quota(
    principal: AuthenticatedPrincipal, params: QuotaDeleteParams
) -> None:
    """Remove the cap for `(customer_id, metric)` under the caller's own
    tenant, raising 404 if no such quota exists (explicit, not silently
    idempotent — plan §"Why 404-on-absent, not idempotent-204").

    A cross-tenant delete attempt matches zero rows (the `api_key_id` filter
    + FORCE RLS backstop), which is indistinguishable from a truly-absent
    quota — the intended BOLA-safe behavior (threat E2).
    """
    async with scoped_transaction(principal.api_key_id) as session:
        removed = await delete_quota(
            session,
            api_key_id=principal.api_key_id,
            customer_id=params.customer_id,
            metric=params.metric,
        )

    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="quota not found")

    logger.info(
        "quota.delete",
        userId=principal.api_key_id,
        action="delete",
        customer_id=params.customer_id,
        metric=params.metric,
    )
