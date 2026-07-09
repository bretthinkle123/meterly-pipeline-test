"""`PUT /v1/quotas` — admin-scoped create-or-replace of a per-customer,
per-metric usage cap.

Edge behavior (security headers, CORS, body-size guard, Tier-1 throttle) is
inherited from the middleware stack; this route wires
auth -> Tier-2 throttle -> admin-scope check -> schema validation -> service,
mirroring the `events`/`usage` routes' sibling `_require_authenticated_and_throttled`
pattern (kept per-route, not shared, per the existing convention).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from src.api.schemas.quotas import QuotaPutRequest, QuotaResponse
from src.auth import require_api_key
from src.auth.api_key import AuthenticatedPrincipal
from src.auth.rate_limit import enforce_tier2_rate_limit
from src.logging import get_logger
from src.services.quota_service import to_response, upsert_tenant_quota

router = APIRouter(tags=["quotas"])
logger = get_logger(service="meterly")

_ADMIN_SCOPE = "admin"


async def _require_admin_and_throttled(
    request: Request, principal: AuthenticatedPrincipal = Depends(require_api_key)
) -> AuthenticatedPrincipal:
    """Compose auth, then the Tier-2 per-key throttle, then the admin-scope
    gate, in that order — mirrors `src/api/routes/events.py`'s dependency
    chain, extended with the function-level authorization check this route
    alone requires (ASVS 8.2.1).
    """
    await enforce_tier2_rate_limit(request, principal)
    if principal.scope != _ADMIN_SCOPE:
        logger.warning(
            "quota.forbidden",
            userId=principal.api_key_id,
            action="deny",
            reason="insufficient_scope",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin scope required")
    return principal


@router.put("/v1/quotas", response_model=QuotaResponse)
async def put_quota(
    payload: QuotaPutRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(_require_admin_and_throttled),
) -> QuotaResponse:
    """Create or replace the cap for `(customer_id, metric)` under the
    caller's own tenant — 201 on create, 200 on replace."""
    outcome = await upsert_tenant_quota(principal, payload)
    response.status_code = outcome.http_status
    return to_response(outcome)
