"""`/dashboard` — the served SCREEN-1 page, its static assets, and the
same-origin BFF that assembles the usage series in-process via `get_usage`.

All routes are mounted on the existing app and inherit its middleware stack
(request-id, security headers, CORS, body-size guard, Tier-1 throttle, error
envelope) — none of it is re-declared here (`api-edge-conventions` edge-
facade rule). `GET /dashboard` is deliberately input-free — filters live only
in the BFF's query string — so there is exactly one untrusted input surface
to validate (`UsageSeriesQueryParams`).
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from src.api.schemas.dashboard import ConfigResponse, UsageSeriesQueryParams, UsageSeriesResponse
from src.config.settings import get_settings
from src.services.dashboard_service import get_usage_series

router = APIRouter(tags=["dashboard"])

# A fixed set of three known assets served by explicit routes below — never a
# `StaticFiles` mount, which would serve any file under a user-controlled
# path (path-traversal surface, ASVS V5). Explicit routes eliminate that
# surface entirely (plan §"Stack notes").
_STATIC_DIR = Path(__file__).resolve().parents[2] / "web" / "static"


@router.get("/dashboard")
async def serve_dashboard_page() -> FileResponse:
    """Serve the SCREEN-1 HTML shell. Carries no query/path input of its own."""
    return FileResponse(_STATIC_DIR / "dashboard.html", media_type="text/html")


@router.get("/dashboard/static/dashboard.css")
async def serve_dashboard_css() -> FileResponse:
    """Serve the page's stylesheet from a fixed, known path."""
    return FileResponse(_STATIC_DIR / "dashboard.css", media_type="text/css")


@router.get("/dashboard/static/dashboard.js")
async def serve_dashboard_js() -> FileResponse:
    """Serve the page's vanilla-JS controller from a fixed, known path."""
    return FileResponse(_STATIC_DIR / "dashboard.js", media_type="application/javascript")


@router.get("/dashboard/api/config", response_model=ConfigResponse)
async def get_dashboard_config() -> ConfigResponse:
    """Return the dropdown option lists + environment badge value from the
    single server-side `Settings` source of truth (`CMP-2`/`CMP-3`, AC20) —
    the same allowlists `UsageSeriesQueryParams` validates against, so the
    page's options and the BFF's validation can never drift apart."""
    settings = get_settings()
    return ConfigResponse(
        customers=list(settings.dashboard_customers),
        metrics=list(settings.dashboard_metrics),
        granularities=list(settings.dashboard_granularities),
        environment=settings.environment,
    )


@router.get("/dashboard/api/usage-series", response_model=UsageSeriesResponse)
async def get_dashboard_usage_series(
    query: Annotated[UsageSeriesQueryParams, Query()],
) -> UsageSeriesResponse:
    """Assemble the current-usage + last-10-windows + deltas series for the
    dashboard's stat card and table (`CMP-5`..`CMP-8`), reading `get_usage`
    in-process via the server-held `dashboard-reader` principal — no client
    credential is ever required or accepted on this route."""
    return await get_usage_series(
        customer_id=query.customer_id, metric=query.metric, granularity=query.granularity
    )
