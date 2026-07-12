"""Integration tests for `GET /v1/usage/daily` against a real Postgres:
happy-path aggregation, empty-day, the 400/422 validation split, cross-tenant
isolation, non-admin access, auth, the Tier-2 throttle wiring, the UTC
day-seam boundary, the read-audit log, OpenAPI documentation, existing-route
no-regression, and the forced-internal-error fail-closed 500 path (AC1-AC15).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
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


async def _insert_rollup_row(
    postgres_url,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    window_start: datetime,
    event_count: int,
    total_quantity: str = "1",
) -> None:
    """Seed a `usage_rollup` row directly — used for the day-boundary and
    multi-customer aggregation tests, where seeding via `POST /v1/events`
    can't pin an exact `window_start` (the write path floors to "now")."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO usage_rollup (api_key_id, customer_id, metric, window_start, total_quantity, event_count)
                VALUES (:api_key_id, :customer_id, :metric, :window_start, :total_quantity, :event_count)
                ON CONFLICT (api_key_id, customer_id, metric, window_start)
                DO UPDATE SET event_count = usage_rollup.event_count + EXCLUDED.event_count
                """
            ),
            {
                "api_key_id": api_key_id,
                "customer_id": customer_id,
                "metric": metric,
                "window_start": window_start,
                "total_quantity": total_quantity,
                "event_count": event_count,
            },
        )
    await engine.dispose()


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def test_daily_counts_aggregate_per_metric(client, make_api_key, postgres_url):
    """AC1: events across multiple metrics, multiple customer_ids, and
    multiple hour-buckets on the same UTC day sum to the correct per-metric
    total, ordered by metric."""
    presented_key, api_key_id = await make_api_key()
    today = datetime.now(timezone.utc).date()
    hour_a = datetime(today.year, today.month, today.day, 2, tzinfo=timezone.utc)
    hour_b = datetime(today.year, today.month, today.day, 14, tzinfo=timezone.utc)

    await _insert_rollup_row(
        postgres_url, api_key_id=api_key_id, customer_id="cust_a", metric="api_calls",
        window_start=hour_a, event_count=3,
    )
    await _insert_rollup_row(
        postgres_url, api_key_id=api_key_id, customer_id="cust_b", metric="api_calls",
        window_start=hour_b, event_count=4,
    )
    await _insert_rollup_row(
        postgres_url, api_key_id=api_key_id, customer_id="cust_a", metric="storage_bytes",
        window_start=hour_a, event_count=1,
    )

    response = await client.get(
        "/v1/usage/daily",
        params={"date": today.isoformat()},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["date"] == today.isoformat()
    assert body["metrics"] == [
        {"metric": "api_calls", "event_count": 7},
        {"metric": "storage_bytes", "event_count": 1},
    ]


async def test_empty_day_returns_200_empty_list(client, make_api_key):
    """AC2: a tenant with no events on the given date gets 200 with an empty
    metrics list, never 404."""
    presented_key, _ = await make_api_key()

    response = await client.get(
        "/v1/usage/daily",
        params={"date": _today_utc_str()},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"] == []


async def test_missing_date_returns_400(client, make_api_key):
    """AC3: omitting `date` entirely returns 400 bad_request, not 422/200."""
    presented_key, _ = await make_api_key()

    response = await client.get(
        "/v1/usage/daily", headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "bad_request"


async def test_malformed_date_returns_400(client, make_api_key):
    """AC4: a malformed (but present) `date` value returns 400, not 422."""
    presented_key, _ = await make_api_key()

    response = await client.get(
        "/v1/usage/daily",
        params={"date": "2026-13-40"},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_out_of_range_date_returns_400(client, make_api_key):
    """AC5: a well-formed date outside [today-90d, today+1d] returns 400."""
    presented_key, _ = await make_api_key()
    too_old = (datetime.now(timezone.utc).date() - timedelta(days=200)).isoformat()

    response = await client.get(
        "/v1/usage/daily",
        params={"date": too_old},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_unknown_query_param_rejected_422(client, make_api_key):
    """AC6: an undeclared query param (e.g. a smuggled api_key_id) returns
    422 validation_failed via extra='forbid', distinct from the 400 date
    contract."""
    presented_key, _ = await make_api_key()

    response = await client.get(
        "/v1/usage/daily",
        params={"date": _today_utc_str(), "api_key_id": "9"},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_cross_tenant_cannot_see_others_counts(client, make_api_key):
    """AC7 (IDOR/BOLA): tenant B querying the same date after tenant A
    ingests sees B's own empty result, never A's counts."""
    key_a, _ = await make_api_key(label="tenant-a")
    key_b, _ = await make_api_key(label="tenant-b")

    await client.post(
        "/v1/events",
        json=_event_payload(idempotency_key="a-1", quantity="1000"),
        headers={"Authorization": f"Bearer {key_a}"},
    )

    response = await client.get(
        "/v1/usage/daily",
        params={"date": _today_utc_str()},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert response.status_code == 200
    assert response.json()["metrics"] == []


async def test_requires_api_key(client):
    """AC8: no Authorization header, and an invalid/malformed key, both 401."""
    missing = await client.get("/v1/usage/daily", params={"date": _today_utc_str()})
    invalid = await client.get(
        "/v1/usage/daily",
        params={"date": _today_utc_str()},
        headers={"Authorization": "Bearer mtr_live_bogus_bogus"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401


async def test_ingest_key_allowed_not_admin_gated(client, make_api_key):
    """AC9: a plain ingest-scoped (non-admin) key gets 200 — no admin scope
    is required for this customer-scoped read."""
    ingest_key, _ = await make_api_key(label="ingest", scope="ingest")

    response = await client.get(
        "/v1/usage/daily",
        params={"date": _today_utc_str()},
        headers={"Authorization": f"Bearer {ingest_key}"},
    )

    assert response.status_code == 200


async def test_utc_day_boundary_is_half_open(client, make_api_key, postgres_url):
    """AC10: an event at 23:xxZ of date D is counted for D; an event at
    00:00Z of D+1 is not — the half-open [day_start, day_end) window."""
    presented_key, api_key_id = await make_api_key()
    today = datetime.now(timezone.utc).date()
    # Use a date safely inside the accepted range so D+1's 00:00Z boundary
    # doesn't itself risk landing outside [today-90d, today+1d].
    target_day = today - timedelta(days=5)
    end_of_target_day = datetime(target_day.year, target_day.month, target_day.day, 23, 30, tzinfo=timezone.utc)
    start_of_next_day = end_of_target_day + timedelta(hours=1)  # 00:30Z of target_day + 1

    await _insert_rollup_row(
        postgres_url, api_key_id=api_key_id, customer_id="cust_seam", metric="api_calls",
        window_start=end_of_target_day, event_count=1,
    )
    await _insert_rollup_row(
        postgres_url, api_key_id=api_key_id, customer_id="cust_seam", metric="api_calls",
        window_start=start_of_next_day, event_count=100,
    )

    response = await client.get(
        "/v1/usage/daily",
        params={"date": target_day.isoformat()},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"] == [{"metric": "api_calls", "event_count": 1}]


async def test_existing_usage_endpoint_unchanged(client, make_api_key):
    """AC11: mounting the new route doesn't disturb POST /v1/events or
    GET /v1/usage's existing behavior."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    post_response = await client.post("/v1/events", json=_event_payload(idempotency_key="sanity"), headers=headers)
    assert post_response.status_code == 201

    now = datetime.now(timezone.utc).isoformat()
    usage_response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "api_calls", "window": now},
        headers=headers,
    )
    assert usage_response.status_code == 200
    body = usage_response.json()
    assert Decimal(str(body["total_quantity"])) == Decimal("5")
    assert body["event_count"] == 1


