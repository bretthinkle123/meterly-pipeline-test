"""Repository for `quotas` — the per-tenant, per-customer, per-metric usage
cap `upsert_quota` writes, and the atomic read-and-decide
`read_tenant_quota_state_locked` `POST /v1/events` consults.

Every query here is scoped by the authenticated `api_key_id` first (the
row-level-security invariant from `code-standards`); the PostgreSQL RLS
policy `quotas_tenant_isolation` is the backstop if this scoping were ever
missing from a query.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class QuotaRecord:
    """The stored/echoed shape of a `quotas` row plus whether this call
    created it (`inserted`) or replaced an existing cap."""

    customer_id: str
    metric: str
    limit_per_window: int
    inserted: bool


@dataclass(frozen=True)
class QuotaState:
    """The result of the atomic read-and-decide: the configured cap and the
    current-window rollup total, read together under `FOR UPDATE OF q`."""

    limit_per_window: int
    current_total: Decimal


async def upsert_quota(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    limit_per_window: int,
) -> QuotaRecord:
    """Create-or-replace the cap for `(api_key_id, customer_id, metric)` in
    one statement, reporting whether this call inserted a new row.

    `RETURNING (xmax = 0) AS inserted` is the idiom that distinguishes a
    fresh `INSERT` from an `ON CONFLICT DO UPDATE` in a single round-trip: a
    freshly inserted row's `xmax` (the deleting-transaction marker) is still
    zero, while an updated row's `xmax` is set by the `UPDATE` — so the
    service can map `inserted -> 201` else `200` without a second read-before-
    write query or the TOCTOU race that would introduce.
    """
    result = await session.execute(
        text(
            """
            INSERT INTO quotas (api_key_id, customer_id, metric, limit_per_window, updated_at)
            VALUES (:api_key_id, :customer_id, :metric, :limit_per_window, now())
            ON CONFLICT (api_key_id, customer_id, metric) DO UPDATE SET
                limit_per_window = EXCLUDED.limit_per_window,
                updated_at = now()
            RETURNING customer_id, metric, limit_per_window, (xmax = 0) AS inserted
            """
        ),
        {
            "api_key_id": api_key_id,
            "customer_id": customer_id,
            "metric": metric,
            "limit_per_window": limit_per_window,
        },
    )
    row = result.mappings().one()
    return QuotaRecord(
        customer_id=row["customer_id"],
        metric=row["metric"],
        limit_per_window=row["limit_per_window"],
        inserted=row["inserted"],
    )


async def read_tenant_quota_state_locked(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    window_start: datetime,
) -> QuotaState | None:
    """Lock the quota row, then read the current-window rollup total fresh —
    the atomic check-then-decide mechanism `POST /v1/events` uses to enforce
    `R + Q <= L` without a TOCTOU race (plan §"The atomic read-and-decide").

    Returns None when no quota row matches `(api_key_id, customer_id,
    metric)` — the caller treats this as unlimited and takes no lock at all
    (the common, zero-contention path).

    **Two round-trips, deliberately, not one combined `LEFT JOIN ... FOR
    UPDATE` statement.** `FOR UPDATE` only guarantees a fresh re-read of the
    *locked* row itself (via PostgreSQL's `EvalPlanQual` recheck) when a
    waiter unblocks after the lock holder commits — it does **not** force a
    fresh snapshot for other tables read in the same statement, including a
    `LEFT JOIN`. A waiter that queued for the lock before the holder
    committed would evaluate that join against its own original (pre-wait)
    snapshot and read a stale `total_quantity`, silently breaking strict
    enforcement under real concurrency (verified empirically: a single
    combined statement let every concurrent waiter read the same stale
    total and all get admitted). Issuing the `usage_rollup` read as its own
    statement *after* the lock is acquired gives it a fresh per-statement
    READ COMMITTED snapshot, which does include everything the previous lock
    holder just committed — this is what actually makes `current_total`
    accurate for every waiter, not just the first, uncontended caller.
    """
    lock_result = await session.execute(
        text(
            """
            SELECT limit_per_window
            FROM quotas
            WHERE api_key_id = :api_key_id AND customer_id = :customer_id AND metric = :metric
            FOR UPDATE
            """
        ),
        {"api_key_id": api_key_id, "customer_id": customer_id, "metric": metric},
    )
    lock_row = lock_result.mappings().first()
    if lock_row is None:
        return None

    rollup_result = await session.execute(
        text(
            """
            SELECT total_quantity
            FROM usage_rollup
            WHERE api_key_id = :api_key_id AND customer_id = :customer_id
              AND metric = :metric AND window_start = :window_start
            """
        ),
        {
            "api_key_id": api_key_id,
            "customer_id": customer_id,
            "metric": metric,
            "window_start": window_start,
        },
    )
    rollup_row = rollup_result.mappings().first()
    current_total = rollup_row["total_quantity"] if rollup_row is not None else Decimal(0)
    return QuotaState(limit_per_window=lock_row["limit_per_window"], current_total=Decimal(current_total))
