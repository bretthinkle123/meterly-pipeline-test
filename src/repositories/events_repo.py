"""Repository for `events` + the `usage_rollup` increment — the idempotent
insert-and-aggregate primitive at the core of `POST /v1/events`.

Every query here is scoped by the authenticated `api_key_id` (the row-level-
security invariant from `code-standards`); the PostgreSQL RLS policy on both
tables is the backstop if this scoping were ever missing.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class EventRecord:
    """A row of `events`, the fields the service layer returns to the caller."""

    id: int
    api_key_id: int
    customer_id: str
    metric: str
    quantity: Decimal
    idempotency_key: str
    window_start: datetime


async def insert_event_if_new(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    quantity: Decimal,
    idempotency_key: str,
    window_start: datetime,
) -> EventRecord | None:
    """Insert a new event row unless `(api_key_id, idempotency_key)` already
    exists; returns None when the row already existed (a duplicate replay).

    The `UNIQUE (api_key_id, idempotency_key)` constraint + `ON CONFLICT DO
    NOTHING` is the sole source of truth for "have I seen this key before?" —
    evaluated atomically by PostgreSQL, so 50 concurrent identical requests
    produce exactly one winner (AC-CONCURRENCY) with no application-level lock.
    """
    result = await session.execute(
        text(
            """
            INSERT INTO events (
                api_key_id, customer_id, metric, quantity, idempotency_key, window_start
            )
            VALUES (
                :api_key_id, :customer_id, :metric, :quantity, :idempotency_key, :window_start
            )
            ON CONFLICT (api_key_id, idempotency_key) DO NOTHING
            RETURNING id, api_key_id, customer_id, metric, quantity, idempotency_key, window_start
            """
        ),
        {
            "api_key_id": api_key_id,
            "customer_id": customer_id,
            "metric": metric,
            "quantity": quantity,
            "idempotency_key": idempotency_key,
            "window_start": window_start,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return EventRecord(**row)


async def find_event_by_idempotency_key(
    session: AsyncSession, *, api_key_id: int, idempotency_key: str
) -> EventRecord | None:
    """Fetch the original event for a duplicate `idempotency_key` replay.

    Scoped by `api_key_id` first — an idempotency key is only ever unique
    within its owning tenant (plan's "idempotency_key scope" note), so this
    also doubles as the tenant-isolation guard for the replay read.
    """
    result = await session.execute(
        text(
            """
            SELECT id, api_key_id, customer_id, metric, quantity, idempotency_key, window_start
            FROM events
            WHERE api_key_id = :api_key_id AND idempotency_key = :idempotency_key
            """
        ),
        {"api_key_id": api_key_id, "idempotency_key": idempotency_key},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return EventRecord(**row)


async def increment_usage_rollup(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    window_start: datetime,
    quantity: Decimal,
) -> None:
    """Upsert the hourly rollup counter for `(api_key_id, customer_id, metric,
    window_start)`, adding `quantity` and incrementing `event_count` by one.

    Only called after `insert_event_if_new` returns a row (this request won
    the insert) — a duplicate replay must never reach here, which is exactly
    why duplicates never double-count (AC2).
    """
    await session.execute(
        text(
            """
            INSERT INTO usage_rollup (
                api_key_id, customer_id, metric, window_start, total_quantity, event_count, updated_at
            )
            VALUES (
                :api_key_id, :customer_id, :metric, :window_start, :quantity, 1, now()
            )
            ON CONFLICT (api_key_id, customer_id, metric, window_start) DO UPDATE SET
                total_quantity = usage_rollup.total_quantity + EXCLUDED.total_quantity,
                event_count = usage_rollup.event_count + 1,
                updated_at = now()
            """
        ),
        {
            "api_key_id": api_key_id,
            "customer_id": customer_id,
            "metric": metric,
            "window_start": window_start,
            "quantity": quantity,
        },
    )
