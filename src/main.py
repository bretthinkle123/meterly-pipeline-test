"""Application entry point: constructs the FastAPI app, registers the
middleware stack in the order `api-edge-conventions` requires, mounts the
routers, and wires the lifespan startup/shutdown hooks.

Deliberately defers every *connecting* dependency (DB pool, Redis client) to
first use rather than import time, so `python -c "import src.main"` succeeds
with no PostgreSQL or Redis running — the smoke check's build-fallback mode
depends on this. OTel/Sentry instrumentation is wired during app construction
instead of inside the lifespan startup hook: Starlette forbids registering
ASGI middleware (which `FastAPIInstrumentor` does) once the app has started
serving, and neither call performs a blocking network operation at
configuration time — exporting is async/background and a no-op when
unconfigured (`src/observability/otel.py`, `src/observability/sentry.py`).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.errors import register_error_handlers
from src.api.middleware import (
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    Tier1EdgeThrottleMiddleware,
    configure_cors,
)
from src.api.routes.events import router as events_router
from src.api.routes.health import router as health_router
from src.api.routes.usage import router as usage_router
from src.config.settings import get_settings
from src.db.session import dispose_engine
from src.logging import get_logger
from src.logging.middleware import RequestContextMiddleware
from src.observability.otel import configure_otel
from src.observability.sentry import configure_sentry

logger = get_logger(service="meterly")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: log readiness (DB/Redis pools are created lazily on first use,
    not here, so a slow/unreachable dependency at boot never blocks the
    process from becoming live). Shutdown: drain and close pools.
    """
    settings = get_settings()
    logger.info("app.startup", environment=settings.environment, release=settings.release_sha)

    yield

    logger.info("app.shutdown")
    await dispose_engine()
    from src.auth.rate_limit import dispose_redis_client

    await dispose_redis_client()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application (the single app-construction
    entry point — nothing outside this function registers middleware or routes)."""
    settings = get_settings()

    app = FastAPI(
        title="Meterly",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
    )

    # Middleware is applied in reverse-registration order by Starlette (the
    # last `add_middleware` call becomes the outermost layer), so this list
    # is written outermost-to-innermost per api-edge-conventions and then
    # reversed at registration to preserve that intent.
    app.add_middleware(Tier1EdgeThrottleMiddleware, capacity=settings.tier1_rate_limit_burst, refill_rate_per_second=settings.tier1_rate_limit_per_second)
    app.add_middleware(BodySizeLimitMiddleware, max_body_size_bytes=settings.max_body_size_bytes)
    configure_cors(app, settings)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(events_router)
    app.include_router(usage_router)

    # Must run after routes are mounted (FastAPIInstrumentor introspects them)
    # and before the app starts serving (it registers ASGI middleware, which
    # Starlette forbids once serving has begun) — so construction time, not
    # the lifespan hook, is the correct place for this.
    configure_sentry(settings)
    configure_otel(app, settings)

    return app


app = create_app()
