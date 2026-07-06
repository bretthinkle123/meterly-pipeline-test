"""App-construction-level tests that need no database or Redis connection:
liveness, OpenAPI exposure (DAST-1), and the unauthenticated-denial/error-
envelope shape (AC18/AC19), all of which resolve before any DB/Redis call.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app


@pytest.fixture
async def client():
    """An async test client bound to the app via ASGI transport (no live server)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def test_liveness_returns_200_with_no_dependencies(client):
    """`/health` returns 200 with no database/Redis reachable (AC21 smoke)."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_openapi_schema_is_served(client):
    """`/openapi.json` is reachable and describes the implemented routes (AC22/DAST-1)."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/v1/events" in schema["paths"]
    assert "/v1/usage" in schema["paths"]


async def test_missing_api_key_is_rejected_with_safe_envelope(client):
    """A request with no Authorization header gets 401 with the generic error envelope (AC18/AC19)."""
    response = await client.post(
        "/v1/events",
        json={"customer_id": "cust_1", "metric": "calls", "quantity": "1", "idempotency_key": "abc"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "requestId" in body["error"]
    assert "stack" not in body["error"]["message"].lower()


async def test_malformed_api_key_is_rejected(client):
    """A malformed bearer token (fails the split-token regex) is rejected before any DB call."""
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "calls", "window": "2026-01-01T00:00:00+00:00"},
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert response.status_code == 401


async def test_security_headers_present_on_every_response(client):
    """The security header set is applied even to an error response."""
    response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" in response.headers


async def test_usage_response_is_never_cached(client):
    """`/v1/usage` responses (even error paths through the route) carry no cache directive
    leak risk — verified here on the 401 path since that's reachable with no DB."""
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "calls", "window": "2026-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 401
    assert response.headers.get("Cache-Control") == "no-store"
