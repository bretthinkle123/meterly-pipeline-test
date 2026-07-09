"""AC16: migration `0003` (quotas table + `api_keys.scope` column) round-trips
`up -> down -> 0002 -> up` and re-enforces every constraint identically.

`quotas` is a create-migration (its `down` drops the table — row survival
across `down` is undefined by definition, only schema + constraints are
asserted). `api_keys.scope` is an add-column on a populated table — its
pre-existing rows (and their other columns) must survive the round-trip; the
`scope` value itself is expected to reset to the `'ingest'` default on `down`
(the defined expand/contract contract of an add-column), not a data-loss bug.

Uses its own scratch Postgres testcontainer (module-scoped), independent of
the shared session-scoped `postgres_url` fixture in `tests/integration/conftest.py`,
since this test owns the migration lifecycle itself (down to 0002, back to head).
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _asyncpg_url(sync_url: str) -> str:
    """Convert testcontainers' default `postgresql+psycopg2://` URL to the
    `postgresql+asyncpg://` dialect the application (and Alembic's env.py) uses."""
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


def _run_alembic(*args: str, env: dict) -> None:
    """Run `alembic <args>` out-of-process against `env`'s `DATABASE_URL`,
    failing loudly with the captured output if the migration itself fails."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def migration_env():
    """A scratch Postgres this test owns end to end (migrated up/down/up
    repeatedly), independent of the shared session-scoped container."""
    import os

    with PostgresContainer("postgres:16-alpine") as postgres:
        url = _asyncpg_url(postgres.get_connection_url())
        env = dict(os.environ)
        env["DATABASE_URL"] = url
        yield env, url


async def _seed_api_key(url: str, *, key_id: str, scope: str | None = None) -> int:
    """Insert one `api_keys` row (optionally with an explicit `scope`) and
    return its surrogate id."""
    engine = create_async_engine(url)
    columns = "key_id, secret_hash, label, rate_limit_per_sec"
    values = ":key_id, :hash, :label, :rate"
    params = {"key_id": key_id, "hash": f"$argon2id$fake${key_id}", "label": key_id, "rate": 100}
    if scope is not None:
        columns += ", scope"
        values += ", :scope"
        params["scope"] = scope
    async with engine.begin() as connection:
        result = await connection.execute(
            text(f"INSERT INTO api_keys ({columns}) VALUES ({values}) RETURNING id"), params
        )
        api_key_id = result.scalar_one()
    await engine.dispose()
    return api_key_id


async def _fetch_api_key_row(url: str, api_key_id: int) -> dict:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT id, key_id, secret_hash, label, rate_limit_per_sec, scope FROM api_keys WHERE id = :id"),
                {"id": api_key_id},
            )
        ).mappings().first()
    await engine.dispose()
    return dict(row)


async def test_0003_roundtrip_schema_and_constraints(migration_env):
    """AC16: `0003` up -> down (to 0002) -> up restores the `quotas` schema and
    re-enforces its PK/FK/CHECK, restores the `api_keys.scope` CHECK, and
    leaves pre-existing `api_keys` rows (other than `scope`, which resets to
    its default) intact."""
    env, url = migration_env

    _run_alembic("upgrade", "head", env=env)

    admin_key_id = await _seed_api_key(url, key_id="admin-key", scope="admin")
    ingest_key_id = await _seed_api_key(url, key_id="ingest-key")  # default scope

    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'cust_1', 'api_calls', 1000)"
            ),
            {"id": admin_key_id},
        )
    await engine.dispose()

    _run_alembic("downgrade", "0002", env=env)

    # `quotas` must be gone entirely after downgrade (create-migration: schema
    # rollback only, row survival across `down` is not asserted).
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        table_exists = (
            await connection.execute(
                text("SELECT to_regclass('public.quotas') IS NOT NULL")
            )
        ).scalar_one()
    await engine.dispose()
    assert table_exists is False, "quotas must not exist after downgrading past 0003"

    # The pre-existing api_keys rows must survive the column drop (only the
    # `scope` column itself disappears — the rows are untouched otherwise).
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        surviving_count = (
            await connection.execute(text("SELECT COUNT(*) FROM api_keys"))
        ).scalar_one()
        has_scope_column = (
            await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'api_keys' AND column_name = 'scope')"
                )
            )
        ).scalar_one()
    await engine.dispose()
    assert surviving_count == 2, "the two pre-existing api_keys rows must survive the downgrade"
    assert has_scope_column is False, "the scope column itself must be dropped on downgrade"

    _run_alembic("upgrade", "head", env=env)

    # Re-enforce: PK (api_key_id, customer_id, metric) rejects a duplicate.
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'cust_1', 'api_calls', 500)"
            ),
            {"id": admin_key_id},
        )
        with pytest.raises(Exception):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                        "VALUES (:id, 'cust_1', 'api_calls', 999)"
                    ),
                    {"id": admin_key_id},
                )

        # FK: api_key_id must reference an existing api_keys row.
        with pytest.raises(Exception):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                        "VALUES (999999, 'cust_2', 'api_calls', 100)"
                    )
                )

        # CHECK (limit_per_window >= 1): zero and negative are rejected.
        with pytest.raises(Exception):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                        "VALUES (:id, 'cust_3', 'api_calls', 0)"
                    ),
                    {"id": admin_key_id},
                )
        with pytest.raises(Exception):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                        "VALUES (:id, 'cust_4', 'api_calls', -5)"
                    ),
                    {"id": admin_key_id},
                )

        # api_keys.scope CHECK IN ('ingest', 'admin') must still be enforced.
        with pytest.raises(Exception):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO api_keys (key_id, secret_hash, label, rate_limit_per_sec, scope) "
                        "VALUES ('bad-scope-key', '$argon2id$x', 'bad', 100, 'superuser')"
                    )
                )
    await engine.dispose()

    # The pre-existing rows survive the full round-trip; scope resets to the
    # 'ingest' default (the defined expand/contract behavior of an add-column,
    # not data loss of a populated business column).
    admin_row = await _fetch_api_key_row(url, admin_key_id)
    ingest_row = await _fetch_api_key_row(url, ingest_key_id)
    assert admin_row["key_id"] == "admin-key"
    assert admin_row["scope"] == "ingest", "scope resets to the column default after down->up, per plan"
    assert ingest_row["key_id"] == "ingest-key"
    assert ingest_row["scope"] == "ingest"

    # Leave the schema at head for any subsequent module-scoped use.
    _run_alembic("upgrade", "head", env=env)
