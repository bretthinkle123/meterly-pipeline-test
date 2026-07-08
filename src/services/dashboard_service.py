"""Assembles the dashboard's "current usage + last 10 windows + deltas"
series by calling the existing `get_usage` service in-process — the
substantive substitution for the design export's synthetic PRNG mock data
(plan §"the data path"; design-spec §6 "Synthetic data generator").

Reads flow through the exact tenant-scoped, RLS-backed path feature 1
verified (`scoped_transaction(api_key_id)` inside `get_usage`), driven by the
server-held `dashboard-reader` principal rather than a client-presented key.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.api.schemas.dashboard import CurrentUsage, Granularity, UsageSeriesResponse, UsageSeriesRow
from src.api.schemas.usage import UsageQueryParams
from src.auth.api_key import AuthenticatedPrincipal
from src.auth.dashboard_reader import get_dashboard_reader_principal
from src.logging import get_logger
from src.services.time_windows import floor_to_hour_utc
from src.services.usage_service import get_usage

logger = get_logger(service="meterly")

_WINDOW_COUNT = 11  # 11 windows -> 10 period-over-period deltas (design-spec §5)

# Bounds the day-granularity fan-out (up to 11 x 24 = 264 reads) so a single
# dashboard render can never hold more DB connections open at once than this,
# independent of the pool's own size (D-D1 — resource exhaustion).
_MAX_CONCURRENT_READS = 10

_WINDOW_NOUNS: dict[Granularity, str] = {"hour": "this hour", "day": "today"}
_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@dataclass(frozen=True)
class _Bucket:
    """One assembled window's aggregate totals — either a single hourly
    rollup (`hour` granularity) or the sum of a day's elapsed hourly rollups
    (`day` granularity)."""

    total_quantity: Decimal
    event_count: int


def _window_starts(now: datetime, granularity: Granularity) -> list[datetime]:
    """Return the `_WINDOW_COUNT` window-start boundaries, newest first,
    anchored to the server's own `now()`.

    The client never supplies a reference time — this is what stops
    unbounded-past probing (plan §"Validation contracts", `— no window/anchor
    param —` row).
    """
    current_hour_start = floor_to_hour_utc(now)
    if granularity == "hour":
        return [current_hour_start - timedelta(hours=offset) for offset in range(_WINDOW_COUNT)]

    current_day_start = current_hour_start.replace(hour=0)
    return [current_day_start - timedelta(days=offset) for offset in range(_WINDOW_COUNT)]


async def _read_hourly_bucket(
    principal: AuthenticatedPrincipal,
    customer_id: str,
    metric: str,
    hour_start: datetime,
    semaphore: asyncio.Semaphore,
) -> _Bucket:
    """Read exactly one hourly rollup via the existing `get_usage` service,
    bounded by `semaphore` so a fan-out can't open unbounded DB connections."""
    async with semaphore:
        query = UsageQueryParams(customer_id=customer_id, metric=metric, window=hour_start)
        response = await get_usage(principal, query)
        return _Bucket(total_quantity=response.total_quantity, event_count=response.event_count)


async def _read_hour_granularity_buckets(
    principal: AuthenticatedPrincipal, customer_id: str, metric: str, window_starts: list[datetime]
) -> list[_Bucket]:
    """One `get_usage` read per hour window (11 reads total) — the fully
    faithful, cheap path (plan §"Window granularity", default granularity)."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_READS)
    buckets = await asyncio.gather(
        *(
            _read_hourly_bucket(principal, customer_id, metric, window_start, semaphore)
            for window_start in window_starts
        )
    )
    return list(buckets)


async def _read_day_granularity_buckets(
    principal: AuthenticatedPrincipal,
    customer_id: str,
    metric: str,
    day_starts: list[datetime],
    now: datetime,
) -> list[_Bucket]:
    """Sum each day's *elapsed* hourly rollups (up to 24 reads per day, fewer
    for the still-in-progress current day) — the bounded fan-out path (plan
    §"Window granularity", up to 11 x 24 = 264 reads).

    Never queries an hour beyond the server's latest completed hour, so every
    read stays inside `get_usage`'s `[now-90d, now+1h]` bound even for
    today's partial window — a day series only reaches 11 days back, well
    within the 90-day lookback.
    """
    latest_completed_hour = floor_to_hour_utc(now)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_READS)

    hours_by_day = [
        [
            hour_start
            for hour_offset in range(24)
            if (hour_start := day_start + timedelta(hours=hour_offset)) <= latest_completed_hour
        ]
        for day_start in day_starts
    ]

    all_hour_starts = [hour_start for day_hours in hours_by_day for hour_start in day_hours]
    all_hourly_buckets = await asyncio.gather(
        *(
            _read_hourly_bucket(principal, customer_id, metric, hour_start, semaphore)
            for hour_start in all_hour_starts
        )
    )

    day_buckets: list[_Bucket] = []
    cursor = 0
    for day_hours in hours_by_day:
        day_slice = all_hourly_buckets[cursor : cursor + len(day_hours)]
        cursor += len(day_hours)
        day_buckets.append(
            _Bucket(
                total_quantity=sum((bucket.total_quantity for bucket in day_slice), Decimal("0")),
                event_count=sum(bucket.event_count for bucket in day_slice),
            )
        )
    return day_buckets


def _format_quantity(value: Decimal) -> str:
    """Format a usage total for display: thousands separators, fixed-point
    (never scientific notation), trimmed of insignificant trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    sign = "-" if text.startswith("-") else ""
    integer_part, _, decimal_part = text.lstrip("-").partition(".")
    grouped_integer = f"{int(integer_part):,}"
    return f"{sign}{grouped_integer}.{decimal_part}" if decimal_part else f"{sign}{grouped_integer}"


