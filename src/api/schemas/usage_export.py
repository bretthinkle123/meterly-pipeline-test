"""Validation contract for `GET /v1/usage/export`.

Reuses the exact `customer_id`/`metric` anchored allowlists
`src/api/schemas/events.py` already enforces for the SQL sink these filters
share, and the same `[now-90d, now+1h]` window-bound idiom `usage.py` uses —
applied here as an optional `[from, to]` range rather than a single
point-in-time window.
"""

from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.api.schemas.events import CustomerId, Metric

_MAX_WINDOW_LOOKBACK = timedelta(days=90)
_MAX_WINDOW_LOOKAHEAD = timedelta(hours=1)


class UsageExportQueryParams(BaseModel):
    """Optional filters for `GET /v1/usage/export`.

    Every field is optional; omitting all of them exports the caller's full
    row set (subject to the 100,000-row cap). `extra='forbid'` rejects an
    unknown query parameter — the same mass-assignment posture the write
    schemas use, applied to a read (a client-supplied `api_key_id` is
    rejected rather than silently ignored).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    customer_id: CustomerId | None = None
    metric: Metric | None = None
    from_: AwareDatetime | None = Field(default=None, alias="from")
    to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_window_bounds(self) -> "UsageExportQueryParams":
        """Enforce `from <= to` and both within `[now-90d, now+1h]` —
        mirrors `UsageQueryParams`'s single-window bound, applied to a range
        so a caller can't probe arbitrarily far into the past (unbounded
        query cost) or the future."""
        now = datetime.now(timezone.utc)
        earliest = now - _MAX_WINDOW_LOOKBACK
        latest = now + _MAX_WINDOW_LOOKAHEAD

        for field_name, value in (("from", self.from_), ("to", self.to)):
            if value is not None and not (earliest <= value <= latest):
                raise ValueError(
                    f"{field_name} must be within [{earliest.isoformat()}, {latest.isoformat()}]"
                )

        if self.from_ is not None and self.to is not None and self.from_ > self.to:
            raise ValueError("from must be <= to")

        return self
