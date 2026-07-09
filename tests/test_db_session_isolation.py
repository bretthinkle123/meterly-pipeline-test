"""Regression guard for the quota lock-then-read isolation dependency (U-03).

`src/repositories/quotas_repo.py::read_tenant_quota_state_locked` takes a row
lock on `quotas` (`SELECT ... FOR UPDATE`) and then reads
`usage_rollup.total_quantity` in a SEPARATE plain statement. That second read
only observes the previous lock holder's committed rollup increment under
READ COMMITTED isolation, where each statement gets a fresh snapshot. Under
REPEATABLE READ a lock-waiter would evaluate the rollup read against its own
pre-wait snapshot, read a stale total, and admit events past the quota cap.

`src/db/session.py` therefore pins the engine to READ COMMITTED explicitly
rather than silently relying on the PostgreSQL / role default. This test fails
if that pin is ever removed or changed, giving the fragility a deterministic
test signal that the concurrency test can only catch probabilistically.

The isolation level configured on `create_async_engine(..., isolation_level=...)`
is not exposed as a public engine attribute until a connection is opened, so we
assert on the dialect's `_on_connect_isolation_level` — the value SQLAlchemy
records for the isolation passed at engine construction (no DB connection
required).
"""

import src.db.session as session_module


def test_engine_pins_read_committed_isolation():
    """The process-wide async engine must be pinned to READ COMMITTED."""
    # Force a fresh build from the current source, bypassing any engine another
    # test may have memoized in the process-wide singleton.
    session_module._engine = None
    try:
        engine = session_module.get_engine()
        configured = engine.sync_engine.dialect._on_connect_isolation_level
        assert configured == "READ COMMITTED", (
            "src/db/session.py must pin isolation_level='READ COMMITTED' on the "
            "async engine: quotas_repo.read_tenant_quota_state_locked depends on "
            "each post-lock statement getting a fresh READ COMMITTED snapshot. "
            f"Engine isolation is currently {configured!r}."
        )
    finally:
        # No connection was ever opened, so there is no pool to dispose; just
        # clear the singleton so this unit test leaves nothing behind.
        session_module._engine = None
