"""Integration tests for the served dashboard page + BFF against a real
Postgres/Redis (AC1, AC2-5 structure, AC8-25 where wire-testable).

The `dashboard-reader` principal is a real seeded API key whose plaintext is
supplied via the `DASHBOARD_READER_API_KEY` env fallback (matches
`Settings.dashboard_reader_secret_env_fallback`) — the secrets facade's
Secrets Manager lookup fails (no AWS credentials in this environment) and
falls through to that env var, exactly like local/dev per the plan.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest


@pytest.fixture(autouse=True)
def _reset_dashboard_reader_cache():
    """The reader principal is memoized process-wide with a TTL — reset it
    around every test so one test's seeded key never leaks into another."""
    from src.auth import dashboard_reader

    dashboard_reader.invalidate_cached_reader_principal()
    yield
    dashboard_reader.invalidate_cached_reader_principal()


@pytest.fixture
async def dashboard_reader_key(make_api_key, monkeypatch):
    """Seed a real API key and point the dashboard-reader env fallback at it."""
    presented_key, api_key_id = await make_api_key(label="dashboard-reader", rate_limit_per_sec=1000)
    monkeypatch.setenv("DASHBOARD_READER_API_KEY", presented_key)
    return presented_key, api_key_id


def _event_payload(**overrides) -> dict:
    payload = {
        "customer_id": "acme-corp",
        "metric": "api_calls",
        "quantity": "5",
        "idempotency_key": "idem-1",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# AC1 / AC14 / AC15: served page + assets, headers, CSP, no-store
# --------------------------------------------------------------------------


async def test_get_dashboard_returns_200_html_with_page_csp_and_header_set(client, dashboard_reader_key):
    response = await client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "connect-src 'self'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "Strict-Transport-Security" in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "Referrer-Policy" in response.headers
    assert "Permissions-Policy" in response.headers


async def test_dashboard_static_assets_serve_200(client, dashboard_reader_key):
    css_response = await client.get("/dashboard/static/dashboard.css")
    js_response = await client.get("/dashboard/static/dashboard.js")

    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert js_response.status_code == 200
    assert js_response.headers["cache-control"] == "no-store"
    assert js_response.headers["content-security-policy"] == css_response.headers["content-security-policy"]


async def test_dashboard_api_routes_carry_no_store_and_strict_json_csp(client, dashboard_reader_key):
    config_response = await client.get("/dashboard/api/config")

    assert config_response.status_code == 200
    assert config_response.headers["cache-control"] == "no-store"
    assert config_response.headers["content-security-policy"] == "default-src 'none'"


async def test_frame_ancestors_and_x_frame_options_both_present(client, dashboard_reader_key):
    """AC15: clickjacking defense via both CSP frame-ancestors and the legacy header."""
    response = await client.get("/dashboard")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


# --------------------------------------------------------------------------
# AC2-5: served HTML structure (CMP ids present as data-testids)
# --------------------------------------------------------------------------


async def test_served_html_contains_cmp_structure(client, dashboard_reader_key):
    response = await client.get("/dashboard")
    html = response.text

    for testid in [
        "dashboard-screen", "app-header", "env-badge", "customer-select",
        "metric-select", "window-segmented-control", "loading-state",
        "populated-state", "empty-state", "error-state", "stat-card",
        "usage-table-card", "usage-table", "stat-number", "stat-delta-pill", "retry-button",
    ]:
        assert f'data-testid="{testid}"' in html, f"missing data-testid={testid}"

    assert "Meterly" in html
    assert "breadcrumb-current\">Usage" in html
    assert 'data-granularity="month"' in html and 'aria-disabled="true"' in html


# --------------------------------------------------------------------------
# AC8 / AC23: data sourced from get_usage, correct hour + day series
# --------------------------------------------------------------------------


async def test_usage_series_hour_granularity_reflects_seeded_events(client, dashboard_reader_key):
    presented_key, _ = dashboard_reader_key
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post("/v1/events", json=_event_payload(idempotency_key="a"), headers=headers)
    await client.post("/v1/events", json=_event_payload(idempotency_key="b", quantity="3"), headers=headers)

    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "populated"
    assert Decimal(body["current"]["quantity"].replace(",", "")) == Decimal("8")
    assert len(body["rows"]) == 10


async def test_usage_series_day_granularity_sums_the_days_hours(client, dashboard_reader_key):
    presented_key, _ = dashboard_reader_key
    headers = {"Authorization": f"Bearer {presented_key}"}

    await client.post("/v1/events", json=_event_payload(idempotency_key="d1", quantity="10"), headers=headers)

    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "day"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "populated"
    assert Decimal(body["current"]["quantity"].replace(",", "")) == Decimal("10")


async def test_usage_series_empty_state_when_no_events(client, dashboard_reader_key):
    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "globex", "metric": "storage_gb", "granularity": "hour"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "empty"
    assert all(Decimal(row["quantity"].replace(",", "")) == Decimal("0") for row in body["rows"])


# --------------------------------------------------------------------------
# AC9: no client-side API key anywhere
# --------------------------------------------------------------------------


async def test_no_credential_in_served_assets_or_bff_responses(client, dashboard_reader_key):
    presented_key, _ = dashboard_reader_key

    page = await client.get("/dashboard")
    css = await client.get("/dashboard/static/dashboard.css")
    js = await client.get("/dashboard/static/dashboard.js")
    config = await client.get("/dashboard/api/config")
    series = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"},
    )

    for response in (page, css, js, config, series):
        text = response.text
        assert "mtr_live" not in text
        assert presented_key not in text
        assert "Authorization" not in text


