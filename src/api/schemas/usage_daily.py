"""Validation contract for `GET /v1/usage/daily`.

The brief pins **HTTP 400** for a missing/malformed/out-of-range `date`,
whereas the codebase's Pydantic query models otherwise surface validation
failures as 422 (`UsageQueryParams`/`UsageExportQueryParams`). To honor both
conventions at once, `date` is bound as a loosely-typed optional string field
(`DailyUsageQueryParams`, `extra="forbid"` — so an *undeclared* param still
rides the house 422 path) and its *value* is validated imperatively by
`parse_daily_date`, which raises `HTTPException(400)` directly. See the
plan's "The 400-not-422 decision" section for the full rationale.
"""

import re
from dataclasses import dataclass
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

# Anchored, fixed-width, no backtracking (ReDoS-safe) — structural shape only;
# calendar validity (e.g. 2026-13-40, 2026-02-30) is checked separately via
# `date.fromisoformat` below.
_DATE_FORMAT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Mirrors the `[now-90d, now+1h]` lookback/lookahead idiom `usage.py` /
# `usage_export.py` use for their window bounds, adapted to a whole calendar
# day: a caller can't probe arbitrarily far into the past (unbounded query
# cost) or request a day that hasn't happened yet.
_MAX_LOOKBACK_DAYS = 90
_MAX_LOOKAHEAD_DAYS = 1


class DailyUsageQueryParams(BaseModel):
    """The single query parameter of `GET /v1/usage/daily`.

    `date` is typed as a loose `str | None` rather than `datetime.date` so a
    malformed or missing value falls through to `parse_daily_date` (400)
    instead of FastAPI's automatic Pydantic-failure mapping (422).
    `extra="forbid"` still rejects any *undeclared* query parameter (e.g. a
    smuggled `customer_id`/`api_key_id`) with the house 422 contract.
    """

    model_config = ConfigDict(extra="forbid")

    date: str | None = None


@dataclass(frozen=True)
class DailyDateWindow:
    """The validated `date` string plus its half-open UTC day window."""

    date_str: str
    day_start: datetime
    day_end: datetime


def day_window_for(parsed_date: calendar_date) -> tuple[datetime, datetime]:
    """Return the half-open `[day_start, day_end)` UTC window for a calendar
    date — pure date arithmetic, no "now"/range dependency, so it stays
    directly unit-testable across month rollovers and leap years independent
    of whichever range bound `parse_daily_date` currently enforces."""
    day_start = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    return day_start, day_start + timedelta(days=1)


def parse_daily_date(raw: str | None) -> DailyDateWindow:
    """Validate the `date` query value and return its UTC day window.

    Raises `HTTPException(400)` for a missing value, a value that doesn't
    match the anchored `YYYY-MM-DD` shape, a value that isn't a real calendar
    date (e.g. `2026-02-30`), or a well-formed date outside
    `[today_utc-90d, today_utc+1d]`. Never touches SQL — the caller receives
    only the derived `day_start`/`day_end` datetimes, which are the values
    that eventually travel as bound parameters into the aggregate query.
    """
    if raw is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date is required (YYYY-MM-DD)")

    if not _DATE_FORMAT_PATTERN.match(raw):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="date must be in YYYY-MM-DD format"
        )

    try:
        parsed_date = calendar_date.fromisoformat(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="date is not a valid calendar date"
        ) from error

    today_utc = datetime.now(timezone.utc).date()
    earliest = today_utc - timedelta(days=_MAX_LOOKBACK_DAYS)
    latest = today_utc + timedelta(days=_MAX_LOOKAHEAD_DAYS)
    if not (earliest <= parsed_date <= latest):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"date must be within [{earliest.isoformat()}, {latest.isoformat()}]",
        )

    day_start, day_end = day_window_for(parsed_date)
    return DailyDateWindow(date_str=raw, day_start=day_start, day_end=day_end)


class DailyMetricCount(BaseModel):
    """One metric's summed `event_count` for the requested UTC day."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    event_count: int


class DailyUsageResponse(BaseModel):
    """The minimal response field set for `GET /v1/usage/daily` (ASVS
    15.3.1) — the echoed `date` plus the per-metric counts, ordered by
    `metric`. `api_key_id`/`customer_id` are never present here."""

    model_config = ConfigDict(extra="forbid")

    date: str
    metrics: list[DailyMetricCount]
