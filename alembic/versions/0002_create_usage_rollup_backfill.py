"""create usage_rollup and backfill from events

An expand + backfill migration. `up` creates `usage_rollup` then derives it
from the existing `events` rows via `GROUP BY`; `down` drops only
`usage_rollup` and leaves `events` untouched. Reversibility kind: the seeded
`events` rows are preserved across `up -> down -> up` (this migration never
mutates `events`), and `usage_rollup` is deterministically re-derived from
them each `up` — so `down` "losing" the rollup is expected (it is pure
derivation, rebuildable from the source of truth). AC-MIGRATION asserts this
round-trip on a prod-shaped seeded dataset.

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-02 00:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create `usage_rollup` and backfill it from `events` via a single
    deterministic `GROUP BY` aggregate.

    Safe on a live system in the classic expand/backfill sense (events
    written *during* the backfill wouldn't be missed) because this is the
    first deploy with no pre-existing traffic and `deploy.yml` runs
    snapshot -> migrate -> roll out, so migrations complete before any
    dual-writing code serves traffic (see the plan's expand/contract note).
    """
    op.create_table(
        "usage_rollup",
        sa.Column("api_key_id", sa.BigInteger(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_quantity", sa.Numeric(38, 6), nullable=False),
        sa.Column("event_count", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint(
            "api_key_id", "customer_id", "metric", "window_start", name="pk_usage_rollup"
        ),
    )

    op.execute(
        """
        INSERT INTO usage_rollup (
            api_key_id, customer_id, metric, window_start, total_quantity, event_count, updated_at
        )
        SELECT api_key_id, customer_id, metric, window_start, SUM(quantity), COUNT(*), now()
        FROM events
        GROUP BY api_key_id, customer_id, metric, window_start
        """
    )

    # Same RLS backstop as `events` (plan §"Row-level security") — a policy on
    # the derived aggregate too, since it holds the same per-tenant totals.
    op.execute("ALTER TABLE usage_rollup ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY usage_rollup_tenant_isolation ON usage_rollup
        USING (api_key_id = current_setting('app.current_api_key_id', true)::bigint)
        """
    )


def downgrade() -> None:
    """Drop `usage_rollup` only — `events`, the source of truth, is untouched
    (so `up` afterward re-derives an identical rollup from the same rows)."""
    op.drop_table("usage_rollup")
