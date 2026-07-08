"""AC21: no new runtime dependency and no migration for the dashboard
feature; the two proposed dev/test deps are pinned exactly (Q4)."""

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The runtime dependency set that existed before this feature (feature 1/2,
# commit faabe9d) — the dashboard must not add to it.
_PRE_EXISTING_RUNTIME_DEPS = {
    "python", "fastapi", "starlette", "uvicorn", "gunicorn", "pydantic",
    "pydantic-settings", "sqlalchemy", "asyncpg", "alembic", "argon2-cffi",
    "redis", "structlog", "sentry-sdk", "opentelemetry-sdk",
    "opentelemetry-instrumentation-fastapi", "opentelemetry-exporter-otlp", "boto3",
}


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def test_no_new_runtime_dependency_added():
    """The `[tool.poetry.dependencies]` runtime set is unchanged — the page
    is served via FastAPI's built-in `FileResponse`, no template engine or
    frontend framework was added."""
    config = _pyproject()
    runtime_deps = set(config["tool"]["poetry"]["dependencies"].keys())
    assert runtime_deps == _PRE_EXISTING_RUNTIME_DEPS, (
        f"runtime dependency set changed: added={runtime_deps - _PRE_EXISTING_RUNTIME_DEPS}, "
        f"removed={_PRE_EXISTING_RUNTIME_DEPS - runtime_deps}"
    )


def test_playwright_dev_deps_are_pinned_exactly():
    """The two proposed dev/test deps carry an exact pin, not a wildcard
    (`code-standards` exact-pin rule)."""
    config = _pyproject()
    dev_deps = config["tool"]["poetry"]["group"]["dev"]["dependencies"]
    assert dev_deps.get("playwright") == "1.61.0"
    assert dev_deps.get("pytest-playwright") == "0.8.0"
    for name in ("playwright", "pytest-playwright"):
        value = dev_deps[name]
        assert re.fullmatch(r"\d+\.\d+\.\d+", value), f"{name} must be an exact pin, got {value!r}"


def test_no_new_alembic_migration_file():
    """No schema change accompanies this feature — only the two migrations
    from feature 1/2 (0001, 0002) exist; the migration round-trip test mode
    does not trigger for this change set."""
    versions_dir = _REPO_ROOT / "alembic" / "versions"
    migration_files = sorted(p.name for p in versions_dir.glob("*.py") if not p.name.startswith("__"))
    assert migration_files == [
        "0001_create_api_keys_and_events.py",
        "0002_create_usage_rollup_backfill.py",
    ], f"unexpected migration files present: {migration_files}"
