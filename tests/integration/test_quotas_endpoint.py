"""Integration tests for `PUT /v1/quotas` against a real Postgres + Redis
(AC1, AC2, AC3, AC4, AC5, AC6, AC15, AC18-write-path, AC19-upsert/forbidden,
AC21-OpenAPI-presence)."""

from sqlalchemy import text


def _payload(**overrides) -> dict:
    payload = {"customer_id": "cust_1", "metric": "api_calls", "limit_per_window": 1000}
    payload.update(overrides)
    return payload


async def test_put_creates_quota_returns_201_and_echoes(client, make_api_key, postgres_url):
    """AC1: admin key + valid body -> 201, echoes the stored row, exactly one `quotas` row."""
    presented_key, api_key_id = await make_api_key(scope="admin")

    response = await client.put(
        "/v1/quotas", json=_payload(), headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body == {"customer_id": "cust_1", "metric": "api_calls", "limit_per_window": 1000}

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM quotas WHERE api_key_id = :id"), {"id": api_key_id}
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 1


async def test_put_replace_returns_200_single_row(client, make_api_key, postgres_url):
    """AC2: replacing an existing (customer_id, metric) -> 200, limit updated, still one row."""
    presented_key, api_key_id = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}

    first = await client.put("/v1/quotas", json=_payload(limit_per_window=1000), headers=headers)
    assert first.status_code == 201

    second = await client.put("/v1/quotas", json=_payload(limit_per_window=5000), headers=headers)
    assert second.status_code == 200
    assert second.json()["limit_per_window"] == 5000

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT limit_per_window FROM quotas WHERE api_key_id = :id"), {"id": api_key_id}
            )
        ).mappings().all()
    await engine.dispose()
    assert len(rows) == 1
    assert rows[0]["limit_per_window"] == 5000


async def test_ingest_key_forbidden(client, make_api_key):
    """AC3: an ingest-scoped key gets 403 `forbidden` — admin scope is required."""
    presented_key, _ = await make_api_key(scope="ingest")

    response = await client.put(
        "/v1/quotas", json=_payload(), headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_put_requires_auth(client):
    """AC4: missing/malformed auth -> 401."""
    response = await client.put("/v1/quotas", json=_payload())
    assert response.status_code == 401

    response = await client.put(
        "/v1/quotas", json=_payload(), headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_validation_rejections(client, make_api_key):
    """AC5: invalid limit / unknown field / bad customer_id all -> 422."""
    presented_key, _ = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}

    zero_limit = await client.put("/v1/quotas", json=_payload(limit_per_window=0), headers=headers)
    assert zero_limit.status_code == 422
    assert zero_limit.json()["error"]["code"] == "validation_failed"

    negative_limit = await client.put("/v1/quotas", json=_payload(limit_per_window=-5), headers=headers)
    assert negative_limit.status_code == 422

    non_integer_limit = await client.put(
        "/v1/quotas", json=_payload(limit_per_window="not-a-number"), headers=headers
    )
    assert non_integer_limit.status_code == 422

    unknown_field = await client.put(
        "/v1/quotas", json=_payload(extra_field="unexpected"), headers=headers
    )
    assert unknown_field.status_code == 422

    bad_customer_id = await client.put(
        "/v1/quotas", json=_payload(customer_id="bad id with spaces"), headers=headers
    )
    assert bad_customer_id.status_code == 422


async def test_injection_rejected_at_boundary(client, make_api_key):
    """AC18: an injection payload never reaches the SQL sink — 422 at the schema boundary."""
    presented_key, _ = await make_api_key(scope="admin")
    response = await client.put(
        "/v1/quotas",
        json=_payload(customer_id="' OR 1=1--"),
        headers={"Authorization": f"Bearer {presented_key}"},
    )
    assert response.status_code == 422


async def test_quota_is_tenant_isolated(client, make_api_key, postgres_url):
    """AC6: a quota set by tenant A is invisible/inert for tenant B's key —
    each tenant's PUT creates its own row scoped by its own api_key_id."""
    key_a, api_key_id_a = await make_api_key(label="tenant-a", scope="admin")
    key_b, api_key_id_b = await make_api_key(label="tenant-b", scope="admin")

    await client.put(
        "/v1/quotas",
        json=_payload(limit_per_window=100),
        headers={"Authorization": f"Bearer {key_a}"},
    )
    response_b = await client.put(
        "/v1/quotas",
        json=_payload(limit_per_window=999),
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert response_b.status_code == 201, "tenant B's PUT must create its own row, not collide with tenant A's"

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT api_key_id, limit_per_window FROM quotas ORDER BY api_key_id")
            )
        ).mappings().all()
    await engine.dispose()

    by_key = {row["api_key_id"]: row["limit_per_window"] for row in rows}
    assert by_key[api_key_id_a] == 100
    assert by_key[api_key_id_b] == 999


async def test_put_rate_limited_per_principal(truncate_tables, make_api_key):
    """AC15: PUT /v1/quotas is Tier-2 per-api_key_id rate limited; two admin
    principals sharing one IP get independent buckets (mirrors test_rate_limit.py)."""
    from httpx import ASGITransport, AsyncClient

    from src.main import app

    key_a, _ = await make_api_key(label="a", rate_limit_per_sec=1, scope="admin")
    key_b, _ = await make_api_key(label="b", rate_limit_per_sec=100, scope="admin")

    same_ip_transport = ASGITransport(app=app, client=("10.0.0.9", 4000))
    async with AsyncClient(transport=same_ip_transport, base_url="http://test") as test_client:
        first = await test_client.put(
            "/v1/quotas", json=_payload(), headers={"Authorization": f"Bearer {key_a}"}
        )
        second = await test_client.put(
            "/v1/quotas", json=_payload(limit_per_window=2), headers={"Authorization": f"Bearer {key_a}"}
        )
        assert first.status_code == 201
        assert second.status_code == 429
        assert "Retry-After" in second.headers

        b_response = await test_client.put(
            "/v1/quotas", json=_payload(), headers={"Authorization": f"Bearer {key_b}"}
        )
        assert b_response.status_code == 201, "principal B, same IP, must have its own untouched bucket"


async def test_upsert_and_forbidden_logged(client, make_api_key, capsys):
    """AC19: a successful upsert logs `quota.upsert`; a scope-denied PUT logs
    `quota.forbidden` (structlog prints JSON to stdout — see test_logging_redaction.py)."""
    admin_key, _ = await make_api_key(scope="admin")
    ingest_key, _ = await make_api_key(label="ingest-only", scope="ingest")

    await client.put("/v1/quotas", json=_payload(), headers={"Authorization": f"Bearer {admin_key}"})
    await client.put("/v1/quotas", json=_payload(), headers={"Authorization": f"Bearer {ingest_key}"})

    captured = capsys.readouterr()
    assert "quota.upsert" in captured.out
    assert "quota.forbidden" in captured.out


async def test_openapi_exposes_put_quotas(client):
    """AC21: the served /openapi.json includes PUT /v1/quotas."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/v1/quotas" in schema["paths"]
    assert "put" in schema["paths"]["/v1/quotas"]
