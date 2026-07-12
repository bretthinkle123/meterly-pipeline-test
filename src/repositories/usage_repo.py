"""Repository for reading `usage_rollup` — the O(1) aggregate lookup behind
`GET /v1/usage`, the count/stream queries behind `GET /v1/usage/export`, and
the per-metric daily aggregate behind `GET /v1/usage/daily`.

Reads the pre-aggregated counter rather than summing `events` live, which is
what keeps the GET p95 budget bounded as ingest volume grows (see the plan's
"why reading the rollup" writeup).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class UsageRollupRecord:
    """The aggregate counter for one `(api_key_id, customer_id, metric, window_start)` bucket."""

    total_quantity: Decimal
    event_count: int


async def find_usage_rollup(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str,
    metric: str,
    window_start: datetime,
) -> UsageRollupRecord | None:
    """Fetch the usage counter for a bucket, scoped by the authenticated
    `api_key_id` first — an authenticated key can never read another
    tenant's totals (IDOR/BOLA mitigation, AC17). None means no events have
    landed in this bucket yet (the caller returns zeros, not 404)."""
    result = await session.execute(
        text(
            """
            SELECT total_quantity, event_count
            FROM usage_rollup
            WHERE api_key_id = :api_key_id
              AND customer_id = :customer_id
              AND metric = :metric
              AND window_start = :window_start
            """
        ),
        {
            "api_key_id": api_key_id,
            "customer_id": customer_id,
            "metric": metric,
            "window_start": window_start,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return UsageRollupRecord(total_quantity=row["total_quantity"], event_count=row["event_count"])


@dataclass(frozen=True)
class UsageRollupExportRecord:
    """The four columns `GET /v1/usage/export` emits — deliberately excludes
    `event_count`, `updated_at`, and `api_key_id` (ASVS 15.3.1 minimal-
    projection rule; the caller's identity is never echoed back)."""

    customer_id: str
    metric: str
    window_start: datetime
    total_quantity: Decimal


def _export_filter_clause_and_params(
    *,
    api_key_id: int,
    customer_id: str | None,
    metric: str | None,
    window_from: datetime | None,
    window_to: datetime | None,
) -> tuple[str, dict]:
    """Build the shared WHERE clause + bound-parameter dict for the export
    count and stream queries, so the two statements can never drift apart.

    Every fragment is a fixed literal chosen only by which filters are
    *present*; the filter *values* always travel as bound parameters, never
    concatenated into the SQL string.
    """
    clauses = ["api_key_id = :api_key_id"]
    params: dict = {"api_key_id": api_key_id}
    if customer_id is not None:
        clauses.append("customer_id = :customer_id")
        params["customer_id"] = customer_id
    if metric is not None:
        clauses.append("metric = :metric")
        params["metric"] = metric
    if window_from is not None:
        clauses.append("window_start >= :window_from")
        params["window_from"] = window_from
    if window_to is not None:
        clauses.append("window_start <= :window_to")
        params["window_to"] = window_to
    return " AND ".join(clauses), params


async def count_usage_rollups(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str | None = None,
    metric: str | None = None,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> int:
    """Count the caller's rollup rows matching the optional filters.

    Backs the export's pre-flight row-cap check (AC8) — an index-prefix scan
    on `api_key_id`, scoped first (row-level-security invariant), cheap even
    without a covering index on the export's ORDER BY (see the plan's
    "Ordering vs. index" tradeoff writeup).
    """
    where_clause, params = _export_filter_clause_and_params(
        api_key_id=api_key_id,
        customer_id=customer_id,
        metric=metric,
        window_from=window_from,
        window_to=window_to,
    )
    # Safe: `where_clause` is composed only of fixed literal fragments chosen by
    # _export_filter_clause_and_params (which filters are *present*); every filter
    # *value* travels in `params` as a bound parameter, never interpolated into the
    # SQL string. No caller-controlled input reaches this text(). Not a SQLi sink.
    count_sql = text(f"SELECT count(*) FROM usage_rollup WHERE {where_clause}")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    result = await session.execute(count_sql, params)
    return result.scalar_one()


# Rows fetched per server-side-cursor round-trip in `stream_usage_rollups`.
# Bounds the client-side buffer (at most one batch is held, regardless of the
# total result size — the constant-memory streaming invariant is preserved) and
# trades a handful of extra rows of memory for far fewer cursor round-trips when
# draining a large export.
_STREAM_FETCH_BATCH = 5000


async def stream_usage_rollups(
    session: AsyncSession,
    *,
    api_key_id: int,
    customer_id: str | None = None,
    metric: str | None = None,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    limit: int,
) -> AsyncIterator[UsageRollupExportRecord]:
    """Stream the caller's rollup rows matching the optional filters via a
    server-side cursor, in deterministic order (AC9, AC13).

    The `ORDER BY` clause is a fixed literal built from server-side column
    names, never derived from client input, so the sort key is not a
    tampering sink. `session.stream(...)` fetches from PostgreSQL via a
    server-side cursor rather than buffering the whole result client-side —
    the property the constant-memory streaming design depends on.

    `yield_per` tunes *how many* rows that server-side cursor fetches per
    round-trip (it does not abandon the cursor): the default single-row/small
    partition fetch cost ~1s to drain 100,000 rows, versus ~0.6s at
    `_STREAM_FETCH_BATCH`, while keeping memory bounded — at most one batch of
    rows is buffered client-side, independent of the total result size.
    """
    where_clause, params = _export_filter_clause_and_params(
        api_key_id=api_key_id,
        customer_id=customer_id,
        metric=metric,
        window_from=window_from,
        window_to=window_to,
    )
    params["limit"] = limit
    # Safe: same invariant as count_usage_rollups — `where_clause` is fixed
    # literal fragments only, the ORDER BY is a server-side constant, and every
    # filter value (plus :limit) is a bound parameter in `params`. No caller
    # input is interpolated into the SQL string. Not a SQLi sink.
    stream_sql = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        f"""
        SELECT customer_id, metric, window_start, total_quantity
        FROM usage_rollup
        WHERE {where_clause}
        ORDER BY window_start, customer_id, metric ASC
        LIMIT :limit
        """
    ).execution_options(yield_per=_STREAM_FETCH_BATCH)
    result = await session.stream(stream_sql, params)
    async for row in result.mappings():
        yield UsageRollupExportRecord(
            customer_id=row["customer_id"],
            metric=row["metric"],
            window_start=row["window_start"],
            total_quantity=row["total_quantity"],
        )


@dataclass(frozen=True)
class DailyMetricCount:
    """One metric's summed `event_count` for a UTC day, aggregated across
    every hour-bucket and `customer_id` under the tenant (the daily
    endpoint's "per metric" grouping deliberately collapses `customer_id`)."""

    metric: str
    event_count: int


async def aggregate_daily_event_counts(
    session: AsyncSession,
    *,
    api_key_id: int,
    day_start: datetime,
    day_end: datetime,
) -> list[DailyMetricCount]:
    """Sum `usage_rollup.event_count` per `metric` over `[day_start, day_end)`,
    scoped by the authenticated `api_key_id` first (IDOR/BOLA mitigation,
    same invariant as `find_usage_rollup`/`count_usage_rollups`).

    Backs `GET /v1/usage/daily`: one grouped aggregate over the day's
    hour-buckets rather than a `COUNT(*)` scan of raw `events`, keeping the
    per-request cost bounded as ingest volume grows (see the plan's "Reading
    the data" writeup). `day_start`/`day_end` are always bound parameters —
    the caller-supplied `date` string never reaches this function, only the
    datetimes `parse_daily_date` already validated.
    """
    result = await session.execute(
        text(
            """
            SELECT metric, SUM(event_count) AS event_count
            FROM usage_rollup
            WHERE api_key_id = :api_key_id
              AND window_start >= :day_start
              AND window_start <  :day_end
            GROUP BY metric
            ORDER BY metric ASC
            """
        ),
        {"api_key_id": api_key_id, "day_start": day_start, "day_end": day_end},
    )
    return [
        DailyMetricCount(metric=row["metric"], event_count=row["event_count"])
        for row in result.mappings()
    ]
