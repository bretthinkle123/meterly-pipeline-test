"""Centralized edge middleware — security headers, CORS, the body-size guard,
and the Tier-1 (pre-auth, IP+route-keyed) throttle.

Registered once at app construction (`src/main.py`) in the order
`api-edge-conventions` specifies: request-id/trace, security headers, CORS,
body-size guard, Tier-1 throttle, auth guard, Tier-2 throttle, handler. No
route re-declares any of this — a new endpoint inherits it by being mounted
on the app.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.auth.rate_limit import get_tier1_limiter
from src.config.settings import Settings
from src.logging import get_logger

logger = get_logger(service="meterly")

_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Sets a fixed set of security headers on every response, including error responses."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Run the request, then stamp the security header set onto the response."""
        response = await call_next(request)
        for header_name, header_value in _SECURITY_HEADERS.items():
            response.headers[header_name] = header_value
        if request.url.path.startswith("/v1/usage"):
            # Usage totals are billing-adjacent personal-scoped data — never cache
            # them client-side or in an intermediary (ASVS 14.3.2).
            response.headers["Cache-Control"] = "no-store"
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects a request whose `Content-Length` exceeds the configured cap with 413.

    These payloads are tiny by design (a handful of scalar fields); an
    unbounded body is a cheap denial-of-service vector worth rejecting before
    it reaches any parsing.
    """

    def __init__(self, app: FastAPI, *, max_body_size_bytes: int) -> None:
        super().__init__(app)
        self._max_body_size_bytes = max_body_size_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Check `Content-Length` before invoking the rest of the stack."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_body_size_bytes:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": "request body exceeds the maximum allowed size",
                                "requestId": getattr(request.state, "request_id", "unknown"),
                            }
                        },
                    )
            except ValueError:
                pass  # malformed Content-Length falls through to normal request handling/validation
        return await call_next(request)


# Paths exempt from the Tier-1 Redis throttle: the liveness probe must return
# 200 with *zero* dependencies (no DB, no Redis) — it is the smoke check's and
# the build-fallback's only signal, and gating it on Redis would make a cache
# outage look like a total application outage (plan §"health split").
_TIER1_EXEMPT_PATHS = frozenset({"/health"})


class Tier1EdgeThrottleMiddleware(BaseHTTPMiddleware):
    """Pre-auth, IP+route-keyed Redis token bucket — sheds an anonymous flood
    before the request can spend an Argon2id verification (mitigates D1/D3).
    """

    def __init__(self, app: FastAPI, *, capacity: int, refill_rate_per_second: float) -> None:
        super().__init__(app)
        self._capacity = capacity
        self._refill_rate_per_second = refill_rate_per_second

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Consume one token from the `client_ip:route` bucket before proceeding.

        Fails **open** (allows the request, logs a warning) on a Redis
        connection error rather than 500ing every request — an unreachable
        cache degrading to "temporarily unlimited" is preferable to a full
        outage of the ingest path; Tier-2's per-key bucket and the underlying
        DB constraints remain the durable protections either way.
        """
        if request.url.path in _TIER1_EXEMPT_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        bucket_identity = f"{client_ip}:{request.url.path}"
        try:
            decision = await get_tier1_limiter().check(
                bucket_identity,
                capacity=self._capacity,
                refill_rate_per_second=self._refill_rate_per_second,
            )
        except Exception:  # noqa: BLE001 - a Redis outage must not take down the whole API
            logger.warning("ratelimit.backend_unavailable", endpoint=request.url.path, tier="tier1")
            return await call_next(request)

        if not decision.allowed:
            logger.warning(
                "ratelimit.exceeded",
                userId=None,
                action="deny",
                endpoint=request.url.path,
                reason="tier1_bucket_exhausted",
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "too many requests",
                        "requestId": getattr(request.state, "request_id", "unknown"),
                    }
                },
                headers={"Retry-After": str(max(1, int(decision.retry_after_seconds) or 1))},
            )
        return await call_next(request)


def configure_cors(app: FastAPI, settings: Settings) -> None:
    """Register the CORS middleware with an explicit origin allowlist from config.

    Defaults to an empty allowlist (this is a server-to-server API, not a
    browser client) rather than `*` — `api-edge-conventions` forbids
    reflecting an arbitrary origin.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
