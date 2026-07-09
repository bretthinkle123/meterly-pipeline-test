"""Integration tests for the `POST /v1/events` quota check against a real
Postgres + Redis (AC7, AC8, AC9, AC10, AC11, AC12, AC14, AC18-read-path, AC19-rejected)."""

import json

from sqlalchemy import text


def _event_payload(**overrides) -> dict:
    payload = {
        "customer_id": "cust_1",
        "metric": "api_calls",
        "quantity": "5",
        "idempotency_key": "idem-1",
    }
    payload.update(overrides)
    return payload


def _quota_payload(**overrides) -> dict:
    payload = {"customer_id": "cust_1", "metric": "api_calls", "limit_per_window": 10}
    payload.update(overrides)
    return payload


async def _set_quota(client, admin_key: str, **overrides) -> None:
    response = await client.put(
        "/v1/quotas", json=_quota_payload(**overrides), headers={"Authorization": f"Bearer {admin_key}"}
    )
    assert response.status_code in (200, 201), response.text


async def test_no_quota_is_unlimited(client, make_api_key):
    """AC7: no quota row for (customer, metric) -> unchanged behavior, 201."""
    admin_key, _ = await make_api_key(scope="admin")

    response = await client.post(
        "/v1/events", json=_event_payload(quantity="1000000"), headers={"Authorization": f"Bearer {admin_key}"}
    )
    assert response.status_code == 201


async def test_under_limit_accepted(client, make_api_key, postgres_url):
    """AC8: R + Q <= L -> 201 accepted, rollup increments normally."""
    admin_key, api_key_id = await make_api_key(scope="admin")
    await _set_quota(client, admin_key, limit_per_window=100)

    response = await client.post(
        "/v1/events", json=_event_payload(quantity="5"), headers={"Authorization": f"Bearer {admin_key}"}
    )
    assert response.status_code == 201

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rollup = (
            await connection.execute(
                text("SELECT total_quantity FROM usage_rollup WHERE api_key_id = :id"), {"id": api_key_id}
            )
        ).mappings().first()
    await engine.dispose()
    assert rollup is not None
    from decimal import Decimal

    assert Decimal(rollup["total_quantity"]) == Decimal("5")


async def test_over_limit_429_and_no_partial_write(client, make_api_key, postgres_url):
    """AC9: R + Q > L -> 429 quota_exceeded with Retry-After; no event row, no rollup increment (rolled back)."""
    admin_key, api_key_id = await make_api_key(scope="admin")
    await _set_quota(client, admin_key, limit_per_window=10)

    accepted = await client.post(
        "/v1/events",
        json=_event_payload(quantity="8", idempotency_key="accepted-1"),
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert accepted.status_code == 201

    rejected = await client.post(
        "/v1/events",
        json=_event_payload(quantity="5", idempotency_key="rejected-1"),
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "quota_exceeded"
    assert "Retry-After" in rejected.headers
    assert int(rejected.headers["Retry-After"]) >= 1

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rejected_event_count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM events WHERE api_key_id = :id AND idempotency_key = 'rejected-1'"),
                {"id": api_key_id},
            )
        ).scalar_one()
        rollup = (
            await connection.execute(
                text("SELECT total_quantity FROM usage_rollup WHERE api_key_id = :id"), {"id": api_key_id}
            )
        ).mappings().first()
    await engine.dispose()

    assert rejected_event_count == 0, "a rejected event must leave no trace (transaction rolled back)"
    from decimal import Decimal

    assert Decimal(rollup["total_quantity"]) == Decimal("8"), "the rollup must reflect only the accepted event"


