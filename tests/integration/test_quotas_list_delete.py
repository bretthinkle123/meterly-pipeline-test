"""Integration tests for `GET /v1/quotas` (list) and `DELETE /v1/quotas`
against a real Postgres + Redis (AC1-AC17, AC19; AC18 is delegated to the
security stage's ASVS reconciliation).
"""

import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


def _put_payload(**overrides) -> dict:
    payload = {"customer_id": "cust_1", "metric": "api_calls", "limit_per_window": 1000}
    payload.update(overrides)
    return payload


async def _seed_quota(client, headers, **overrides) -> None:
    """Create a quota via the existing PUT endpoint — reusing the already-proven
    write path rather than inserting rows directly keeps these tests focused on
    the new GET/DELETE behavior."""
    response = await client.put("/v1/quotas", json=_put_payload(**overrides), headers=headers)
    assert response.status_code in (200, 201)


async def test_get_lists_tenant_quotas_ordered_minimal_fields(client, make_api_key):
    """AC1: GET returns the full unpaginated list, ordered by (customer_id,
    metric), each row exactly {customer_id, metric, limit_per_window}."""
    presented_key, _ = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}

    # Seed out of order so the response ordering is actually proven, not
    # merely echoing insertion order.
    await _seed_quota(client, headers, customer_id="cust_b", metric="api_calls", limit_per_window=200)
    await _seed_quota(client, headers, customer_id="cust_a", metric="storage_gb", limit_per_window=50)
    await _seed_quota(client, headers, customer_id="cust_a", metric="api_calls", limit_per_window=100)

    response = await client.get("/v1/quotas", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body == [
        {"customer_id": "cust_a", "metric": "api_calls", "limit_per_window": 100},
        {"customer_id": "cust_a", "metric": "storage_gb", "limit_per_window": 50},
        {"customer_id": "cust_b", "metric": "api_calls", "limit_per_window": 200},
    ]
    for row in body:
        assert set(row.keys()) == {"customer_id", "metric", "limit_per_window"}


async def test_get_is_tenant_isolated(client, make_api_key):
    """AC2: tenant A's list contains only tenant A's rows, never tenant B's."""
    key_a, _ = await make_api_key(label="tenant-a", scope="admin")
    key_b, _ = await make_api_key(label="tenant-b", scope="admin")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    await _seed_quota(client, headers_a, customer_id="cust_a", limit_per_window=100)
    await _seed_quota(client, headers_b, customer_id="cust_b", limit_per_window=200)

    response_a = await client.get("/v1/quotas", headers=headers_a)
    assert response_a.status_code == 200
    body_a = response_a.json()
    assert len(body_a) == 1
    assert body_a[0]["customer_id"] == "cust_a"


async def test_get_empty_returns_200_empty_list(client, make_api_key):
    """AC3: no quotas -> 200 with an empty array, never 404."""
    presented_key, _ = await make_api_key(scope="admin")

    response = await client.get(
        "/v1/quotas", headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 200
    assert response.json() == []


async def test_get_ingest_key_forbidden(client, make_api_key):
    """AC4: an ingest-scoped key gets 403 `forbidden` on GET — admin scope required."""
    presented_key, _ = await make_api_key(scope="ingest")

    response = await client.get(
        "/v1/quotas", headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_get_requires_auth(client):
    """AC5: missing/malformed auth -> 401 on GET."""
    response = await client.get("/v1/quotas")
    assert response.status_code == 401

    response = await client.get(
        "/v1/quotas", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_delete_existing_returns_204_and_removes_row(client, make_api_key, postgres_url):
    """AC6: DELETE of an existing quota -> 204 empty body; the row is gone
    from a subsequent GET and from the table directly."""
    presented_key, api_key_id = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}
    await _seed_quota(client, headers, customer_id="cust_1", metric="api_calls")

    response = await client.delete(
        "/v1/quotas", params={"customer_id": "cust_1", "metric": "api_calls"}, headers=headers
    )

    assert response.status_code == 204
    assert response.content == b""

    listing = await client.get("/v1/quotas", headers=headers)
    assert listing.json() == []

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM quotas WHERE api_key_id = :id"), {"id": api_key_id}
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 0


async def test_delete_absent_returns_404_envelope(client, make_api_key):
    """AC7: DELETE of a (customer_id, metric) with no quota -> 404 not_found,
    the standard error envelope (explicit, not silently idempotent)."""
    presented_key, _ = await make_api_key(scope="admin")

    response = await client.delete(
        "/v1/quotas",
        params={"customer_id": "cust_missing", "metric": "api_calls"},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_delete_cannot_touch_other_tenant(client, make_api_key, postgres_url):
    """AC8: tenant A deleting a (customer_id, metric) that exists only under
    tenant B -> 404 (invisible), and tenant B's row is still present."""
    key_a, _ = await make_api_key(label="tenant-a", scope="admin")
    key_b, api_key_id_b = await make_api_key(label="tenant-b", scope="admin")
    headers_a = {"Authorization": f"Bearer {key_a}"}
    headers_b = {"Authorization": f"Bearer {key_b}"}

    await _seed_quota(client, headers_b, customer_id="cust_shared", metric="api_calls", limit_per_window=500)

    response = await client.delete(
        "/v1/quotas", params={"customer_id": "cust_shared", "metric": "api_calls"}, headers=headers_a
    )
    assert response.status_code == 404

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT limit_per_window FROM quotas WHERE api_key_id = :id"),
                {"id": api_key_id_b},
            )
        ).mappings().one()
    await engine.dispose()
    assert row["limit_per_window"] == 500


async def test_delete_validation_rejections(client, make_api_key):
    """AC9: missing/invalid/injection/unknown-extra query params -> 422 validation_failed."""
    presented_key, _ = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}

    missing_metric = await client.delete(
        "/v1/quotas", params={"customer_id": "cust_1"}, headers=headers
    )
    assert missing_metric.status_code == 422
    assert missing_metric.json()["error"]["code"] == "validation_failed"

    bad_customer_id = await client.delete(
        "/v1/quotas", params={"customer_id": "bad id with spaces", "metric": "api_calls"}, headers=headers
    )
    assert bad_customer_id.status_code == 422

    injection = await client.delete(
        "/v1/quotas", params={"customer_id": "' OR 1=1--", "metric": "api_calls"}, headers=headers
    )
    assert injection.status_code == 422

    extra_param = await client.delete(
        "/v1/quotas",
        params={"customer_id": "cust_1", "metric": "api_calls", "api_key_id": "999"},
        headers=headers,
    )
    assert extra_param.status_code == 422


async def test_delete_ingest_key_forbidden(client, make_api_key):
    """AC10: an ingest-scoped key gets 403 `forbidden` on DELETE."""
    presented_key, _ = await make_api_key(scope="ingest")

    response = await client.delete(
        "/v1/quotas",
        params={"customer_id": "cust_1", "metric": "api_calls"},
        headers={"Authorization": f"Bearer {presented_key}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_delete_requires_auth(client):
    """AC11: missing/malformed auth -> 401 on DELETE."""
    response = await client.delete(
        "/v1/quotas", params={"customer_id": "cust_1", "metric": "api_calls"}
    )
    assert response.status_code == 401

    response = await client.delete(
        "/v1/quotas",
        params={"customer_id": "cust_1", "metric": "api_calls"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


async def test_quota_admin_routes_rate_limited_per_principal(truncate_tables, make_api_key):
    """AC12: both GET and DELETE /v1/quotas are Tier-2 per-api_key_id rate
    limited; two admin principals sharing one client IP get independent
    buckets (mirrors test_quotas_endpoint.py::test_put_rate_limited_per_principal)."""
    from src.main import app

    key_a, _ = await make_api_key(label="a", rate_limit_per_sec=1, scope="admin")
    key_b, _ = await make_api_key(label="b", rate_limit_per_sec=100, scope="admin")

    same_ip_transport = ASGITransport(app=app, client=("10.0.0.9", 4000))
    async with AsyncClient(transport=same_ip_transport, base_url="http://test") as test_client:
        first = await test_client.get("/v1/quotas", headers={"Authorization": f"Bearer {key_a}"})
        second = await test_client.get("/v1/quotas", headers={"Authorization": f"Bearer {key_a}"})
        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

        b_response = await test_client.get("/v1/quotas", headers={"Authorization": f"Bearer {key_b}"})
        assert b_response.status_code == 200, "principal B, same IP, must have its own untouched bucket"

        third = await test_client.delete(
            "/v1/quotas",
            params={"customer_id": "cust_1", "metric": "api_calls"},
            headers={"Authorization": f"Bearer {key_b}"},
        )
        # key_b's bucket has capacity left (100/sec); a 404 (no such quota) is
        # the expected outcome here, proving the request reached the handler
        # rather than being throttled.
        assert third.status_code == 404


async def test_delete_does_not_reset_usage_and_events_uncapped(client, make_api_key, postgres_url):
    """AC13: deleting a quota removes only the cap — `usage_rollup` for that
    (customer, metric) is unchanged, and a subsequent POST /v1/events for the
    now-uncapped pair is accepted 201 unlimited (same as never-capped)."""
    presented_key, api_key_id = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}

    # Cap it low, then record usage that approaches (but does not exceed) the cap.
    await _seed_quota(client, headers, customer_id="cust_1", metric="api_calls", limit_per_window=10)
    event_response = await client.post(
        "/v1/events",
        json={
            "customer_id": "cust_1",
            "metric": "api_calls",
            "quantity": "5",
            "idempotency_key": "pre-delete-event",
        },
        headers=headers,
    )
    assert event_response.status_code == 201

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rollup_before = (
            await connection.execute(
                text(
                    "SELECT total_quantity FROM usage_rollup WHERE api_key_id = :id "
                    "AND customer_id = 'cust_1' AND metric = 'api_calls'"
                ),
                {"id": api_key_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert rollup_before == 5

    delete_response = await client.delete(
        "/v1/quotas", params={"customer_id": "cust_1", "metric": "api_calls"}, headers=headers
    )
    assert delete_response.status_code == 204

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rollup_after = (
            await connection.execute(
                text(
                    "SELECT total_quantity FROM usage_rollup WHERE api_key_id = :id "
                    "AND customer_id = 'cust_1' AND metric = 'api_calls'"
                ),
                {"id": api_key_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert rollup_after == 5, "DELETE must never touch usage_rollup"

    # A large event that would have exceeded the old cap (10) is now accepted
    # unlimited, since the cap no longer exists.
    uncapped_response = await client.post(
        "/v1/events",
        json={
            "customer_id": "cust_1",
            "metric": "api_calls",
            "quantity": "9999",
            "idempotency_key": "post-delete-event",
        },
        headers=headers,
    )
    assert uncapped_response.status_code == 201


async def test_delete_and_forbidden_are_logged(client, make_api_key, capsys):
    """AC16: a successful DELETE logs `quota.delete` INFO (customer_id
    redacted); a scope-denied GET or DELETE logs the existing `quota.forbidden` WARNING."""
    admin_key, _ = await make_api_key(scope="admin")
    ingest_key, _ = await make_api_key(label="ingest-only", scope="ingest")
    headers = {"Authorization": f"Bearer {admin_key}"}
    await _seed_quota(client, headers, customer_id="cust_1", metric="api_calls")

    await client.delete("/v1/quotas", params={"customer_id": "cust_1", "metric": "api_calls"}, headers=headers)
    await client.get("/v1/quotas", headers={"Authorization": f"Bearer {ingest_key}"})

    captured = capsys.readouterr()
    assert "quota.delete" in captured.out
    assert "***redacted***" in captured.out
    assert "quota.forbidden" in captured.out


async def test_openapi_exposes_get_and_delete_quotas(client):
    """AC17 (DAST-readiness): the served /openapi.json includes both GET and
    DELETE /v1/quotas, matching the implemented routes/schemas."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/v1/quotas" in schema["paths"]
    assert "get" in schema["paths"]["/v1/quotas"]
    assert "delete" in schema["paths"]["/v1/quotas"]


async def test_get_forced_internal_error_returns_safe_envelope(
    truncate_tables, make_api_key, monkeypatch
):
    """AC19: a forced internal error during the GET list read returns the
    generic 500 envelope, with no stack trace / SQL fragment / exception
    type / internal path leaked."""
    presented_key, _ = await make_api_key(scope="admin")

    from src.services import quota_service

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(quota_service, "list_quotas", _boom)

    from src.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.get(
            "/v1/quotas", headers={"Authorization": f"Bearer {presented_key}"}
        )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal"
    assert "requestId" in body["error"]
    message = body["error"]["message"].lower()
    assert "runtimeerror" not in message
    assert "simulated read failure" not in message
    assert "traceback" not in message


async def test_delete_forced_internal_error_returns_safe_envelope_and_fails_closed(
    client, make_api_key, postgres_url, monkeypatch
):
    """AC19: a forced internal error during DELETE returns the generic 500
    envelope and fails closed — the transaction rolls back, so the target
    quota row survives (mirrors test_events_endpoint.py's forced-error test)."""
    presented_key, api_key_id = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}
    await _seed_quota(client, headers, customer_id="cust_1", metric="api_calls", limit_per_window=42)

    from src.services import quota_service

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated delete failure")

    monkeypatch.setattr(quota_service, "delete_quota", _boom)

    from src.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.delete(
            "/v1/quotas",
            params={"customer_id": "cust_1", "metric": "api_calls"},
            headers=headers,
        )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal"
    assert "requestId" in body["error"]
    message = body["error"]["message"].lower()
    assert "runtimeerror" not in message
    assert "simulated delete failure" not in message
    assert "traceback" not in message

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT limit_per_window FROM quotas WHERE api_key_id = :id "
                    "AND customer_id = 'cust_1' AND metric = 'api_calls'"
                ),
                {"id": api_key_id},
            )
        ).mappings().one()
    await engine.dispose()
    assert row["limit_per_window"] == 42, "a failed DELETE must leave the target row intact (fail-closed)"


async def test_concurrent_delete_of_the_same_quota_yields_exactly_one_winner(
    client, make_api_key, postgres_url
):
    """AC6/AC7 under contention: N concurrent DELETE requests for the exact
    same (customer_id, metric) must yield exactly one 204 (the winner) and
    the rest 404 (the loser's explicit outcome) -- never two 204s (a double
    side effect) and never a 500/race artifact. Proves the decision point
    (delete_quota's single parameterized DELETE ... RETURNING) is actually
    atomic under real concurrent access, not merely correct in the
    single-caller tests above."""
    presented_key, api_key_id = await make_api_key(scope="admin")
    headers = {"Authorization": f"Bearer {presented_key}"}
    await _seed_quota(client, headers, customer_id="cust_race", metric="api_calls", limit_per_window=100)

    concurrent_requests = 10

    async def _delete():
        return await client.delete(
            "/v1/quotas", params={"customer_id": "cust_race", "metric": "api_calls"}, headers=headers
        )

    responses = await asyncio.gather(*(_delete() for _ in range(concurrent_requests)))
    statuses = [response.status_code for response in responses]

    winners = [status for status in statuses if status == 204]
    losers = [status for status in statuses if status == 404]

    assert len(winners) == 1, f"expected exactly one 204 winner, got statuses: {statuses}"
    assert len(losers) == concurrent_requests - 1, f"expected the rest to be 404 losers, got statuses: {statuses}"
    assert set(statuses) <= {204, 404}, f"no request should error under contention, got statuses: {statuses}"

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM quotas WHERE api_key_id = :id "
                    "AND customer_id = 'cust_race' AND metric = 'api_calls'"
                ),
                {"id": api_key_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert count == 0, "the row must be gone exactly once, not left behind or double-removed"


async def test_no_new_alembic_migration_added(postgres_url):
    """AC14: this feature operates on the existing `quotas` table — no new
    file under alembic/versions/ beyond the existing revisions 0001-0003."""
    from pathlib import Path

    versions_dir = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    revision_files = sorted(p.name for p in versions_dir.glob("*.py") if not p.name.startswith("__"))
    assert len(revision_files) == 3, f"expected exactly 3 migration files, found: {revision_files}"
