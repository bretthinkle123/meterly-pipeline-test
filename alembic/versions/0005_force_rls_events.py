"""force row-level security on events

Security remediation (defense-in-depth backstop repair): make the
`events_tenant_isolation` RLS policy effective for the table OWNER.

Migration `0001` created `events` and ran `ALTER TABLE events ENABLE ROW LEVEL
SECURITY` + `CREATE POLICY events_tenant_isolation`, but NOT `FORCE`. The
migration job resolves the DB URL through the same secrets facade the app uses
(alembic/env.py -> get_database_url -> the meterly_app credential in Secrets
Manager), so `meterly_app` -- which holds CREATE ON SCHEMA public -- is the
table OWNER, and the runtime app also connects as `meterly_app`. A table owner
BYPASSES non-FORCE RLS regardless of NOBYPASSRLS, so the policy was inert for
the app role: a named defense-in-depth control with no efficacy.

This mirrors migration `0004` (usage_rollup) and `0003` (quotas), which
already applied FORCE for exactly this owner-bypass reason. It is
BEHAVIOR-PRESERVING: every runtime reader/writer of `events`
(`src/services/events_service.py`'s ingest write, and the usage/usage_daily/
usage_export read services) executes inside `scoped_transaction`, which
issues `SET LOCAL app.current_api_key_id` -- so under FORCE each still sees
exactly the rows the application `api_key_id` predicate already returns; no
legitimate query result changes.

Reversibility: pure grant-semantics toggle -- `up` applies FORCE, `down`
restores NO FORCE. No rows or schema are touched, so `up -> down -> up` is a
no-op on data and restores the policy's binding state identically.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-12 00:00:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """FORCE the `events_tenant_isolation` policy so it binds even for the
    table owner (`meterly_app`), making the defense-in-depth backstop
    effective. Mirrors `0004`'s FORCE on `usage_rollup`."""
    op.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Restore the pre-remediation binding: the owner again bypasses the
    policy (the primary application-level `api_key_id` filter remains the
    effective control either way)."""
    op.execute("ALTER TABLE events NO FORCE ROW LEVEL SECURITY")