def _format_delta(delta: Decimal) -> tuple[str, str]:
    """Return `(delta_text, delta_direction)` for a period-over-period delta.

    A zero delta gets the neutral "—" glyph rather than a forced up/down
    arrow — the small, in-language `CMP-6` extension the export's
    populated/empty-only mock doesn't cover (design-spec §2, plan Q5).
    """
    if delta > 0:
        return f"+{_format_quantity(delta)}", "up"
    if delta < 0:
        return f"-{_format_quantity(-delta)}", "down"
    return "—", "neutral"


def _format_window_label(window_start: datetime, granularity: Granularity) -> str:
    """Render a window's start as the `CMP-7` "Window start" column text."""
    month = _MONTH_ABBREVIATIONS[window_start.month - 1]
    if granularity == "hour":
        return f"{month} {window_start.day}, {window_start.hour:02d}:00"
    return f"{month} {window_start.day}, {window_start.year}"


async def get_usage_series(
    *, customer_id: str, metric: str, granularity: Granularity
) -> UsageSeriesResponse:
    """Assemble the current-usage + last-10-windows + deltas series
    (`CMP-5`..`CMP-8`) for one customer/metric/granularity selection.

    Populated vs. empty is decided by "all 11 windows zero" (plan §"the data
    path", step 6) — the real trigger, replacing the export mock's hardcoded
    `customer === 'initech'` check (design-spec §6).
    """
    principal = await get_dashboard_reader_principal()
    now = datetime.now(timezone.utc)
    window_starts = _window_starts(now, granularity)

    if granularity == "hour":
        buckets = await _read_hour_granularity_buckets(principal, customer_id, metric, window_starts)
    else:
        buckets = await _read_day_granularity_buckets(principal, customer_id, metric, window_starts, now)

    quantities = [bucket.total_quantity for bucket in buckets]
    state = "empty" if all(quantity == 0 for quantity in quantities) else "populated"

    rows = []
    for index in range(_WINDOW_COUNT - 1):
        delta_text, delta_direction = _format_delta(quantities[index] - quantities[index + 1])
        rows.append(
            UsageSeriesRow(
                window_start=_format_window_label(window_starts[index], granularity),
                metric=metric,
                quantity=_format_quantity(quantities[index]),
                delta_text=delta_text,
                delta_direction=delta_direction,
            )
        )

    current_delta_text, current_delta_direction = _format_delta(quantities[0] - quantities[1])
    current = CurrentUsage(
        window_start=_format_window_label(window_starts[0], granularity),
        quantity=_format_quantity(quantities[0]),
        metric=metric,
        window_label=_WINDOW_NOUNS[granularity],
        delta_text=current_delta_text,
        delta_direction=current_delta_direction,
    )

    # `userId` is the reader's own opaque surrogate id, never the (redacted)
    # customer_id — belt-and-suspenders on top of the structlog redaction
    # processor (plan §"Logging / observability").
    logger.info(
        "dashboard.usage_series",
        userId=principal.api_key_id,
        action="read",
        operation="dashboard.usage_series",
        granularity=granularity,
        windows=len(window_starts),
        state=state,
    )

    return UsageSeriesResponse(state=state, current=current, rows=rows)
