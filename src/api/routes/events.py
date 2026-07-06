"""`POST /v1/events` — record a metered event, idempotent on `idempotency_key`.

Edge behavior (security headers, CORS, body-size guard, Tier-1 throttle) is
inherited from the middleware stack; this route only wires
auth -> Tier-2 throttle -> schema validation -> service.
"""

from fastapi import APIRouter, Depends, Request, Response

from src.api.schemas.events import EventCreateRequest, EventResponse
from src.auth import require_api_key
from src.auth.api_key import AuthenticatedPrincipal
from src.auth.rate_limit import enforce_tier2_rate_limit
from src.services.events_service import create_event, to_response

router = APIRouter(tags=["events"])


async def _require_authenticated_and_throttled(
    request: Request, principal: AuthenticatedPrincipal = Depends(require_api_key)
) -> AuthenticatedPrincipal:
    """Compose auth then the Tier-2 per-key throttle, in that order.

    A single dependency function pins the ordering FastAPI resolves
    dependencies in: `require_api_key` must complete (and thus populate
    `principal`) before `enforce_tier2_rate_limit` can key its bucket on it.
    """
    await enforce_tier2_rate_limit(request, principal)
    return principal


@router.post("/v1/events", response_model=EventResponse, status_code=201)
async def post_event(
    payload: EventCreateRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(_require_authenticated_and_throttled),
) -> EventResponse:
    """Record `payload` as a new event, or return the original result if
    `idempotency_key` was already seen for this caller (200, no-op)."""
    outcome = await create_event(principal, payload)
    response.status_code = outcome.http_status
    return to_response(outcome)
