"""create api_keys and events

A create-migration: `down` drops both tables in FK-safe order. Reversibility
kind is schema + constraints (row survival across `down` is undefined by
definition for a create-migration — `down` drops the tables); AC-MIGRATION-1
asserts `up -> down -> up` restores the schema and re-enforces every
CHECK/FK/UNIQUE/NOT NULL identically.

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create `api_keys` (the tenant/credential table) and `events` (the
    append-only ingest log), with the UNIQUE constraint that backs the
    idempotency guarantee and the index the backfill/operational queries need.
    """
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("rate_limit_per_sec", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_id", name="uq_api_keys_key_id"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("api_key_id", sa.BigInteger(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quantity > 0", name="ck_events_quantity_positive"),
        sa.UniqueConstraint("api_key_id", "idempotency_key", name="uq_events_api_key_idempotency_key"),
    )
    op.create_index(
        "ix_events_api_key_customer_metric_window",
        "events",
        ["api_key_id", "customer_id", "metric", "window_start"],
    )

    # Row-level security backstop (defense-in-depth behind the application-level
    # api_key_id scoping every repository query applies — plan §"Row-level security").
    op.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY events_tenant_isolation ON events
        USING (api_key_id = current_setting('app.current_api_key_id', true)::bigint)
        """
    )


def downgrade() -> None:
    """Drop `events` then `api_keys` (FK-safe order). A create-migration's
    `down` is a schema-only rollback — row survival is not asserted here."""
    op.drop_table("events")
    op.drop_table("api_keys")
