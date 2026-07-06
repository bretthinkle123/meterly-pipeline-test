"""The error-envelope facade — every exception a handler raises terminates
here, at one centralized boundary, as the single response shape:
`{"error": {"code", "message", "requestId"}}`.

Never leaks a stack trace, exception type, SQL, or internal path to the
client (ASVS 16.5.1/16.5.3) — the detail is logged server-side and the client
gets a generic, stable `code` plus the `requestId` that ties a user report
back to the server log line.
"""

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.logging import get_logger

logger = get_logger(service="meterly")

_STATUS_TO_CODE = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_failed",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}


def _request_id(request: Request) -> str:
    """Read the correlation id the logging middleware assigned to this request."""
    return getattr(request.state, "request_id", "unknown")


def _envelope(code: str, message: str, request_id: str) -> dict:
    """Build the one response shape every error returns."""
    return {"error": {"code": code, "message": message, "requestId": request_id}}


def register_error_handlers(app: FastAPI) -> None:
    """Register the centralized exception handlers on `app`.

    Called once from `src/main.py` at app construction — no route ever builds
    its own error response; it raises and lets this boundary map it.
    """

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Map Pydantic/FastAPI validation failures to 422 with a safe message."""
        request_id = _request_id(request)
        logger.warning(
            "validation.failed",
            requestId=request_id,
            endpoint=request.url.path,
            errorCount=len(exc.errors()),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope("validation_failed", "the request failed validation", request_id),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Map a raised `HTTPException` (auth, rate-limit, not-found, ...) to the envelope."""
        request_id = _request_id(request)
        code = _STATUS_TO_CODE.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, message, request_id),
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all: log the full detail server-side, return a generic 500 to the client.

        This is the fail-closed boundary — an unhandled exception mid-write
        never leaks stack/SQL/secret detail, and because the events + rollup
        write happens in one transaction (`src/services/events_service.py`),
        an exception here means that transaction was rolled back, not
        partially applied.
        """
        request_id = _request_id(request)
        logger.error(
            "request.unhandled_error",
            requestId=request_id,
            endpoint=request.url.path,
            **{"error.type": type(exc).__name__, "error.message": str(exc)},
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal", "an internal error occurred", request_id),
        )


__all__ = ["register_error_handlers", "HTTPException"]
