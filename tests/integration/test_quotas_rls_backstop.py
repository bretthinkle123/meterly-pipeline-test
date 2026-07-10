"""AC6 backstop proof: the `quotas_tenant_isolation` PostgreSQL RLS policy
(migration `0003`) must independently enforce tenant isolation even when the
primary control -- the application's explicit `api_key_id = :api_key_id`
filter every `quotas_repo` query applies -- is entirely absent from the
query.

Every other AC6 test in `test_quotas_endpoint.py` (`test_quota_is_tenant_isolated`)
goes through the app's own repository functions, which always include the
explicit filter -- so it can never distinguish "isolation because of the
filter" from "isolation because of RLS". It also connects to the database
via the same superuser testcontainers provisions, and PostgreSQL superusers
bypass RLS entirely regardless of `FORCE ROW LEVEL SECURITY` -- so even a raw,
filter-less query issued over the shared `postgres_url` fixture would not
actually exercise the policy.

This test closes both gaps: it provisions a `NOBYPASSRLS` role mirroring the
production `meterly_app` role (`infra/modules/data/main.tf`), connects as
that role, issues a query with **no** `api_key_id` predicate at all (the
primary control disabled/missing), and asserts the RLS policy alone still
returns only the session's own tenant rows -- proving the backstop actually
fires, not merely that it is declared.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
async def nobypassrls_role(postgres_url):
    """Create (or reuse) a NOBYPASSRLS login role with plain CRUD grants on
    `quotas` -- mirroring the least-privilege `meterly_app` role Terraform
    provisions in production (`infra/modules/data/main.tf`), so this test
    proves the RLS policy against a role that cannot bypass it, not against
    the testcontainers superuser."""
    role_name = f"rls_test_role_{uuid.uuid4().hex[:8]}"
    role_password = "test-role-password"

    admin_engine = create_async_engine(postgres_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(f"CREATE ROLE {role_name} LOGIN PASSWORD '{role_password}' NOBYPASSRLS")
        )
        await connection.execute(text(f"GRANT CONNECT ON DATABASE postgres TO {role_name}"))
        await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_name}"))
        await connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON quotas TO {role_name}")
        )
    await admin_engine.dispose()

    # Build a connection URL for the new role against the same database.
    from sqlalchemy.engine import make_url

    base_url = make_url(postgres_url)
    role_url = base_url.set(username=role_name, password=role_password)

    # `str(URL)` masks the password as literal "***" -- render_as_string with
    # hide_password=False is required to get the real, connectable DSN.
    yield role_url.render_as_string(hide_password=False)

    cleanup_engine = create_async_engine(postgres_url)
    async with cleanup_engine.begin() as connection:
        await connection.execute(text(f"REVOKE ALL PRIVILEGES ON quotas FROM {role_name}"))
        await connection.execute(text(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role_name}"))
        await connection.execute(text(f"REVOKE ALL PRIVILEGES ON DATABASE postgres FROM {role_name}"))
    async with cleanup_engine.begin() as connection:
        await connection.execute(text(f"DROP ROLE IF EXISTS {role_name}"))
    await cleanup_engine.dispose()


async def test_rls_blocks_cross_tenant_read_when_app_filter_is_absent(
    postgres_url, nobypassrls_role, make_api_key
):
    """With the explicit `api_key_id` filter entirely removed from the query
    (simulating the primary control being missing/buggy), the RLS policy
    alone must still confine a `NOBYPASSRLS` session to only its own
    tenant's rows."""
    _, tenant_a_id = await make_api_key(label="rls-tenant-a", scope="admin")
    _, tenant_b_id = await make_api_key(label="rls-tenant-b", scope="admin")

    # Seed one quota row per tenant directly (superuser bypasses RLS for setup).
    admin_engine = create_async_engine(postgres_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'rls_cust', 'api_calls', 1000)"
            ),
            {"id": tenant_a_id},
        )
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'rls_cust', 'api_calls', 2000)"
            ),
            {"id": tenant_b_id},
        )
    await admin_engine.dispose()

    role_engine = create_async_engine(nobypassrls_role)
    try:
        async with role_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_api_key_id', :id, true)"),
                {"id": str(tenant_a_id)},
            )
            # No api_key_id predicate at all -- the primary control is absent.
            rows = (
                await connection.execute(text("SELECT api_key_id FROM quotas"))
            ).mappings().all()
        seen_ids = {row["api_key_id"] for row in rows}
        assert seen_ids == {tenant_a_id}, (
            "RLS must confine the NOBYPASSRLS session to its own tenant's rows "
            f"even with no application-level filter; saw api_key_ids={seen_ids}"
        )

        # A cross-tenant UPDATE with no filter must likewise touch zero rows.
        async with role_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_api_key_id', :id, true)"),
                {"id": str(tenant_a_id)},
            )
            result = await connection.execute(
                text("UPDATE quotas SET limit_per_window = 1 WHERE customer_id = 'rls_cust'")
            )
        assert result.rowcount == 1, "the unfiltered UPDATE must only affect tenant A's own row"
    finally:
        await role_engine.dispose()


