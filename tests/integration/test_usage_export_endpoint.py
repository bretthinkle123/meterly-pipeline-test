"""Integration tests for `GET /v1/usage/export` against a real Postgres:
streaming correctness, tenant isolation, the row cap, formula-escaping
end-to-end, response headers, and the pre-flight fail-closed safe-error path
(AC1-AC13, AC17, AC19, AC22).
"""

import asyncio
import re
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _event_payload(**overrides) -> dict:
    payload = {
        "customer_id": "cust_1",
        "metric": "api_calls",
        "quantity": "5",
        "idempotency_key": "idem-1",
    }
    payload.update(overrides)
    return payload


async def _bulk_insert_rollups(
    postgres_url, *, api_key_id: int, metric: str, window_start: datetime, count: int
) -> None:
    """Directly seed `usage_rollup` rows via a single set-based INSERT — the
    cap/perf tests need tens of thousands of rows, which is impractical to
    seed one ingest POST at a time."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO usage_rollup (api_key_id, customer_id, metric, window_start, total_quantity, event_count)
                SELECT :api_key_id, 'cust_' || lpad(gs::text, 8, '0'), :metric, :window_start, 1, 1
                FROM generate_series(1, :count) AS gs
                """
            ),
            {"api_key_id": api_key_id, "metric": metric, "window_start": window_start, "count": count},
        )
    await engine.dispose()


async def test_export_is_header_only_csv_for_empty_result(client, make_api_key):
    """AC12: a caller with no matching rollups gets 200 with a single header
    line, never 404."""
    presented_key, _ = await make_api_key()
    response = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {presented_key}"})

    assert response.status_code == 200
    assert response.text == "customer_id,metric,window_start,total_quantity\r\n"


async def test_export_with_no_filters_returns_all_caller_rows(client, make_api_key):
    """AC2: omitting every filter exports the caller's full row set."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post(
        "/v1/events", json=_event_payload(customer_id="cust_a", idempotency_key="a"), headers=headers
    )
    await client.post(
        "/v1/events", json=_event_payload(customer_id="cust_b", idempotency_key="b"), headers=headers
    )

    response = await client.get("/v1/usage/export", headers=headers)
    lines = response.text.strip("\r\n").split("\r\n")

    assert response.status_code == 200
    assert lines[0] == "customer_id,metric,window_start,total_quantity"
    assert len(lines) == 3  # header + 2 rows


async def test_export_customer_id_filter_narrows_the_result(client, make_api_key):
    """AC2: a customer_id filter excludes rows for other customers."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post(
        "/v1/events", json=_event_payload(customer_id="cust_a", idempotency_key="a"), headers=headers
    )
    await client.post(
        "/v1/events", json=_event_payload(customer_id="cust_b", idempotency_key="b"), headers=headers
    )

    response = await client.get(
        "/v1/usage/export", params={"customer_id": "cust_a"}, headers=headers
    )
    lines = response.text.strip("\r\n").split("\r\n")

    assert len(lines) == 2  # header + cust_a only
    assert "cust_a" in lines[1]
    assert "cust_b" not in response.text


async def test_export_metric_filter_narrows_the_result(client, make_api_key):
    """AC2: a metric filter excludes rows for other metrics."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post(
        "/v1/events", json=_event_payload(metric="api_calls", idempotency_key="a"), headers=headers
    )
    await client.post(
        "/v1/events", json=_event_payload(metric="storage_bytes", idempotency_key="b"), headers=headers
    )

    response = await client.get("/v1/usage/export", params={"metric": "api_calls"}, headers=headers)
    lines = response.text.strip("\r\n").split("\r\n")

    assert len(lines) == 2  # header + api_calls only
    assert "api_calls" in lines[1]
    assert "storage_bytes" not in response.text


async def test_export_from_to_window_filter_narrows_the_result(client, make_api_key):
    """AC2: `from`/`to` narrow the export to rows within the window; a row
    outside the range is excluded."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post(
        "/v1/events", json=_event_payload(customer_id="cust_in_window", idempotency_key="a"), headers=headers
    )

    now = datetime.now(timezone.utc)
    in_window_from = (now - timedelta(hours=1)).isoformat()
    in_window_to = (now + timedelta(hours=1)).isoformat()
    # A window entirely before the row's window_start (valid from<=to, but
    # both endpoints predate "now") — the row must be excluded.
    before_window_from = (now - timedelta(hours=4)).isoformat()
    before_window_to = (now - timedelta(hours=2)).isoformat()

    in_window_response = await client.get(
        "/v1/usage/export", params={"from": in_window_from, "to": in_window_to}, headers=headers
    )
    before_window_response = await client.get(
        "/v1/usage/export", params={"from": before_window_from, "to": before_window_to}, headers=headers
    )

    assert "cust_in_window" in in_window_response.text
    assert before_window_response.text == "customer_id,metric,window_start,total_quantity\r\n"


