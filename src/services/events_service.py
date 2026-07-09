"""Service for `POST /v1/events` — the idempotent insert + rollup increment.

Orchestrates one PostgreSQL transaction per request: try the insert, and only
if it actually landed a new row, upsert the rollup counter. This is the one
transaction the plan's idempotency mechanism depends on for atomicity — see
`src/repositories/events_repo.py` for the `ON CONFLICT` primitive itself.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.api.errors import AppError
from src.api.schemas.events import EventCreateRequest, EventResponse
from src.auth.api_key import AuthenticatedPrincipal
from src.db.session import scoped_transaction
from src.logging import get_logger
from src.repositories.events_repo import (
    EventRecord,
    find_event_by_idempotency_key,
    increment_usage_rollup,
    insert_event_if_new,
)
from src.repositories.quotas_repo import read_tenant_quota_state_locked
from src.services.time_windows import floor_to_hour_utc

logger = get_logger(service="meterly")


@dataclass(frozen=True)
class EventCreationOutcome:
    """The result of processing a `POST /v1/events` request: the event
    record and the HTTP status the route should return (201 new, 200 replay)."""

    record: EventRecord
    http_status: int
    idempotent_replay: bool


class IdempotencyRaceError(RuntimeError):
    """Raised if a duplicate-key insert lost the race but the original row
    can't be found — should be unreachable given the UNIQUE constraint, but
    the service fails loudly rather than returning an empty response."""


class QuotaExceededError(AppError):
    """429 with the distinct `quota_exceeded` app_code (never `rate_limited`,
    which already owns 429 for the Tier-2 throttle), carrying `Retry-After`
    seconds to the next hour boundary.

    Raising this mid-transaction — after the event `INSERT`, before the
    rollup increment — is what triggers the rollback that leaves no partial
    write behind: propagating out of `scoped_transaction`'s `session.begin()`
    context undoes the event row and skips `increment_usage_rollup` entirely.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status_code=429,
            app_code="quota_exceeded",
            detail="metric quota exceeded for the current window",
            headers={"Retry-After": str(retry_after_seconds)},
        )


def _seconds_until_next_hour_boundary(received_at: datetime, window_start: datetime) -> int:
    """Seconds from `received_at` to the end of `window_start`'s hour,
    floored to at least 1 — the `Retry-After` value for a quota rejection."""
    next_window_start = window_start + timedelta(hours=1)
    return max(1, math.ceil((next_window_start - received_at).total_seconds()))


async def create_event(
    principal: AuthenticatedPrincipal, payload: EventCreateRequest
) -> EventCreationOutcome:
    """Record a metered event, idempotent on `(api_key_id, idempotency_key)`.

    The server — never the client — sets `api_key_id` (from the authenticated
    principal) and `window_start` (the receive time floored to the UTC hour),
    which is the mass-assignment protection the plan calls out (ASVS 15.3.3).
    """
    received_at = datetime.now(timezone.utc)
    window_start = floor_to_hour_utc(received_at)

    async with scoped_transaction(principal.api_key_id) as session:
        inserted = await insert_event_if_new(
            session,
            api_key_id=principal.api_key_id,
            customer_id=payload.customer_id,
            metric=payload.metric,
            quantity=payload.quantity,
            idempotency_key=payload.idempotency_key,
            window_start=window_start,
        )

        if inserted is not None:
            quota_state = await read_tenant_quota_state_locked(
                session,
                api_key_id=principal.api_key_id,
                customer_id=inserted.customer_id,
                metric=inserted.metric,
                window_start=inserted.window_start,
            )
            if quota_state is not None and quota_state.current_total + inserted.quantity > quota_state.limit_per_window:
                # Deliberately omits current_total/limit from both the log and
                # the (AppError-carried) client envelope — usage totals are
                # billing-adjacent (plan §"Info Disclosure"); the enforcement
                # event itself is what ops needs (ASVS 16.3.3).
                logger.warning(
                    "quota.rejected",
                    userId=principal.api_key_id,
                    action="deny",
                    customer_id=inserted.customer_id,
                    metric=inserted.metric,
                    reason="quota_exceeded",
                )
                raise QuotaExceededError(_seconds_until_next_hour_boundary(received_at, window_start))

            await increment_usage_rollup(
                session,
                api_key_id=principal.api_key_id,
                customer_id=inserted.customer_id,
                metric=inserted.metric,
                window_start=inserted.window_start,
                quantity=inserted.quantity,
            )
            logger.info(
                "event.create",
                userId=principal.api_key_id,
                action="create",
                resource_id=inserted.id,
                idempotent_replay=False,
            )
            return EventCreationOutcome(record=inserted, http_status=201, idempotent_replay=False)

        original = await find_event_by_idempotency_key(
            session, api_key_id=principal.api_key_id, idempotency_key=payload.idempotency_key
        )
        if original is None:
            raise IdempotencyRaceError(
                "insert reported a conflict but the original row could not be found"
            )

        logger.info(
            "event.create",
            userId=principal.api_key_id,
            action="read",
            resource_id=original.id,
            idempotent_replay=True,
        )
        return EventCreationOutcome(record=original, http_status=200, idempotent_replay=True)


def to_response(outcome: EventCreationOutcome) -> EventResponse:
    """Map the service outcome to the minimal public response shape (ASVS 15.3.1)."""
    record = outcome.record
    return EventResponse(
        event_id=record.id,
        customer_id=record.customer_id,
        metric=record.metric,
        quantity=Decimal(record.quantity),
        window_start=record.window_start.isoformat(),
        idempotent_replay=outcome.idempotent_replay,
    )
