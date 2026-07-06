"""Async database engine/session management.

The engine is created lazily (on first use / at lifespan startup), never at
import time — this is what lets `import src.main` succeed in the smoke-check
build-fallback mode with no database reachable. Every request runs inside a
transaction that sets `app.current_api_key_id` via `SET LOCAL`, which backs the
PostgreSQL row-level-security policy on `events`/`usage_rollup` (defense in
depth behind the application-level `api_key_id` scoping every repository query
already applies).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config.secrets import get_database_url

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call.

    Deferred creation (rather than at import time) keeps `import src.main`
    dependency-free for the smoke-check build-fallback path.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_database_url(),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=5,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def scoped_transaction(api_key_id: int) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to the authenticated `api_key_id`.

    Sets `app.current_api_key_id` with `SET LOCAL` at the start of the
    transaction so the PostgreSQL RLS policy backstop applies even if a
    repository query were ever missing its own `api_key_id` filter. The
    setting is transaction-local and is discarded automatically on commit/
    rollback.
    """
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await session.execute(
            _set_local_statement(),
            {"api_key_id": str(api_key_id)},
        )
        yield session


def _set_local_statement():
    """Build the `SET LOCAL` statement for the current-tenant RLS setting.

    A separate function keeps the (slightly unusual) `set_config` call —
    `SET LOCAL` does not accept a bound parameter directly in all drivers —
    isolated and documented in one place.
    """
    from sqlalchemy import text

    return text("SELECT set_config('app.current_api_key_id', :api_key_id, true)")


async def dispose_engine() -> None:
    """Dispose of the engine's connection pool (called from lifespan shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def check_database_ready() -> bool:
    """Run a fast `SELECT 1` to back the `/health/ready` readiness probe."""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - readiness must never raise, only report False
        return False
