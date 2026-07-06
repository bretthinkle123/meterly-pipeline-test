"""Integration tests for `POST /v1/events` against a real Postgres + Redis
(AC1, AC2, AC3, AC13-wire, AC18, AC19)."""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


def _payload(**overrides) -> dict:
    payload = {
        "customer_id": "cust_1",
        "metric": "api_calls",
        "quantity": "5",
        "idempotency_key": "idem-key-1",
    }
    payload.update(overrides)
    return payload


async def test_post_event_returns_201_and_persists_one_row(client, make_api_key, postgres_url):
    """AC1: valid key + body -> 201, exactly one `events` row, minimal response fields."""
    presented_key, api_key_id = await make_api_key()

    response = await client.post(
        "/v1/events",
        json=_payload(),
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) == {"event_id", "customer_id", "metric", "quantity", "window_start", "idempotent_replay"}
    assert body["idempotent_replay"] is False
    assert body["customer_id"] == "cust_1"
    assert Decimal(str(body["quantity"])) == Decimal("5")

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        count = (await connection.execute(text("SELECT COUNT(*) FROM events WHERE api_key_id = :id"), {"id": api_key_id})).scalar_one()
    await engine.dispose()
    assert count == 1


async def test_duplicate_idempotency_key_is_a_no_op(client, make_api_key, postgres_url):
    """AC2: a replayed idempotency_key creates no new row and does not double-increment the rollup."""
    presented_key, api_key_id = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    first = await client.post("/v1/events", json=_payload(), headers=headers)
    assert first.status_code == 201

    second = await client.post("/v1/events", json=_payload(quantity="999"), headers=headers)
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        event_count = (
            await connection.execute(text("SELECT COUNT(*) FROM events WHERE api_key_id = :id"), {"id": api_key_id})
        ).scalar_one()
        rollup_row = (
            await connection.execute(
                text(
                    "SELECT total_quantity, event_count FROM usage_rollup WHERE api_key_id = :id"
                ),
                {"id": api_key_id},
            )
        ).mappings().first()
    await engine.dispose()

    assert event_count == 1
    assert rollup_row["event_count"] == 1
    assert Decimal(rollup_row["total_quantity"]) == Decimal("5")


async def test_replay_body_pins_the_original_event_representation(client, make_api_key):
    """AC3: the replay response equals the *original* event, not the duplicate's payload,
    even when the duplicate carries different values (replay semantics pinned)."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    first = await client.post("/v1/events", json=_payload(quantity="5"), headers=headers)
    first_body = first.json()

    second = await client.post(
        "/v1/events",
        json=_payload(quantity="9999", customer_id="cust_1"),
        headers=headers,
    )
    second_body = second.json()

    assert second_body["event_id"] == first_body["event_id"]
    assert Decimal(str(second_body["quantity"])) == Decimal(str(first_body["quantity"])) == Decimal("5")
    assert second_body["idempotent_replay"] is True
    assert first_body["idempotent_replay"] is False


async def test_injection_payload_in_customer_id_is_rejected_at_the_boundary(client, make_api_key):
    """AC13-wire: an injection payload never reaches the SQL sink — 422 at the schema boundary."""
    presented_key, _ = await make_api_key()
    response = await client.post(
        "/v1/events",
        json=_payload(customer_id="' OR 1=1--"),
        headers={"Authorization": f"Bearer {presented_key}"},
    )
    assert response.status_code == 422


async def test_wrong_secret_for_a_known_key_id_is_rejected(client, make_api_key):
    """AC18: a well-formed token with a valid key_id but the wrong secret is
    denied (401), indistinguishable from an unknown key_id."""
    presented_key, _ = await make_api_key()
    key_id = presented_key.split("_")[2]
    tampered_key = f"mtr_live_{key_id}_{'0' * 32}"

    response = await client.post(
        "/v1/events",
        json=_payload(),
        headers={"Authorization": f"Bearer {tampered_key}"},
    )
    assert response.status_code == 401


async def test_revoked_api_key_is_rejected(client, make_api_key, postgres_url):
    """AC18: a revoked key is denied (401), never 200."""
    presented_key, api_key_id = await make_api_key()

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE api_keys SET revoked_at = now() WHERE id = :id"), {"id": api_key_id}
        )
    await engine.dispose()

    response = await client.post(
        "/v1/events",
        json=_payload(),
        headers={"Authorization": f"Bearer {presented_key}"},
    )
    assert response.status_code == 401


async def test_forced_internal_error_returns_safe_envelope_and_fails_closed(
    truncate_tables, make_api_key, postgres_url, monkeypatch
):
    """AC19: a forced internal error returns the generic envelope (no stack/SQL leak)
    and leaves no partial write behind (event + rollup are one transaction)."""
    presented_key, api_key_id = await make_api_key()

    from src.services import events_service

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-write")

    monkeypatch.setattr(events_service, "increment_usage_rollup", _boom)

    # ASGITransport re-raises app exceptions by default (useful for debugging
    # a broken test); here we're deliberately exercising the app's own 500
    # handler, so this client must let it convert the exception to a response.
    from src.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/events",
            json=_payload(idempotency_key="will-fail"),
            headers={"Authorization": f"Bearer {presented_key}"},
        )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal"
    assert "requestId" in body["error"]
    message = body["error"]["message"].lower()
    assert "runtimeerror" not in message
    assert "simulated failure" not in message
    assert "traceback" not in message

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        event_count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM events WHERE api_key_id = :id AND idempotency_key = 'will-fail'"),
                {"id": api_key_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert event_count == 0, "the failed transaction must not leave a partial event row"
