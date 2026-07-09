"""`GET /v1/usage` — the aggregated usage counter for a customer/metric/window."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from src.api.schemas.usage import UsageQueryParams, UsageResponse
from src.auth import require_api_key
from src.auth.api_key import AuthenticatedPrincipal
from src.auth.rate_limit import enforce_tier2_rate_limit
from src.services.usage_service import get_usage

router = APIRouter(tags=["usage"])


async def _require_authenticated_and_throttled(
    request: Request, principal: AuthenticatedPrincipal = Depends(require_api_key)
) -> AuthenticatedPrincipal:
    """Compose auth then the Tier-2 per-key throttle, in that order (mirrors
    `src/api/routes/events.py` — kept as a sibling, not a shared import, so
    each route's dependency chain stays independently readable)."""
    await enforce_tier2_rate_limit(request, principal)
    return principal


@router.get("/v1/usage", response_model=UsageResponse)
async def get_usage_endpoint(
    query: Annotated[UsageQueryParams, Query()],
    principal: AuthenticatedPrincipal = Depends(_require_authenticated_and_throttled),
) -> UsageResponse:
    """Return the aggregated `{total_quantity, event_count}` for the caller's
    own `(customer_id, metric, window)` bucket. A bucket with no data returns
    zeros with 200, never 404."""
    return await get_usage(principal, query)
