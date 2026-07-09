"""create quotas table and api_keys.scope column

One expand-only revision covering two changes (plan: "ONE expand-only
migration"):

- `quotas`: a create-migration. `down` drops the table, so row survival
  across `down` is undefined by definition — reversibility kind is
  schema + constraints only. `up -> down -> up` must restore the schema and
  re-enforce every PK/FK/CHECK identically (AC16).
- `api_keys.scope`: an expand on a populated table (`DEFAULT 'ingest'`
  backfills existing rows for free, no data migration needed, safe on a live
  table). Pre-existing `api_keys` rows survive `up -> down -> up` unchanged
  except the `scope` column resets to its default on `down` — the defined
  expand/contract contract of an add-column, not data loss of a populated
  business column (AC16).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08 00:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create `quotas` (per-tenant per-customer per-metric usage caps) and add
    `api_keys.scope` (authorization attribute: `'ingest'` default, `'admin'`
    elevated for `PUT /v1/quotas`)."""
    op.create_table(
        "quotas",
        sa.Column("api_key_id", sa.BigInteger(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("limit_per_window", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("api_key_id", "customer_id", "metric", name="pk_quotas"),
        sa.CheckConstraint("limit_per_window >= 1", name="ck_quotas_limit_per_window_positive"),
    )

    # Row-level security backstop (mirrors events/usage_rollup — plan
    # §"Row-level security"); the primary control is the explicit
    # `api_key_id = :api_key_id` filter every quotas query already applies.
    op.execute("ALTER TABLE quotas ENABLE ROW LEVEL SECURITY")
    # FORCE so the policy binds even for the table OWNER. The migration job
    # resolves the DB URL through the same secrets facade the app uses
    # (alembic/env.py -> get_database_url -> the meterly_app credential in
    # Secrets Manager), so meterly_app — which holds CREATE ON SCHEMA public —
    # is the table owner. A table owner bypasses non-FORCE RLS regardless of
    # NOBYPASSRLS, which would leave quotas_tenant_isolation inert for the app
    # role (plan Open Question 2). FORCE makes the backstop effective; the app
    # path is unaffected because every quotas access runs inside
    # scoped_transaction, which SET LOCAL app.current_api_key_id so the policy's
    # USING/WITH CHECK predicate passes for the caller's own rows.
    op.execute("ALTER TABLE quotas FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY quotas_tenant_isolation ON quotas
        USING (api_key_id = current_setting('app.current_api_key_id', true)::bigint)
        """
    )

    # `DEFAULT 'ingest'` backfills every existing row with no data migration
    # (Postgres 11+ does not rewrite the table for a non-volatile default),
    # so this is safe to run against a live, populated api_keys table.
    op.add_column(
        "api_keys",
        sa.Column("scope", sa.Text(), nullable=False, server_default="ingest"),
    )
    op.execute(
        "ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_scope CHECK (scope IN ('ingest', 'admin'))"
    )


def downgrade() -> None:
    """Drop `quotas` (schema-only rollback — row survival not asserted) and
    remove `api_keys.scope` (the pre-existing rows and their other columns
    survive; only the `scope` column itself disappears)."""
    op.drop_table("quotas")
    op.drop_constraint("ck_api_keys_scope", "api_keys", type_="check")
    op.drop_column("api_keys", "scope")
