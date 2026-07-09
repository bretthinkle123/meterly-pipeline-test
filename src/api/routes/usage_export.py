"""`GET /v1/usage/export` — streams the authenticated caller's own
`usage_rollup` rows as an RFC 4180 CSV.

Edge behavior (security headers, CORS, body-size guard, Tier-1 throttle) is
inherited from the middleware stack; this route wires
auth -> Tier-2 throttle -> schema validation -> a two-phase pre-flight-cap +
stream service call, mirroring `src/api/routes/usage.py`'s composition. Kept
in its own module (not added to `usage.py`) so `GET /v1/usage`'s code path is
literally untouched by this change — the brief's no-behavior-change
constraint is then trivially auditable in the diff.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from src.api.schemas.usage_export import UsageExportQueryParams
from src.auth import require_api_key
from src.auth.api_key import AuthenticatedPrincipal
from src.auth.rate_limit import enforce_tier2_rate_limit
from src.services.usage_export_service import prepare_export, stream_export_csv

router = APIRouter(tags=["usage"])

# Explicit OpenAPI response metadata: a StreamingResponse has no `response_model`
# for FastAPI to introspect, so the served schema would otherwise omit the real
# response shape — this is what a DAST scanner (and AC19) reads.
_EXPORT_RESPONSES: dict = {
    200: {
        "description": "The caller's usage_rollup rows as RFC 4180 CSV (header row always first).",
        "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
    },
    422: {
        "description": "Validation failure, or the filtered result exceeds the 100,000-row export cap.",
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": "validation_failed",
                        "message": "result exceeds 100000 rows; narrow with customer_id, metric, from, and/or to",
                        "requestId": "…",
                    }
                }
            }
        },
    },
}


async def _require_authenticated_and_throttled(
    request: Request, principal: AuthenticatedPrincipal = Depends(require_api_key)
) -> AuthenticatedPrincipal:
    """Compose auth then the Tier-2 per-key throttle, in that order (kept as
    a sibling, not a shared import, per the existing per-route convention —
    each route's dependency chain stays independently readable)."""
    await enforce_tier2_rate_limit(request, principal)
    return principal


def _export_filename(now: datetime) -> str:
    """Build the `Content-Disposition` filename: a UTC timestamp only — no
    tenant identifier, so the filename itself never leaks `api_key_id` or any
    caller identity (ASVS 14.3.2)."""
    return f"usage-export-{now:%Y%m%dT%H%M%SZ}.csv"


@router.get("/v1/usage/export", responses=_EXPORT_RESPONSES)
async def get_usage_export(
    params: Annotated[UsageExportQueryParams, Query()],
    principal: AuthenticatedPrincipal = Depends(_require_authenticated_and_throttled),
) -> StreamingResponse:
    """Stream the caller's own `usage_rollup` rows as CSV, optionally
    narrowed by `customer_id`/`metric`/`from`/`to`.

    Two-phase: `prepare_export` runs the pre-flight row-cap check (a clean
    422 over 100,000 rows, or a fail-closed generic 500 on an unexpected
    error — AC22) before any response byte; only then is the
    `StreamingResponse` constructed. Starlette pulls `stream_export_csv`
    after this handler returns, so the generator opens its own DB
    transaction rather than reusing one already closed by the time it runs.
    """
    await prepare_export(principal, params)

    filename = _export_filename(datetime.now(timezone.utc))
    return StreamingResponse(
        stream_export_csv(principal, params),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
