"""Backstop proof for the `events_tenant_isolation` PostgreSQL RLS policy
(migration `0001` creates the policy, `0005` makes it effective for the table
owner via `FORCE ROW LEVEL SECURITY`).

The plan names this policy as the defense-in-depth backstop for the
Elevation-of-Privilege / Information-Disclosure threat on `events` reads and
writes -- the layer meant to confine a query to its own tenant even if the
primary application control (the explicit `api_key_id = :api_key_id` filter
every repository query applies) were ever missing or buggy.

Why this test connects as the table **owner**, not the `NOBYPASSRLS` non-owner
role used by `test_quotas_rls_backstop.py`: the security finding this test
guards is *owner bypass*. In production the app connects as `meterly_app`,
which **owns** `events` (the migration job creates the table as that role). In
PostgreSQL a table owner **bypasses** a policy under plain `ENABLE ROW LEVEL
SECURITY`; only `FORCE ROW LEVEL SECURITY` binds the owner too. Migration
`0001` ran `ENABLE` without `FORCE`, so the backstop was inert for the app
role. A non-owner `NOBYPASSRLS` role is subject to RLS under `ENABLE` alone,
so it could not distinguish the pre-0005 (inert) state from the post-0005
(effective) state -- it would pass either way. Connecting as a non-superuser
*owner* is the only role class whose behavior actually flips on `FORCE`, so it
is the genuine fails-before-0005 / passes-after-0005 witness for this fix.

(The testcontainers superuser that provisions the DB bypasses RLS entirely --
`FORCE` or not -- so a query over the shared superuser `postgres_url` fixture
would never exercise the policy; hence the dedicated non-superuser owner
role.)
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

_WINDOW_START = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)


async def _seed_events(engine_url: str, *, api_key_id: int, idempotency_key: str) -> None:
    """Insert one `events` row for a tenant, connecting as the testcontainers
    superuser (which bypasses RLS, so setup is unconstrained). Supplies every
    NOT NULL column without a server default and satisfies
    `ck_events_quantity_positive` and the idempotency-key UNIQUE constraint."""
    admin_engine = create_async_engine(engine_url)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO events "
                    "(api_key_id, customer_id, metric, quantity, idempotency_key, window_start) "
                    "VALUES (:id, 'ev_rls_cust', 'api_calls', :quantity, :idempotency_key, :window_start)"
                ),
                {
                    "id": api_key_id,
                    "quantity": 1,
                    "idempotency_key": idempotency_key,
                    "window_start": _WINDOW_START,
                },
            )
    finally:
        await admin_engine.dispose()


@pytest.fixture
async def events_owner_role(postgres_url):
    """Create a non-superuser login role and hand it ownership of `events`
    for the duration of the test, mirroring how the production `meterly_app`
    role owns the table it created via migrations.

    A non-superuser table owner is the one role class whose RLS visibility
    depends on `FORCE`: under plain `ENABLE` it bypasses the policy, under
    `FORCE` it is bound by it. Original ownership is restored in teardown so
    the session-scoped container is left exactly as found for other tests.
    """
    role_name = f"ev_rls_owner_{uuid.uuid4().hex[:8]}"
    role_password = "test-owner-password"

    admin_engine = create_async_engine(postgres_url)
    async with admin_engine.begin() as connection:
        original_owner = (
            await connection.execute(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'events'")
            )
        ).scalar_one()
        # Non-superuser, NOBYPASSRLS login role. CREATE ON SCHEMA is required for
        # the role to be allowed to own a table in that schema.
        # nosemgrep triage (avoid-sqlalchemy-text): these are test-only role/grant
        # DDL statements against a disposable testcontainer. The only interpolated
        # values are a UUID-derived role name, a static test literal password, and a
        # DB-internal pg_tables.tableowner identifier — none user-controlled, and
        # PostgreSQL DDL cannot bind identifiers as parameters. Mirrors the accepted
        # pattern in usage_repo.py and the sibling RLS-backstop tests.
        await connection.execute(
            text(f"CREATE ROLE {role_name} LOGIN PASSWORD '{role_password}' NOBYPASSRLS")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        )
        await connection.execute(text(f"GRANT CONNECT ON DATABASE postgres TO {role_name}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await connection.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {role_name}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await connection.execute(text(f"ALTER TABLE events OWNER TO {role_name}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    await admin_engine.dispose()

    base_url = make_url(postgres_url)
    role_url = base_url.set(username=role_name, password=role_password)
    # str(URL) masks the password as "***"; render_as_string(hide_password=False)
    # yields the real connectable DSN.
    try:
        yield role_url.render_as_string(hide_password=False)
    finally:
        cleanup_engine = create_async_engine(postgres_url)
        async with cleanup_engine.begin() as connection:
            await connection.execute(
                text(f"ALTER TABLE events OWNER TO {original_owner}")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            )
            await connection.execute(
                text(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role_name}")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            )
            await connection.execute(
                text(f"REVOKE ALL PRIVILEGES ON DATABASE postgres FROM {role_name}")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            )
        async with cleanup_engine.begin() as connection:
            await connection.execute(text(f"DROP ROLE IF EXISTS {role_name}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await cleanup_engine.dispose()


async def test_rls_confines_table_owner_read_when_app_filter_is_absent(
    postgres_url, events_owner_role, make_api_key
):
    """With the explicit `api_key_id` filter entirely removed from the query
    (simulating the primary control being missing/buggy) and connecting as the
    non-superuser table *owner* (the production app-role class), the RLS
    policy alone must confine the session to its own tenant's rows.

    Fails before migration `0005` (owner bypasses the non-FORCE policy -> sees
    both tenants); passes after (`FORCE` binds the owner -> sees only its
    own).
    """
    _, tenant_a_id = await make_api_key(label="ev-rls-tenant-a")
    _, tenant_b_id = await make_api_key(label="ev-rls-tenant-b")

    await _seed_events(postgres_url, api_key_id=tenant_a_id, idempotency_key="ev-rls-a-1")
    await _seed_events(postgres_url, api_key_id=tenant_b_id, idempotency_key="ev-rls-b-1")

    role_engine = create_async_engine(events_owner_role)
    try:
        async with role_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_api_key_id', :id, true)"),
                {"id": str(tenant_a_id)},
            )
            # No api_key_id predicate at all -- the primary control is absent.
            rows = (
                await connection.execute(text("SELECT api_key_id FROM events"))
            ).mappings().all()
        seen_ids = {row["api_key_id"] for row in rows}
        assert seen_ids == {tenant_a_id}, (
            "the RLS backstop must confine the table owner to its own tenant's "
            "events rows even with no application-level filter (requires "
            f"FORCE ROW LEVEL SECURITY); saw api_key_ids={seen_ids}"
        )
    finally:
        await role_engine.dispose()


async def test_rls_denies_all_rows_to_owner_when_tenant_setting_is_unset(
    postgres_url, events_owner_role, make_api_key
):
    """Fail-closed check: connecting as the table owner with
    `app.current_api_key_id` never set (a session that skipped
    `scoped_transaction`'s `SET LOCAL`), the RLS policy must return zero rows
    rather than defaulting open.

    Fails before migration `0005` (owner bypasses the non-FORCE policy -> sees
    every tenant's rows despite the unset setting); passes after.
    """
    _, tenant_a_id = await make_api_key(label="ev-rls-unset-a")
    _, tenant_b_id = await make_api_key(label="ev-rls-unset-b")

    await _seed_events(postgres_url, api_key_id=tenant_a_id, idempotency_key="ev-rls-unset-a-1")
    await _seed_events(postgres_url, api_key_id=tenant_b_id, idempotency_key="ev-rls-unset-b-1")

    role_engine = create_async_engine(events_owner_role)
    try:
        async with role_engine.begin() as connection:
            # Deliberately do NOT set app.current_api_key_id.
            rows = (
                await connection.execute(text("SELECT api_key_id FROM events"))
            ).mappings().all()
        assert rows == [], (
            "with no tenant setting configured the RLS backstop must fail closed "
            f"(zero rows) even for the table owner; saw {rows}"
        )
    finally:
        await role_engine.dispose()