async def test_bff_request_requires_no_authorization_header(client, dashboard_reader_key):
    """The BFF proxy is app-layer unauthenticated for the viewer — the
    browser sends no credential at all."""
    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"},
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------
# AC10: tenant isolation / IDOR through the data path
# --------------------------------------------------------------------------


async def test_cross_tenant_rows_never_appear_in_bff_output(client, make_api_key, monkeypatch):
    """A different api_key_id's events for the same customer_id never leak
    into the reader tenant's BFF series — proves the api_key_id scoping (+
    RLS) still isolates when driven by the server-held reader principal."""
    from src.auth import dashboard_reader

    other_key, _ = await make_api_key(label="tenant-a", rate_limit_per_sec=100)
    await client.post(
        "/v1/events",
        json=_event_payload(idempotency_key="a-1", quantity="99999"),
        headers={"Authorization": f"Bearer {other_key}"},
    )

    reader_key, _ = await make_api_key(label="dashboard-reader", rate_limit_per_sec=100)
    monkeypatch.setenv("DASHBOARD_READER_API_KEY", reader_key)
    dashboard_reader.invalidate_cached_reader_principal()

    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "empty", "another tenant's data must never surface through the reader's BFF"


# --------------------------------------------------------------------------
# AC11: BFF input validation
# --------------------------------------------------------------------------


async def test_usage_series_rejects_customer_not_in_allowlist(client, dashboard_reader_key):
    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "not-a-real-customer", "metric": "api_calls", "granularity": "hour"},
    )
    assert response.status_code == 422


async def test_usage_series_rejects_month_granularity(client, dashboard_reader_key):
    """AC23/Q1: month is disabled — cannot be served correctly, must 422."""
    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "month"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("customer_id", ["' OR 1=1--", "<script>alert(1)</script>", "../../etc/passwd"])
async def test_usage_series_rejects_injection_payloads(client, dashboard_reader_key, customer_id):
    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": customer_id, "metric": "api_calls", "granularity": "hour"},
    )
    assert response.status_code == 422
    assert customer_id not in response.text


