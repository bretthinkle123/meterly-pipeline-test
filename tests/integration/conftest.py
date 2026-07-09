"""Shared fixtures for integration tests: a real PostgreSQL + Redis, each in a
testcontainer, migrated with the project's actual Alembic revisions and wired
into the app's process-wide singletons so requests through the ASGI app hit
real dependencies — the correctness guarantees under test here (ON CONFLICT
concurrency, RLS tenant isolation, migration round-trips) are only real
against actual Postgres, not a mock.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _asyncpg_url(sync_url: str) -> str:
    """Convert testcontainers' default `postgresql+psycopg2://` URL to the
    `postgresql+asyncpg://` dialect the application (and Alembic's env.py) uses."""
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest.fixture(scope="session")
def postgres_url():
    """A session-scoped live Postgres, migrated to head once.

    Raises `max_connections` above Postgres's default of 100: this container
    is shared across the whole integration-test session, including three k6
    perf fixtures that each launch a separate multi-worker uvicorn process
    (its own DB connection pool) against it — the default ceiling is too low
    for that cumulative demand when the full suite runs in one session and
    was observed to exhaust mid-suite (`TooManyConnectionsError`) with only
    the default. This is a test-container capacity knob, not a production
    change (the real RDS instance sizes `max_connections` independently).
    """
    with PostgresContainer("postgres:16-alpine").with_command(
        "postgres -c max_connections=300"
    ) as postgres:
        url = _asyncpg_url(postgres.get_connection_url())
        env = dict(os.environ)
        env["DATABASE_URL"] = url
        # Run the project's real Alembic migrations (0001 + 0002) against the
        # container out-of-process, so this is the same upgrade path `alembic
        # upgrade head` runs in CI/deploy, not a re-implemented schema.
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"
        yield url


@pytest.fixture(scope="session")
def redis_url():
    """A session-scoped live Redis for the Tier-1/Tier-2 token-bucket limiters."""
    with RedisContainer("redis:7-alpine") as redis_container:
        host = redis_container.get_container_host_ip()
        port = redis_container.get_exposed_port(redis_container.port)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def app_env(postgres_url, redis_url, monkeypatch):
    """Point the app's process-wide DB/Redis singletons at the live
    containers, then reset them so each test starts with fresh pools/caches
    and no leaked verification-cache entries from a previous test.
    """
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("METERLY_REDIS_URL", redis_url)

    from src import auth as auth_module
    from src.auth import rate_limit as rate_limit_module
    from src.config import secrets as secrets_module
    from src.config.settings import get_settings
    from src.db import session as session_module

    get_settings.cache_clear()
    secrets_module._facade._cache.clear()

    await session_module.dispose_engine()
    await rate_limit_module.dispose_redis_client()
    auth_module._verification_cache = None

    yield

    await session_module.dispose_engine()
    await rate_limit_module.dispose_redis_client()
    auth_module._verification_cache = None
    get_settings.cache_clear()


@pytest.fixture
async def truncate_tables(app_env, postgres_url, redis_url):
    """Truncate the mutable tables between tests so each test starts from a
    clean, empty schema (api_keys are re-seeded per test via `make_api_key`).

    The DB truncate uses RESTART IDENTITY, so `api_keys.id` (the Tier-2 bucket
    key) resets to 1 each test. Redis is session-scoped and would otherwise
    retain the previous test's token buckets under those reused ids, leaking an
    exhausted bucket into the next test -> flush it here too so bucket state is
    isolated per test, mirroring the DB truncate.
    """
    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        from sqlalchemy import text

        await connection.execute(
            text("TRUNCATE TABLE events, usage_rollup, quotas, api_keys RESTART IDENTITY CASCADE")
        )
    await engine.dispose()

    redis_client = redis_async.from_url(redis_url, decode_responses=True)
    await redis_client.flushdb()
    await redis_client.aclose()
    yield


@pytest.fixture
async def client(truncate_tables):
    """An async test client bound to the real app via ASGI transport, with
    live DB/Redis singletons already pointed at the containers."""
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def make_api_key(truncate_tables):
    """Factory fixture: provision a real API key row (Argon2id-hashed) and
    return `(presented_key, api_key_id)`."""
    from src.auth.api_key import generate_split_token, hash_new_secret
    from src.db.session import get_engine
    from src.repositories.api_keys_repo import create_api_key

    created_ids = []

    async def _make(
        label: str = "test-key", rate_limit_per_sec: int = 100, scope: str = "ingest"
    ) -> tuple[str, int]:
        key_id, secret, presented_key = generate_split_token()
        secret_hash = hash_new_secret(secret)
        async with get_engine().begin() as connection:
            api_key_row_id = await create_api_key(
                connection,
                key_id=key_id,
                secret_hash=secret_hash,
                label=label,
                rate_limit_per_sec=rate_limit_per_sec,
                scope=scope,
            )
        created_ids.append(api_key_row_id)
        return presented_key, api_key_row_id

    yield _make