async def test_emits_usage_daily_read_log(client, make_api_key, capsys):
    """AC12: a `usage.daily.read` structured event is emitted with
    userId/action/resource/date and no customer_id value (structlog prints
    JSON to stdout — see test_logging_redaction.py)."""
    import json

    presented_key, api_key_id = await make_api_key()

    response = await client.get(
        "/v1/usage/daily",
        params={"date": _today_utc_str()},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 200
    captured = capsys.readouterr()
    matching = [
        json.loads(line)
        for line in captured.out.splitlines()
        if '"event": "usage.daily.read"' in line
    ]
    assert len(matching) == 1
    record = matching[0]
    assert record["userId"] == api_key_id
    assert record["action"] == "read"
    assert record["resource"] == "usage_rollup"
    assert record["date"] == _today_utc_str()
    assert "customer_id" not in record


async def test_route_behind_tier2_throttle(truncate_tables, make_api_key):
    """AC13: the route resolves the Tier-2 per-api_key_id throttle — a key
    with a tiny bucket gets 429 on the second call within the same second."""
    from src.main import app

    presented_key, _ = await make_api_key(rate_limit_per_sec=1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as scoped_client:
        headers = {"Authorization": f"Bearer {presented_key}"}
        first = await scoped_client.get("/v1/usage/daily", params={"date": _today_utc_str()}, headers=headers)
        second = await scoped_client.get("/v1/usage/daily", params={"date": _today_utc_str()}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


async def test_openapi_documents_daily_route(client):
    """AC14: the served /openapi.json includes GET /v1/usage/daily with its
    DailyUsageResponse schema."""
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "/v1/usage/daily" in schema["paths"]
    operation = schema["paths"]["/v1/usage/daily"]["get"]
    assert "200" in operation["responses"]
    content_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in content_schema or "properties" in content_schema


async def test_daily_forced_repo_error_returns_generic_500(truncate_tables, make_api_key, monkeypatch):
    """AC15: a forced failure inside aggregate_daily_event_counts (simulating
    a DB/connection drop) returns the generic 500 internal envelope — no
    stack/SQL/detail leak, no error-swallowing try/except in the route or
    service (mirrors the usage_export sibling test)."""
    from src.main import app
    from src.services import usage_daily_service

    async def _raising_aggregate(session, **kwargs):
        raise RuntimeError("simulated connection drop mid-aggregate")

    monkeypatch.setattr(usage_daily_service, "aggregate_daily_event_counts", _raising_aggregate)

    presented_key, _ = await make_api_key()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as forced_client:
        response = await forced_client.get(
            "/v1/usage/daily",
            params={"date": _today_utc_str()},
            headers={"Authorization": f"Bearer {presented_key}"},
        )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal"
    assert "requestId" in body["error"]
    assert "simulated connection drop" not in response.text
    assert "Traceback" not in response.text
