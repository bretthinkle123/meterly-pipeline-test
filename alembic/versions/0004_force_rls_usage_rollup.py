"""force row-level security on usage_rollup

Security remediation (defense-in-depth backstop repair): make the
`usage_rollup_tenant_isolation` RLS policy effective for the table OWNER.

Migration `0002` created `usage_rollup` and ran `ALTER TABLE usage_rollup
ENABLE ROW LEVEL SECURITY` + `CREATE POLICY usage_rollup_tenant_isolation`, but
NOT `FORCE`. The migration job resolves the DB URL through the same secrets
facade the app uses (alembic/env.py -> get_database_url -> the meterly_app
credential in Secrets Manager), so `meterly_app` -- which holds CREATE ON SCHEMA
public -- is the table OWNER, and the runtime app also connects as `meterly_app`.
A table owner BYPASSES non-FORCE RLS regardless of NOBYPASSRLS, so the policy was
inert for the app role: a named defense-in-depth control with no efficacy.

This mirrors migration `0003`, which already applied FORCE to `quotas` for
exactly this owner-bypass reason. It is BEHAVIOR-PRESERVING: every runtime
reader/writer of `usage_rollup` (usage_daily, usage_export, the quota
`read_tenant_quota_state_locked` read, and the `POST /v1/events` rollup
increment) executes inside `scoped_transaction`, which issues
`SET LOCAL app.current_api_key_id` -- so under FORCE each still sees exactly the
rows the application `api_key_id` predicate already returns; no legitimate query
result changes. (The `0002` backfill `INSERT ... SELECT` ran as owner without the
GUC, but it completes before this migration enables FORCE, so it is unaffected.)

Reversibility: pure grant-semantics toggle -- `up` applies FORCE, `down` restores
NO FORCE. No rows or schema are touched, so `up -> down -> up` is a no-op on data
and restores the policy's binding state identically.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11 00:00:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """FORCE the `usage_rollup_tenant_isolation` policy so it binds even for the
    table owner (`meterly_app`), making the defense-in-depth backstop effective.
    Mirrors `0003`'s FORCE on `quotas`."""
    op.execute("ALTER TABLE usage_rollup FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Restore the pre-remediation binding: the owner again bypasses the policy
    (the primary application-level `api_key_id` filter remains the effective
    control either way)."""
    op.execute("ALTER TABLE usage_rollup NO FORCE ROW LEVEL SECURITY")
