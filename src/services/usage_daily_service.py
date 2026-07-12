"""Service for `GET /v1/usage/daily` — validate the `date`, aggregate the
scoped rollup, build the response, and emit the `usage.daily.read` log.

Adds **no** error-swallowing try/except of its own: an unexpected failure
from `aggregate_daily_event_counts` (a DB error, a connection drop) propagates
uncaught to the central `handle_unexpected_error` fail-closed boundary
(`src/api/errors.py`), identical to the sibling `usage_export_service`
posture (AC15).
"""

from src.api.schemas.usage_daily import (
    DailyMetricCount,
    DailyUsageQueryParams,
    DailyUsageResponse,
    parse_daily_date,
)
from src.auth.api_key import AuthenticatedPrincipal
from src.db.session import scoped_transaction
from src.logging import get_logger
from src.repositories.usage_repo import aggregate_daily_event_counts

logger = get_logger(service="meterly")


async def get_daily_usage(
    principal: AuthenticatedPrincipal, query: DailyUsageQueryParams
) -> DailyUsageResponse:
    """Return the caller's own per-metric event counts for one UTC day.

    Collapses every hour-bucket and `customer_id` under the tenant into one
    summed `event_count` per `metric` (see plan.md "Grouping by metric
    only"). A day with no events returns 200 with an empty `metrics` list,
    never 404 — the same "absence is a valid answer" contract `get_usage`
    documents.
    """
    window = parse_daily_date(query.date)

    async with scoped_transaction(principal.api_key_id) as session:
        records = await aggregate_daily_event_counts(
            session,
            api_key_id=principal.api_key_id,
            day_start=window.day_start,
            day_end=window.day_end,
        )

    logger.info(
        "usage.daily.read",
        userId=principal.api_key_id,
        action="read",
        resource="usage_rollup",
        date=window.date_str,
        metricCount=len(records),
    )

    return DailyUsageResponse(
        date=window.date_str,
        metrics=[
            DailyMetricCount(metric=record.metric, event_count=record.event_count)
            for record in records
        ],
    )
