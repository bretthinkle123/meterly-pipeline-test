"""Validation contract for `GET /v1/usage`.

`window` must be a timezone-aware ISO-8601 timestamp within `[now-90d, now+1h]`;
a naive datetime (no offset) is rejected rather than silently assumed-UTC, since
guessing wrong would silently misattribute a bucket.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from src.api.schemas.events import CustomerId, Metric

_MAX_WINDOW_LOOKBACK = timedelta(days=90)
_MAX_WINDOW_LOOKAHEAD = timedelta(hours=1)


class UsageQueryParams(BaseModel):
    """The three required query parameters of `GET /v1/usage`, validated as a
    single boundary schema rather than three ad-hoc query-param checks."""

    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    metric: Metric
    window: AwareDatetime

    @field_validator("window")
    @classmethod
    def _window_within_supported_range(cls, value: datetime) -> datetime:
        """Reject a `window` timestamp outside the supported query range.

        Pydantic's `AwareDatetime` already rejects naive datetimes; this
        validator adds the `[now-90d, now+1h]` bound so a caller can't probe
        arbitrarily far into the past (unbounded query cost) or the future
        (nothing to aggregate yet).
        """
        now = datetime.now(timezone.utc)
        earliest = now - _MAX_WINDOW_LOOKBACK
        latest = now + _MAX_WINDOW_LOOKAHEAD
        if not (earliest <= value <= latest):
            raise ValueError(
                f"window must be within [{earliest.isoformat()}, {latest.isoformat()}]"
            )
        return value


class UsageResponse(BaseModel):
    """The minimal response field set for a usage query (ASVS 15.3.1)."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str
    metric: str
    window_start: str
    total_quantity: Decimal
    event_count: int