async def test_usage_series_rejects_unknown_query_param(client, dashboard_reader_key):
    response = await client.get(
        "/dashboard/api/usage-series",
        params={
            "customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour",
            "window": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# AC12: Tier-1 anonymous edge rate limit
# --------------------------------------------------------------------------


async def test_usage_series_tier1_throttle_exhausted_returns_429(postgres_url, redis_url, make_api_key, truncate_tables):
    """Tier-1 capacity is bound into the middleware at app-construction time
    from `Settings`, so this drives a fresh out-of-process uvicorn with a
    tiny bucket (capacity=1) rather than mutating the shared in-process app
    singleton other tests already imported."""
    import asyncio
    import os
    import socket
    import subprocess
    import sys
    import time
    from contextlib import closing
    from pathlib import Path

    import httpx

    repo_root = Path(__file__).resolve().parents[2]

    def _free_port() -> int:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    presented_key, _ = await make_api_key(label="dashboard-reader", rate_limit_per_sec=1000)

    port = _free_port()
    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url
    env["METERLY_REDIS_URL"] = redis_url
    env["METERLY_TIER1_RATE_LIMIT_PER_SECOND"] = "1"
    env["METERLY_TIER1_RATE_LIMIT_BURST"] = "1"
    env["DASHBOARD_READER_API_KEY"] = presented_key

    import tempfile

    scratch_dir = Path(os.environ.get("METERLY_TEST_SCRATCH_DIR", tempfile.gettempdir()))
    scratch_dir.mkdir(parents=True, exist_ok=True)
    log_path = scratch_dir / "dashboard_tier1_uvicorn.log"
    log_file = open(log_path, "w")

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(repo_root), env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as probe:
            ready = False
            while time.monotonic() < deadline:
                try:
                    response = await probe.get(f"{base_url}/health", timeout=1.0)
                    if response.status_code == 200:
                        ready = True
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.3)
            if not ready:
                pytest.skip("uvicorn did not become ready in time in this environment")

            params = {"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"}
            try:
                first = await probe.get(f"{base_url}/dashboard/api/usage-series", params=params, timeout=30.0)
                second = await probe.get(f"{base_url}/dashboard/api/usage-series", params=params, timeout=30.0)
            except httpx.TransportError as exc:
                log_file.flush()
                pytest.fail(f"request to the throttled server failed/timed out: {exc}; see {log_path}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


# --------------------------------------------------------------------------
# AC17: DAST-1 readiness — openapi schema includes the BFF routes
# --------------------------------------------------------------------------


async def test_openapi_schema_includes_dashboard_bff_routes(client, dashboard_reader_key):
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/dashboard/api/usage-series" in schema["paths"]
    assert "/dashboard/api/config" in schema["paths"]

    usage_series_get = schema["paths"]["/dashboard/api/usage-series"]["get"]
    param_names = {param["name"] for param in usage_series_get.get("parameters", [])}
    assert {"customer_id", "metric", "granularity"} <= param_names


# --------------------------------------------------------------------------
# AC19: data-protection — no raw customer_id logged, secret not serialized
# --------------------------------------------------------------------------


async def test_dashboard_usage_series_log_does_not_include_raw_customer_id(client, dashboard_reader_key, capsys):
    response = await client.get(
        "/dashboard/api/usage-series",
        params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"},
    )
    assert response.status_code == 200

    captured = capsys.readouterr()
    assert "acme-corp" not in captured.out, "the raw customer_id must never reach the rendered log line"
    assert '"operation": "dashboard.usage_series"' in captured.out, "the audit event must actually fire"


# --------------------------------------------------------------------------
# AC20: environment badge sourced from real config
# --------------------------------------------------------------------------


async def test_config_route_returns_configured_allowlists_and_environment(client, dashboard_reader_key):
    response = await client.get("/dashboard/api/config")
    assert response.status_code == 200
    body = response.json()
    assert body["customers"] == ["acme-corp", "globex", "initech"]
    assert body["metrics"] == ["api_calls", "storage_gb", "active_seats"]
    assert body["granularities"] == ["hour", "day"]
    assert "environment" in body


# --------------------------------------------------------------------------
# AC24: safe error — a forced BFF internal error returns the generic envelope
# --------------------------------------------------------------------------


async def test_forced_internal_error_returns_generic_envelope_no_leak(dashboard_reader_key, monkeypatch):
    """Force the reader-principal resolution to raise mid-request and assert
    the response is the generic 500 envelope: no stack, no SQL, no secret,
    no reader-key/customer_id, no internal path."""
    from httpx import ASGITransport, AsyncClient

    from src.main import app
    from src.services import dashboard_service

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated internal failure touching secret mtr_live_shouldnotleak and /var/app/secret.py")

    monkeypatch.setattr(dashboard_service, "get_dashboard_reader_principal", _boom)

    # ASGITransport re-raises app exceptions by default; here we deliberately
    # exercise the app's own generic 500 handler, so let it convert instead.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.get(
            "/dashboard/api/usage-series",
            params={"customer_id": "acme-corp", "metric": "api_calls", "granularity": "hour"},
        )

    assert response.status_code == 500
    body = response.json()
    assert set(body["error"].keys()) == {"code", "message", "requestId"}
    assert body["error"]["code"] == "internal"

    raw_text = response.text
    for leak in ["mtr_live_shouldnotleak", "/var/app/secret.py", "Traceback", "RuntimeError", "acme-corp"]:
        assert leak not in raw_text


async def test_forced_config_route_error_also_returns_generic_envelope(dashboard_reader_key, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from src.api.routes import dashboard as dashboard_routes
    from src.main import app

    def _boom():
        raise RuntimeError("config internal failure")

    monkeypatch.setattr(dashboard_routes, "get_settings", _boom)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.get("/dashboard/api/config")

    assert response.status_code == 500
    body = response.json()
    assert set(body["error"].keys()) == {"code", "message", "requestId"}
    assert "config internal failure" not in response.text
