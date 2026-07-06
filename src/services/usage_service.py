"""Service for `GET /v1/usage` — the scoped rollup read + hour flooring.

A missing bucket is a valid, common answer (zero usage), not an error — see
`get_usage`'s docstring for why this must never become a 404.
"""

from decimal import Decimal

from src.api.schemas.usage import UsageQueryParams, UsageResponse
from src.auth.api_key import AuthenticatedPrincipal
from src.db.session import scoped_transaction
from src.logging import get_logger
from src.repositories.usage_repo import find_usage_rollup
from src.services.time_windows import floor_to_hour_utc

logger = get_logger(service="meterly")


async def get_usage(
    principal: AuthenticatedPrincipal, query: UsageQueryParams
) -> UsageResponse:
    """Return the aggregated usage counter for `(customer_id, metric, window)`.

    A bucket with no recorded events returns zeros with a 200, never a 404 —
    returning 404 would leak whether *any* tenant has data for that bucket,
    and "no usage yet" is a legitimate steady state for a new customer.
    """
    window_start = floor_to_hour_utc(query.window)

    async with scoped_transaction(principal.api_key_id) as session:
        rollup = await find_usage_rollup(
            session,
            api_key_id=principal.api_key_id,
            customer_id=query.customer_id,
            metric=query.metric,
            window_start=window_start,
        )

    logger.info(
        "usage.read",
        userId=principal.api_key_id,
        action="read",
        found=rollup is not None,
    )

    total_quantity = rollup.total_quantity if rollup is not None else Decimal("0")
    event_count = rollup.event_count if rollup is not None else 0

    return UsageResponse(
        customer_id=query.customer_id,
        metric=query.metric,
        window_start=window_start.isoformat(),
        total_quantity=Decimal(total_quantity),
        event_count=event_count,
    )