async def test_rls_blocks_cross_tenant_delete_when_app_filter_is_absent(
    postgres_url, nobypassrls_role, make_api_key
):
    """AC8/AC15 backstop proof for the quota-admin feature's new `DELETE
    /v1/quotas` surface: with the explicit `api_key_id` filter entirely
    removed from the query (simulating the primary control being
    missing/buggy in `quotas_repo.delete_quota`), the RLS policy alone must
    still confine a `NOBYPASSRLS` session's DELETE to only its own tenant's
    row -- tenant B's row must survive an unfiltered DELETE issued under
    tenant A's session setting.
    """
    _, tenant_a_id = await make_api_key(label="rls-delete-tenant-a", scope="admin")
    _, tenant_b_id = await make_api_key(label="rls-delete-tenant-b", scope="admin")

    admin_engine = create_async_engine(postgres_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'rls_del_cust', 'api_calls', 1000)"
            ),
            {"id": tenant_a_id},
        )
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'rls_del_cust', 'api_calls', 2000)"
            ),
            {"id": tenant_b_id},
        )
    await admin_engine.dispose()

    role_engine = create_async_engine(nobypassrls_role)
    try:
        async with role_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_api_key_id', :id, true)"),
                {"id": str(tenant_a_id)},
            )
            # No api_key_id predicate at all -- mirrors delete_quota's DELETE
            # statement with its primary WHERE clause stripped out.
            result = await connection.execute(
                text("DELETE FROM quotas WHERE customer_id = 'rls_del_cust' RETURNING api_key_id")
            )
            deleted_ids = {row[0] for row in result.fetchall()}
        assert deleted_ids == {tenant_a_id}, (
            "an unfiltered DELETE under tenant A's session setting must only "
            f"remove tenant A's own row; deleted api_key_ids={deleted_ids}"
        )

        admin_check_engine = create_async_engine(postgres_url)
        async with admin_check_engine.connect() as connection:
            survivor = (
                await connection.execute(
                    text(
                        "SELECT limit_per_window FROM quotas "
                        "WHERE api_key_id = :id AND customer_id = 'rls_del_cust'"
                    ),
                    {"id": tenant_b_id},
                )
            ).mappings().one()
        await admin_check_engine.dispose()
        assert survivor["limit_per_window"] == 2000, "tenant B's row must survive the cross-tenant DELETE"
    finally:
        await role_engine.dispose()


async def test_rls_denies_all_rows_when_tenant_setting_is_unset(
    postgres_url, nobypassrls_role, make_api_key
):
    """Fail-closed check: if `app.current_api_key_id` is never set at all (a
    session that skipped `scoped_transaction`'s `SET LOCAL`), the RLS policy
    must return zero rows rather than defaulting open."""
    _, tenant_id = await make_api_key(label="rls-tenant-unset", scope="admin")

    admin_engine = create_async_engine(postgres_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window) "
                "VALUES (:id, 'rls_cust_unset', 'api_calls', 500)"
            ),
            {"id": tenant_id},
        )
    await admin_engine.dispose()

    role_engine = create_async_engine(nobypassrls_role)
    try:
        async with role_engine.begin() as connection:
            rows = (
                await connection.execute(
                    text("SELECT api_key_id FROM quotas WHERE customer_id = 'rls_cust_unset'")
                )
            ).mappings().all()
        assert rows == [], "with no tenant setting configured, RLS must fail closed (zero rows)"
    finally:
        await role_engine.dispose()
