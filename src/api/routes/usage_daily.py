"""`GET /v1/usage/daily` — the authenticated tenant's per-metric event counts
for one UTC day, aggregated from the pre-computed `usage_rollup` counters.

Kept in its own module (not added to `usage.py`) so `GET /v1/usage`'s code
path is literally untouched by this change — the brief's no-behavior-change
constraint is then trivially auditable in the diff (mirrors
`src/api/routes/usage_export.py`'s rationale for the same choice).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from src.api.schemas.usage_daily import DailyUsageQueryParams, DailyUsageResponse
from src.auth import require_api_key
from src.auth.api_key import AuthenticatedPrincipal
from src.auth.rate_limit import enforce_tier2_rate_limit
from src.services.usage_daily_service import get_daily_usage

router = APIRouter(tags=["usage"])


async def _require_authenticated_and_throttled(
    request: Request, principal: AuthenticatedPrincipal = Depends(require_api_key)
) -> AuthenticatedPrincipal:
    """Compose auth then the Tier-2 per-key throttle, in that order (kept as
    a sibling, not a shared import, per the existing per-route convention —
    each route's dependency chain stays independently readable)."""
    await enforce_tier2_rate_limit(request, principal)
    return principal


@router.get("/v1/usage/daily", response_model=DailyUsageResponse)
async def get_usage_daily_endpoint(
    query: Annotated[DailyUsageQueryParams, Query()],
    principal: AuthenticatedPrincipal = Depends(_require_authenticated_and_throttled),
) -> DailyUsageResponse:
    """Return the caller's own per-metric event counts for `date` (a UTC day).

    Customer-scoped, not admin-gated — any authenticated key may read its own
    daily summary, exactly like `GET /v1/usage`. A day with no events returns
    200 with `metrics: []`, never 404.
    """
    return await get_daily_usage(principal, query)