async def test_empty_window_q_gt_l_rejected(client, make_api_key, postgres_url):
    """AC10: Q > L against an empty window (no prior rollup row) -> 429; no rollup row created."""
    admin_key, api_key_id = await make_api_key(scope="admin")
    await _set_quota(client, admin_key, limit_per_window=3)

    response = await client.post(
        "/v1/events", json=_event_payload(quantity="5"), headers={"Authorization": f"Bearer {admin_key}"}
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rollup = (
            await connection.execute(
                text("SELECT COUNT(*) FROM usage_rollup WHERE api_key_id = :id"), {"id": api_key_id}
            )
        ).scalar_one()
    await engine.dispose()
    assert rollup == 0, "no rollup row should be created for a rejected first event in an empty window"


async def test_replay_over_quota_returns_200_no_usage(client, make_api_key, postgres_url):
    """AC11: a replay of a previously accepted event, window now over quota,
    returns 200 (the original replay) — the quota is never consulted, no usage added."""
    admin_key, api_key_id = await make_api_key(scope="admin")
    await _set_quota(client, admin_key, limit_per_window=10)

    first = await client.post(
        "/v1/events",
        json=_event_payload(quantity="8", idempotency_key="replay-key"),
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert first.status_code == 201

    # Lower the quota below what's already accumulated, so a *fresh* event
    # would now be rejected -- but a replay of the same idempotency_key must
    # still succeed with the original result, untouched by the new quota.
    await _set_quota(client, admin_key, limit_per_window=1)

    replay = await client.post(
        "/v1/events",
        json=_event_payload(quantity="8", idempotency_key="replay-key"),
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rollup = (
            await connection.execute(
                text("SELECT total_quantity, event_count FROM usage_rollup WHERE api_key_id = :id"),
                {"id": api_key_id},
            )
        ).mappings().first()
    await engine.dispose()

    from decimal import Decimal

    assert rollup["event_count"] == 1, "a replay must never add usage"
    assert Decimal(rollup["total_quantity"]) == Decimal("8")


async def test_midwindow_change_takes_effect(client, make_api_key):
    """AC12: lowering L mid-window (L <= R already accumulated) blocks the
    rest of the window immediately, against a fresh read each POST."""
    admin_key, _ = await make_api_key(scope="admin")
    await _set_quota(client, admin_key, limit_per_window=100)

    first = await client.post(
        "/v1/events",
        json=_event_payload(quantity="20", idempotency_key="midwindow-1"),
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert first.status_code == 201

    # Lower the cap below the already-accumulated total (20).
    await _set_quota(client, admin_key, limit_per_window=10)

    second = await client.post(
        "/v1/events",
        json=_event_payload(quantity="1", idempotency_key="midwindow-2"),
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "quota_exceeded"


async def test_throttle_precedes_quota_distinct_codes(truncate_tables, make_api_key):
    """AC14: the Tier-2 per-key throttle fires (code: rate_limited) before any
    quota logic runs; a genuine quota rejection uses the distinct quota_exceeded code."""
    from httpx import ASGITransport, AsyncClient

    from src.main import app

    throttled_key, _ = await make_api_key(label="throttled", rate_limit_per_sec=1, scope="admin")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        first = await test_client.post(
            "/v1/events", json=_event_payload(idempotency_key="t-1"), headers={"Authorization": f"Bearer {throttled_key}"}
        )
        second = await test_client.post(
            "/v1/events", json=_event_payload(idempotency_key="t-2"), headers={"Authorization": f"Bearer {throttled_key}"}
        )
        assert first.status_code == 201
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"

        quota_key, _ = await make_api_key(label="quota-key", rate_limit_per_sec=1000, scope="admin")
        await test_client.put(
            "/v1/quotas",
            json=_quota_payload(limit_per_window=1),
            headers={"Authorization": f"Bearer {quota_key}"},
        )
        rejected = await test_client.post(
            "/v1/events",
            json=_event_payload(quantity="5", idempotency_key="q-1"),
            headers={"Authorization": f"Bearer {quota_key}"},
        )
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "quota_exceeded"


async def test_rejection_is_logged_without_totals(client, make_api_key, capsys):
    """AC19: a 429 quota rejection logs `quota.rejected` (WARNING) without
    the current_total/limit numbers (structlog prints JSON to stdout)."""
    admin_key, _ = await make_api_key(scope="admin")
    await _set_quota(client, admin_key, limit_per_window=1)

    response = await client.post(
        "/v1/events", json=_event_payload(quantity="5"), headers={"Authorization": f"Bearer {admin_key}"}
    )
    assert response.status_code == 429

    captured = capsys.readouterr()
    rejected_lines = [line for line in captured.out.splitlines() if "quota.rejected" in line]
    assert len(rejected_lines) == 1

    rejection_record = json.loads(rejected_lines[0])
    assert rejection_record["reason"] == "quota_exceeded"
    assert "current_total" not in rejection_record
    assert "limit_per_window" not in rejection_record
    assert "limit" not in rejection_record
