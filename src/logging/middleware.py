"""Request-scoped logging middleware: assigns `requestId`/`traceId`, binds them
into structlog's contextvars so every log line emitted during the request
carries them, and logs one completion event with `duration`/`statusCode`.

Registered first in the middleware stack (`src/api/middleware.py`) — outermost —
so every mitigation below it (security headers, throttles, auth, the handler)
logs with a correlation id already attached.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.logging import get_logger

logger = get_logger(service="meterly")

_AWS_TRACE_HEADER = "x-amzn-trace-id"


def _resolve_trace_id(request: Request) -> str:
    """Resolve a trace id in priority order: active OTel span, then the AWS
    `X-Amzn-Trace-Id` header, then a freshly generated UUID."""
    span = trace.get_current_span()
    span_context = span.get_span_context()
    if span_context is not None and span_context.is_valid:
        return format(span_context.trace_id, "032x")

    aws_header = request.headers.get(_AWS_TRACE_HEADER)
    if aws_header:
        for segment in aws_header.split(";"):
            if segment.strip().startswith("Root="):
                return segment.strip()[len("Root=") :]

    return str(uuid.uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds `requestId`/`traceId` to the logging context for the lifetime of
    a request and emits one structured completion log per request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Assign correlation ids, run the request, and log its outcome."""
        request_id = str(uuid.uuid4())
        trace_id = _resolve_trace_id(request)
        started_at = time.monotonic()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(requestId=request_id, traceId=trace_id)
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.monotonic() - started_at) * 1000, 2)
            logger.error(
                "request.failed",
                operation=f"{request.method} {request.url.path}",
                duration=duration_ms,
                method=request.method,
                endpoint=request.url.path,
            )
            raise

        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "request.completed",
            operation=f"{request.method} {request.url.path}",
            statusCode=response.status_code,
            duration=duration_ms,
            method=request.method,
            endpoint=request.url.path,
        )
        return response
