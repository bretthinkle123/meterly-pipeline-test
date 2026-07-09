"""Repository for reading `usage_rollup` — the O(1) aggregate lookup behind
`GET /v1/usage`.

Reads the pre-aggregated counter rather than summing `events` live, which is
what keeps the GET p95 budget bounded as ingest volume grows (see the plan's
"why reading the rollup" writeup).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class UsageRollupRecord:
    """The aggregate counter for one `(api_key_id, customer_id, metric, window_start)` bucket."""

    total_quantity: Decimal
    event_count: int


async def find_usage_rollup(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    window_start: datetime,
) -> UsageRollupRecord | None:
    """Fetch the usage counter for a bucket, scoped by the authenticated
    `api_key_id` first — an authenticated key can never read another
    tenant's totals (IDOR/BOLA mitigation, AC17). None means no events have
    landed in this bucket yet (the caller returns zeros, not 404)."""
    result = await session.execute(
        text(
            """
            SELECT total_quantity, event_count
            FROM usage_rollup
            WHERE api_key_id = :api_key_id
              AND customer_id = :customer_id
              AND metric = :metric
              AND window_start = :window_start
            """
        ),
        {
            "api_key_id": api_key_id,
            "customer_id": customer_id,
            "metric": metric,
            "window_start": window_start,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return UsageRollupRecord(total_quantity=row["total_quantity"], event_count=row["event_count"])
