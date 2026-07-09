"""Service for `GET /v1/usage/export` — the pre-flight row-cap check and the
constant-memory CSV streaming generator.

Two-phase by design: `prepare_export` runs to completion (and can still
cleanly 422, or fail closed to a generic 500 on an unexpected error — AC22)
*before* the route ever constructs a `StreamingResponse`; `stream_export_csv`
is the response body, pulled by Starlette only after the handler returns, so
it opens its own transaction rather than reusing one the handler already closed.
"""

import csv
import io
from collections.abc import AsyncIterator

from fastapi import HTTPException, status

from src.api.csv_export import (
    EXPORT_HEADER,
    escape_csv_text_cell,
    format_total_quantity,
    format_window_start,
)
from src.api.schemas.usage_export import UsageExportQueryParams
from src.auth.api_key import AuthenticatedPrincipal
from src.db.session import scoped_transaction
from src.logging import get_logger
from src.repositories.usage_repo import count_usage_rollups, stream_usage_rollups

logger = get_logger(service="meterly")

MAX_EXPORT_ROWS = 100_000
EXPORT_COLUMNS = ("customer_id", "metric", "window_start", "total_quantity")

# How many encoded data rows to accumulate in the buffer before handing one
# chunk to the ASGI stack. Emitting a chunk *per row* meant the full stack —
# four nested Starlette `BaseHTTPMiddleware` layers, each of which re-pumps
# every streamed chunk through its own anyio memory-object-stream — ran once
# per row (100,000× at the cap), which dominated the export's wall-clock time
# (p95 ~10-25s), not the DB read or the CSV encoding. Batching ~1,000 rows per
# chunk cuts that per-chunk fan-out ~1,000× while staying constant-memory: the
# buffer holds at most one batch of encoded rows (~tens of KB) before it drains.
_ROWS_PER_CHUNK = 1000


async def prepare_export(principal: AuthenticatedPrincipal, params: UsageExportQueryParams) -> None:
    """Pre-flight cap check: raise a 422 before any response byte if the
    caller's filtered result exceeds `MAX_EXPORT_ROWS`.

    This must run and fully resolve *before* the route returns a
    `StreamingResponse` — once that response object exists the status line
    is already 200 and bytes may already be on the wire, so a 422 can no
    longer be sent. Any *other* (non-cap) exception from the `COUNT` query
    (a DB connection drop, a query error) is deliberately left to propagate
    uncaught to the error-envelope boundary (`src/api/errors.py`'s catch-all
    handler): fail-closed generic `500 internal`, the pre-flight transaction
    rolled back, and no `StreamingResponse` ever constructed (AC22).
    """
    async with scoped_transaction(principal.api_key_id) as session:
        row_count = await count_usage_rollups(
            session,
            api_key_id=principal.api_key_id,
            customer_id=params.customer_id,
            metric=params.metric,
            window_from=params.from_,
            window_to=params.to,
        )

    if row_count > MAX_EXPORT_ROWS:
        logger.warning(
            "usage.export.rejected",
            userId=principal.api_key_id,
            action="deny",
            reason="row_cap_exceeded",
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"result exceeds {MAX_EXPORT_ROWS} rows; narrow with customer_id, metric, from, and/or to",
        )


def _drain(buffer: io.StringIO) -> bytes:
    """Read `buffer`'s current contents as UTF-8 bytes, then reset it in
    place — this is what keeps the generator at constant memory: it never
    holds more than one row's encoded text at a time."""
    data = buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)
    return data


async def stream_export_csv(
    principal: AuthenticatedPrincipal, params: UsageExportQueryParams
) -> AsyncIterator[bytes]:
    """The `StreamingResponse` body: the header row first, always, then one
    CSV-encoded row per streamed `usage_rollup` record.

    Yielding the header before touching the database means an empty result
    is still a 200 header-only CSV, never a 404 (AC12, mirrors the existing
    zeros-not-404 contract on `GET /v1/usage`). Opens its own tenant-scoped
    transaction so the `SET LOCAL app.current_api_key_id` RLS-backstop
    setting and the server-side cursor both stay valid for the whole stream.
    """
    row_count = 0
    capped = False
    completed = False

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)

    try:
        # Header first, always, as its own chunk — so an empty result is a
        # 200 header-only CSV (AC12) and the first bytes are observable before
        # the data rows stream (AC9), independent of the data-row batching below.
        writer.writerow(EXPORT_HEADER)
        yield _drain(buffer)

        async with scoped_transaction(principal.api_key_id) as session:
            async for record in stream_usage_rollups(
                session,
                api_key_id=principal.api_key_id,
                customer_id=params.customer_id,
                metric=params.metric,
                window_from=params.from_,
                window_to=params.to,
                limit=MAX_EXPORT_ROWS,
            ):
                writer.writerow(
                    [
                        escape_csv_text_cell(record.customer_id),
                        escape_csv_text_cell(record.metric),
                        format_window_start(record.window_start),
                        format_total_quantity(record.total_quantity),
                    ]
                )
                row_count += 1
                # Hand a chunk to the ASGI stack once per _ROWS_PER_CHUNK rows,
                # not once per row (see _ROWS_PER_CHUNK). Still streaming: at the
                # cap this yields ~100 chunks, never one buffered blob.
                if row_count % _ROWS_PER_CHUNK == 0:
                    yield _drain(buffer)

        # Flush the trailing partial batch. For an empty result nothing is
        # buffered here (the header already went out above), so no stray chunk.
        if buffer.tell():
            yield _drain(buffer)

        capped = row_count >= MAX_EXPORT_ROWS
        completed = True
    finally:
        # In `finally` so a client disconnect or a mid-stream DB error still
        # records what was actually sent (`completed=False` in that case) —
        # exactly one usage.export event per request either way (AC17).
        logger.info(
            "usage.export",
            userId=principal.api_key_id,
            action="read",
            resource="usage_rollup",
            rowCount=row_count,
            capped=capped,
            completed=completed,
            filtered_by_customer=params.customer_id is not None,
            filtered_by_metric=params.metric is not None,
            bounded_from=params.from_ is not None,
            bounded_to=params.to is not None,
        )
