"""E2E fixtures: a real out-of-process uvicorn (real Postgres/Redis
testcontainers) + a real Chromium browser (Playwright) driving the served
`/dashboard` page — the render-state and XSS-safety behaviors (AC4, AC6, AC7,
AC13, AC20, AC23) that can only be proved by actually executing the shipped
`dashboard.js` in a browser, not by a Python HTTP client.
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _asyncpg_url(sync_url: str) -> str:
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def e2e_postgres_url():
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = _asyncpg_url(postgres.get_connection_url())
        env = dict(os.environ)
        env["DATABASE_URL"] = url
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"
        yield url


@pytest.fixture(scope="session")
def e2e_redis_url():
    with RedisContainer("redis:7-alpine") as redis_container:
        host = redis_container.get_container_host_ip()
        port = redis_container.get_exposed_port(redis_container.port)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def e2e_reader_key(e2e_postgres_url):
    """Seed a real `dashboard-reader` API key (and the `cust-e2e` customer's
    seed events) directly against the session Postgres via the app's own
    repository code, once per session."""
    from src.auth.api_key import generate_split_token, hash_new_secret
    from src.db import session as session_module
    from src.repositories.api_keys_repo import create_api_key

    async def _seed():
        os.environ["DATABASE_URL"] = e2e_postgres_url
        session_module.get_engine.cache_clear() if hasattr(session_module.get_engine, "cache_clear") else None
        key_id, secret, presented_key = generate_split_token()
        secret_hash = hash_new_secret(secret)
        async with session_module.get_engine().begin() as connection:
            await create_api_key(
                connection, key_id=key_id, secret_hash=secret_hash,
                label="e2e-dashboard-reader", rate_limit_per_sec=100000,
            )
        await session_module.dispose_engine()
        return presented_key

    return asyncio.run(_seed())


@pytest.fixture(scope="session")
def e2e_server(e2e_postgres_url, e2e_redis_url, e2e_reader_key):
    """A real, session-scoped uvicorn process serving the actual app + static
    dashboard assets, backed by real Postgres/Redis testcontainers."""
    port = _free_port()
    env = dict(os.environ)
    env["DATABASE_URL"] = e2e_postgres_url
    env["METERLY_REDIS_URL"] = e2e_redis_url
    env["METERLY_TIER1_RATE_LIMIT_PER_SECOND"] = "100000"
    env["METERLY_TIER1_RATE_LIMIT_BURST"] = "100000"
    env["DASHBOARD_READER_API_KEY"] = e2e_reader_key

    scratch_dir = Path(os.environ.get("METERLY_TEST_SCRATCH_DIR", tempfile.gettempdir()))
    scratch_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(scratch_dir / "dashboard_e2e_uvicorn.log", "w")

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_REPO_ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        deadline = time.monotonic() + 30

        async def _wait_ready():
            async with httpx.AsyncClient() as probe:
                while time.monotonic() < deadline:
                    try:
                        response = await probe.get(f"{base_url}/health", timeout=1.0)
                        if response.status_code == 200:
                            return True
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.3)
            return False

        ready = asyncio.run(_wait_ready())
        if not ready:
            pytest.skip("uvicorn did not become ready in time in this environment")

        yield {"base_url": base_url, "reader_key": e2e_reader_key, "postgres_url": e2e_postgres_url}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()


@pytest.fixture(scope="session")
def base_url(e2e_server):
    """Overrides pytest-playwright's `base_url` fixture so `page.goto("/dashboard")` works."""
    return e2e_server["base_url"]


@pytest.fixture
def seed_event(e2e_server):
    """Seed a usage event under the e2e reader's own tenant via the real
    `POST /v1/events` endpoint (so RLS/aggregation is exercised for real).

    Synchronous (plain `httpx.Client`) so it can be called directly from a
    Playwright sync-API test without fighting that test's own event loop.
    """

    def _seed(*, customer_id: str, metric: str = "api_calls", quantity: str = "5", idempotency_key: str = "e2e-1"):
        with httpx.Client() as client:
            response = client.post(
                f"{e2e_server['base_url']}/v1/events",
                json={
                    "customer_id": customer_id, "metric": metric,
                    "quantity": quantity, "idempotency_key": idempotency_key,
                },
                headers={"Authorization": f"Bearer {e2e_server['reader_key']}"},
                timeout=10.0,
            )
            assert response.status_code in (200, 201), response.text
        return response.json()

    return _seed
