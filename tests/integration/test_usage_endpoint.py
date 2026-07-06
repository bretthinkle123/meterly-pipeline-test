"""Integration tests for `GET /v1/usage` against a real Postgres (AC4, AC5,
AC15-wire, AC17 tenant isolation)."""

from datetime import datetime, timezone
from decimal import Decimal


def _event_payload(**overrides) -> dict:
    payload = {
        "customer_id": "cust_1",
        "metric": "api_calls",
        "quantity": "5",
        "idempotency_key": "idem-1",
    }
    payload.update(overrides)
    return payload


async def test_usage_returns_correct_aggregate_for_seeded_events(client, make_api_key):
    """AC4: two events in the same hour bucket aggregate to the correct total/count."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post("/v1/events", json=_event_payload(idempotency_key="a"), headers=headers)
    await client.post("/v1/events", json=_event_payload(idempotency_key="b", quantity="3"), headers=headers)

    now = datetime.now(timezone.utc).isoformat()
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "api_calls", "window": now},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["total_quantity"])) == Decimal("8")
    assert body["event_count"] == 2


async def test_usage_for_empty_bucket_returns_200_with_zeros(client, make_api_key):
    """AC5: a window with no data returns 200 with zeros, never 404."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    now = datetime.now(timezone.utc).isoformat()
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_no_data", "metric": "api_calls", "window": now},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["total_quantity"])) == Decimal("0")
    assert body["event_count"] == 0


async def test_naive_datetime_window_is_rejected(client, make_api_key):
    """AC15-wire: a naive (no-offset) window is rejected with 422 through the live route."""
    presented_key, _ = await make_api_key()
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "api_calls", "window": "2026-01-01T00:00:00"},
        headers={"Authorization": f"Bearer {presented_key}"},
    )
    assert response.status_code == 422


async def test_cross_owner_cannot_read_anothers_usage(client, make_api_key):
    """AC17 (IDOR/BOLA): key B querying key A's customer_id gets B's own zero,
    never A's totals — proves the api_key_id scoping (+ RLS backstop) actually isolates."""
    key_a, _ = await make_api_key(label="tenant-a")
    key_b, _ = await make_api_key(label="tenant-b")

    await client.post(
        "/v1/events",
        json=_event_payload(idempotency_key="a-1", quantity="1000"),
        headers={"Authorization": f"Bearer {key_a}"},
    )

    now = datetime.now(timezone.utc).isoformat()
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "api_calls", "window": now},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["total_quantity"])) == Decimal("0")
    assert body["event_count"] == 0


async def test_cross_owner_idempotency_key_is_scoped_per_tenant(client, make_api_key):
    """AC17 write-path: the same idempotency_key string used by two different
    keys creates two independent events, not a cross-tenant collision."""
    key_a, api_key_id_a = await make_api_key(label="tenant-a")
    key_b, api_key_id_b = await make_api_key(label="tenant-b")

    response_a = await client.post(
        "/v1/events",
        json=_event_payload(idempotency_key="shared-key"),
        headers={"Authorization": f"Bearer {key_a}"},
    )
    response_b = await client.post(
        "/v1/events",
        json=_event_payload(idempotency_key="shared-key"),
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert response_a.status_code == 201
    assert response_b.status_code == 201
    assert response_a.json()["event_id"] != response_b.json()["event_id"]
