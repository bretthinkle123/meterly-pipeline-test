"""Liveness and readiness endpoints.

Split per `containerization-conventions`: `/health` is liveness (process is
up, no dependency checks — must pass with no database reachable, which is
what the local smoke check and `import src.main` build-fallback rely on);
`/health/ready` is readiness (checks the database and migration head) and is
what the ALB target group and the canary soak probe, so traffic is gated on
real dependency health.
"""

from fastapi import APIRouter
from sqlalchemy import text
from starlette.responses import JSONResponse

from src.db.session import check_database_ready, get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict:
    """Liveness probe: returns 200 whenever the process is up. No I/O, no dependencies."""
    return {"status": "ok"}


async def _current_migration_is_head() -> bool:
    """Compare the database's applied Alembic revision against the code's
    migration head — a mismatch means the deployed image is ahead of (or
    behind) the schema it expects, which readiness should fail on."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script_directory = ScriptDirectory.from_config(Config("alembic.ini"))
    expected_head = script_directory.get_current_head()

    async with get_engine().connect() as connection:
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        applied_revision = result.scalar_one_or_none()

    return applied_revision == expected_head


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """Readiness probe: 200 only if the database is reachable and at the
    expected migration head.

    A readiness endpoint that returns 200 unconditionally defeats the canary
    health gate (`containerization-conventions`), so this performs a real,
    fast `SELECT 1` plus a migration-head comparison rather than a static
    response.
    """
    if not await check_database_ready():
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unreachable"})

    try:
        at_head = await _current_migration_is_head()
    except Exception:  # noqa: BLE001 - readiness must report, never raise
        at_head = False

    if not at_head:
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "schema_mismatch"})

    return JSONResponse(status_code=200, content={"status": "ready", "database": "reachable"})