async def test_export_malformed_query_param_returns_422(client, make_api_key):
    """AC4: an out-of-window `from` returns 422 validation_failed."""
    presented_key, _ = await make_api_key()
    too_old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()

    response = await client.get(
        "/v1/usage/export", params={"from": too_old}, headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_export_unknown_query_param_returns_422(client, make_api_key):
    """AC4: `extra='forbid'` rejects an unexpected query param (e.g. api_key_id probe)."""
    presented_key, _ = await make_api_key()

    response = await client.get(
        "/v1/usage/export", params={"api_key_id": "999"}, headers={"Authorization": f"Bearer {presented_key}"}
    )

    assert response.status_code == 422


async def test_export_requires_authentication(client, make_api_key):
    """AC6: no/invalid key -> 401 before the handler runs."""
    missing = await client.get("/v1/usage/export")
    invalid = await client.get("/v1/usage/export", headers={"Authorization": "Bearer mtr_live_bogus_bogus"})

    assert missing.status_code == 401
    assert invalid.status_code == 401


async def test_export_any_authenticated_scope_may_export_ingest_and_admin(client, make_api_key):
    """AC6: both an ingest-scoped and an admin-scoped key succeed (no scope gate)."""
    ingest_key, _ = await make_api_key(label="ingest", scope="ingest")
    admin_key, _ = await make_api_key(label="admin", scope="admin")

    ingest_response = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {ingest_key}"})
    admin_response = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {admin_key}"})

    assert ingest_response.status_code == 200
    assert admin_response.status_code == 200


async def test_export_rate_limit_two_principals_one_ip_get_independent_buckets(truncate_tables, make_api_key):
    """AC5: the reused Tier-2 throttle is keyed on `api_key_id`, not client
    IP — principal A exhausting its own tiny bucket must not affect
    principal B behind the same client IP (mirrors
    tests/integration/test_rate_limit.py's discriminating shape, applied to
    this route)."""
    from src.main import app

    key_a, _ = await make_api_key(label="export-tenant-a", rate_limit_per_sec=1)
    key_b, _ = await make_api_key(label="export-tenant-b", rate_limit_per_sec=100)

    same_ip_transport = ASGITransport(app=app, client=("10.0.0.9", 5000))
    async with AsyncClient(transport=same_ip_transport, base_url="http://test") as shared_ip_client:
        first = await shared_ip_client.get("/v1/usage/export", headers={"Authorization": f"Bearer {key_a}"})
        second = await shared_ip_client.get("/v1/usage/export", headers={"Authorization": f"Bearer {key_a}"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

        third = await shared_ip_client.get("/v1/usage/export", headers={"Authorization": f"Bearer {key_b}"})
        assert third.status_code == 200


async def test_export_tenant_isolation_excludes_other_tenants_rows(client, make_api_key):
    """AC7: tenant A's export never contains tenant B's rows (IDOR/BOLA)."""
    key_a, _ = await make_api_key(label="tenant-a")
    key_b, _ = await make_api_key(label="tenant-b")

    await client.post(
        "/v1/events",
        json=_event_payload(customer_id="cust_shared_name", idempotency_key="a-1", quantity="1000"),
        headers={"Authorization": f"Bearer {key_a}"},
    )
    await client.post(
        "/v1/events",
        json=_event_payload(customer_id="cust_shared_name", idempotency_key="b-1", quantity="2000"),
        headers={"Authorization": f"Bearer {key_b}"},
    )

    response_a = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {key_a}"})
    lines_a = response_a.text.strip("\r\n").split("\r\n")

    assert len(lines_a) == 2  # header + exactly tenant A's one row
    assert "1000.000000" in response_a.text
    assert "2000.000000" not in response_a.text


async def test_export_row_cap_returns_422_with_no_partial_body(client, make_api_key, postgres_url):
    """AC8: a result over 100,000 rows is rejected with a clean 422 and no
    partial CSV body — the pre-flight count runs before any response byte."""
    presented_key, api_key_id = await make_api_key()
    window_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await _bulk_insert_rollups(
        postgres_url, api_key_id=api_key_id, metric="api_calls", window_start=window_start, count=100_001
    )

    response = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {presented_key}"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")  # not text/csv
    assert not response.text.startswith("customer_id,metric,window_start,total_quantity")
    assert "\r\n" not in response.text  # no CSV line-terminated rows, ever streamed
    body = response.json()
    assert body["error"]["code"] == "validation_failed"
    assert set(body["error"].keys()) == {"code", "message", "requestId"}  # the envelope shape only


async def test_export_deterministic_ordering(client, make_api_key):
    """AC13: rows are ordered by window_start, customer_id, metric ascending."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    for customer_id in ("cust_c", "cust_a", "cust_b"):
        await client.post(
            "/v1/events",
            json=_event_payload(customer_id=customer_id, idempotency_key=customer_id),
            headers=headers,
        )

    response = await client.get("/v1/usage/export", headers=headers)
    lines = response.text.strip("\r\n").split("\r\n")[1:]
    seen_customer_ids = [line.split(",")[0] for line in lines]

    assert seen_customer_ids == ["cust_a", "cust_b", "cust_c"]


async def test_export_formula_injection_is_escaped_end_to_end(client, make_api_key):
    """AC10: a leading-'-' customer_id (permitted by the ingest allowlist) is
    quote-prefixed in the exported CSV."""
    presented_key, _ = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post(
        "/v1/events", json=_event_payload(customer_id="-1", idempotency_key="dash"), headers=headers
    )

    response = await client.get("/v1/usage/export", headers=headers)
    lines = response.text.strip("\r\n").split("\r\n")

    assert lines[1].startswith("'-1,")


async def test_export_response_headers(client, make_api_key):
    """AC11: text/csv content type, attachment filename (UTC timestamp only,
    no tenant identifier), nosniff + no-store inherited from the middleware
    stack. The filename is asserted against its exact expected shape (not a
    substring exclusion) — a strictly-pinned pattern is what actually proves
    no extra data, tenant id included, ever rides along in it."""
    presented_key, _ = await make_api_key()

    response = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {presented_key}"})

    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    content_disposition = response.headers["content-disposition"]
    assert re.fullmatch(
        r'attachment; filename="usage-export-\d{8}T\d{6}Z\.csv"', content_disposition
    ), content_disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def _free_port() -> int:
    """Find an unused TCP port for the out-of-process uvicorn server below."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def running_server(postgres_url, redis_url, make_api_key, truncate_tables):
    """Launch the real app under uvicorn, out-of-process, against the live
    containers.

    Needed specifically for the streaming-not-buffered sanity check:
    httpx's in-process `ASGITransport` joins the *entire* response body into
    one chunk before yielding it (`ASGIResponseStream.__aiter__` does
    `yield b"".join(self._body)`), which would make a chunk-count assertion
    pass or fail independent of whether the app actually streams — only a
    real socket exposes genuine chunk boundaries.
    """
    port = _free_port()
    import os

    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url
    env["METERLY_REDIS_URL"] = redis_url
    env["METERLY_TIER1_RATE_LIMIT_PER_SECOND"] = "100000"
    env["METERLY_TIER1_RATE_LIMIT_BURST"] = "100000"

    presented_key, api_key_id = await make_api_key(rate_limit_per_sec=100000)

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as probe:
            while time.monotonic() < deadline:
                try:
                    response = await probe.get(f"{base_url}/health", timeout=1.0)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("uvicorn did not become ready in time in this sandboxed environment")

        yield base_url, presented_key, api_key_id
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


async def test_export_streaming_delivers_multiple_discrete_chunks(running_server, postgres_url):
    """AC9: the response is streamed (multiple discrete chunks arrive over
    the real socket, not one fully-buffered write) for a multi-row export."""
    base_url, presented_key, api_key_id = running_server
    window_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Large enough that a real streamed write can't coalesce into a single
    # TCP read on the client side (small payloads often do, even when the
    # server genuinely writes them incrementally).
    await _bulk_insert_rollups(
        postgres_url, api_key_id=api_key_id, metric="api_calls", window_start=window_start, count=20_000
    )

    chunk_count = 0
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        async with http_client.stream(
            "GET", f"{base_url}/v1/usage/export", headers={"Authorization": f"Bearer {presented_key}"}
        ) as response:
            assert response.status_code == 200
            async for _chunk in response.aiter_bytes():
                chunk_count += 1

    # Proof the response isn't assembled into one buffered blob before being
    # sent — a genuinely streamed multi-row export arrives as more than one
    # discrete read on the wire.
    assert chunk_count > 1


async def test_openapi_schema_exposes_the_export_route_and_responses(client):
    """AC19 (DAST-1): the served /openapi.json includes GET /v1/usage/export
    with its 200 text/csv and 422 responses, matching the implemented route."""
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "/v1/usage/export" in schema["paths"]
    export_operation = schema["paths"]["/v1/usage/export"]["get"]
    assert "200" in export_operation["responses"]
    assert "422" in export_operation["responses"]
    assert "text/csv" in export_operation["responses"]["200"]["content"]


async def test_export_seeded_dast_key_can_call_the_export(postgres_url, truncate_tables):
    """AC20 (DAST-2): a key provisioned by the real `scripts/seed_api_key.py`
    (not the fast `make_api_key` test fixture) can successfully call
    `GET /v1/usage/export` and get a 200 header-only CSV. The pre-existing
    `tests/integration/test_seed_api_key_script.py` proves the script
    persists a working key row, but never actually calls an endpoint with the
    key it prints — this closes that gap for the export route specifically,
    which is what AC20 requires ('can call the export')."""
    import os

    from src.main import app

    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url

    result = subprocess.run(
        [sys.executable, "scripts/seed_api_key.py", "--label", "dast-export-key", "--rate-limit", "50"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"seed script failed:\n{result.stdout}\n{result.stderr}"
    presented_key = result.stdout.strip().splitlines()[-1]
    assert presented_key.startswith("mtr_live_")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as seeded_client:
        response = await seeded_client.get(
            "/v1/usage/export", headers={"Authorization": f"Bearer {presented_key}"}
        )

    assert response.status_code == 200
    assert response.text == "customer_id,metric,window_start,total_quantity\r\n"


async def test_export_forced_pre_flight_count_error_returns_generic_500(truncate_tables, make_api_key, monkeypatch):
    """AC22: a forced error during the pre-flight COUNT phase (before any
    response byte) returns the generic 500 internal envelope — no
    stack/SQL/internal-path leak, and no partial CSV body."""
    from src.main import app
    from src.services import usage_export_service

    async def _raising_count(session, **kwargs):
        raise RuntimeError("simulated connection drop mid-count")

    monkeypatch.setattr(usage_export_service, "count_usage_rollups", _raising_count)

    presented_key, _ = await make_api_key()

    # ASGITransport re-raises app exceptions by default (useful for debugging
    # a broken test); here we're deliberately exercising the app's own 500
    # handler, so this client must let it convert the exception to a response
    # (mirrors tests/integration/test_events_endpoint.py's AC19 pattern).
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/usage/export", headers={"Authorization": f"Bearer {presented_key}"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")  # not text/csv
    body = response.json()
    assert body["error"]["code"] == "internal"
    assert "requestId" in body["error"]
    assert "simulated connection drop" not in response.text
    assert "Traceback" not in response.text
    assert not response.text.startswith("customer_id,metric,window_start,total_quantity")  # no CSV body ever started
