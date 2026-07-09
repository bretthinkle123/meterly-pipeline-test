"""Unit tests for `src/services/usage_export_service.py` — the pre-flight
row-cap check and the streaming generator, with the DB layer mocked out
(AC8, AC9, AC12, AC17, AC22).
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.api.schemas.usage_export import UsageExportQueryParams
from src.auth.api_key import AuthenticatedPrincipal
from src.repositories.usage_repo import UsageRollupExportRecord
from src.services import usage_export_service


def _principal(api_key_id: int = 42) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(api_key_id=api_key_id, rate_limit_per_sec=100)


def _no_filter_params() -> UsageExportQueryParams:
    return UsageExportQueryParams()


def _fake_scoped_transaction_factory():
    """A fake `scoped_transaction` async context manager yielding a sentinel
    'session' — the mocked repo functions never actually use it."""

    @asynccontextmanager
    async def _fake(api_key_id: int):
        yield "fake-session"

    return _fake


async def test_prepare_export_passes_when_under_cap(monkeypatch):
    """AC8: a count under the cap raises nothing."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    async def _fake_count(session, **kwargs):
        return 5

    monkeypatch.setattr(usage_export_service, "count_usage_rollups", _fake_count)

    await usage_export_service.prepare_export(_principal(), _no_filter_params())


async def test_prepare_export_passes_when_exactly_at_cap_boundary(monkeypatch):
    """AC8 boundary: a count exactly equal to MAX_EXPORT_ROWS (100,000) is
    NOT rejected — the check is strictly `>`, not `>=`, so the cap itself is
    a valid, exportable size."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    async def _fake_count(session, **kwargs):
        return usage_export_service.MAX_EXPORT_ROWS

    monkeypatch.setattr(usage_export_service, "count_usage_rollups", _fake_count)

    await usage_export_service.prepare_export(_principal(), _no_filter_params())


async def test_prepare_export_rejects_over_cap_with_422(monkeypatch, capsys):
    """AC8: a count over MAX_EXPORT_ROWS raises a 422 with no stream started,
    and logs the rejection at warn (AC17)."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    async def _fake_count(session, **kwargs):
        return usage_export_service.MAX_EXPORT_ROWS + 1

    monkeypatch.setattr(usage_export_service, "count_usage_rollups", _fake_count)

    with pytest.raises(HTTPException) as excinfo:
        await usage_export_service.prepare_export(_principal(), _no_filter_params())

    assert excinfo.value.status_code == 422
    captured = capsys.readouterr()
    assert "usage.export.rejected" in captured.out
    assert "row_cap_exceeded" in captured.out


async def test_prepare_export_propagates_non_cap_error_uncaught(monkeypatch):
    """AC22: an unexpected error during the pre-flight COUNT (e.g. a DB
    connection drop) is NOT caught or swallowed here — it propagates to the
    error-envelope boundary, fail-closed, so no stream is ever started."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    async def _raising_count(session, **kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(usage_export_service, "count_usage_rollups", _raising_count)

    with pytest.raises(RuntimeError, match="connection reset by peer"):
        await usage_export_service.prepare_export(_principal(), _no_filter_params())


async def test_stream_export_csv_is_header_only_on_empty_result(monkeypatch):
    """AC12: an empty result still yields the header row first — a
    header-only CSV, never an empty body / 404."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    async def _empty_stream(session, **kwargs):
        return
        yield  # pragma: no cover - makes this an async generator function

    monkeypatch.setattr(usage_export_service, "stream_usage_rollups", _empty_stream)

    chunks = [chunk async for chunk in usage_export_service.stream_export_csv(_principal(), _no_filter_params())]
    body = b"".join(chunks).decode("utf-8")

    assert body == "customer_id,metric,window_start,total_quantity\r\n"


async def test_stream_export_csv_rows_are_escaped_and_formatted(monkeypatch):
    """AC1/AC10/AC14: streamed rows are formula-escaped on text cells and
    formatted per the value-format contract."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    record = UsageRollupExportRecord(
        customer_id="-1",
        metric="api_calls",
        window_start=datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc),
        total_quantity=Decimal("12.500000"),
    )

    async def _one_row_stream(session, **kwargs):
        yield record

    monkeypatch.setattr(usage_export_service, "stream_usage_rollups", _one_row_stream)

    chunks = [chunk async for chunk in usage_export_service.stream_export_csv(_principal(), _no_filter_params())]
    body = b"".join(chunks).decode("utf-8")
    lines = body.split("\r\n")

    assert lines[0] == "customer_id,metric,window_start,total_quantity"
    assert lines[1] == "'-1,api_calls,2026-07-08T14:00:00+00:00,12.500000"


async def test_stream_export_csv_logs_completion_audit_event(monkeypatch, capsys):
    """AC17: exactly one `usage.export` info event at completion, with
    rowCount/capped/completed and no raw customer_id."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    record = UsageRollupExportRecord(
        customer_id="cust_super_secret",
        metric="api_calls",
        window_start=datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc),
        total_quantity=Decimal("1.000000"),
    )

    async def _one_row_stream(session, **kwargs):
        yield record

    monkeypatch.setattr(usage_export_service, "stream_usage_rollups", _one_row_stream)

    async for _chunk in usage_export_service.stream_export_csv(_principal(), _no_filter_params()):
        pass

    captured = capsys.readouterr()
    assert "usage.export" in captured.out
    assert '"rowCount": 1' in captured.out
    assert '"capped": false' in captured.out
    assert '"completed": true' in captured.out
    assert "cust_super_secret" not in captured.out


async def test_stream_export_csv_batches_rows_into_few_chunks(monkeypatch):
    """Regression (AC16 perf finding): the streaming generator must NOT emit
    one ASGI chunk per row.

    Root cause of the ~10-25s p95 at the 100,000-row cap: yielding a chunk per
    row fanned every row out through the app's four nested Starlette
    `BaseHTTPMiddleware` layers, each of which re-pumps every streamed chunk
    through its own anyio memory-object-stream — so the per-chunk stack cost ran
    100,000x at the cap, dwarfing the DB read and the CSV encoding. This guards
    the chunk-per-row anti-pattern from returning: a large row set must arrive in
    *far fewer chunks than rows* (batched) while still being genuinely streamed
    (more than one chunk, never a single buffered blob), with byte-identical CSV.
    """
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    row_total = 5000

    async def _many_rows(session, **kwargs):
        for i in range(row_total):
            yield UsageRollupExportRecord(
                customer_id=f"cust_{i:06d}",
                metric="api_calls",
                window_start=datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc),
                total_quantity=Decimal("1.000000"),
            )

    monkeypatch.setattr(usage_export_service, "stream_usage_rollups", _many_rows)

    chunks = [
        chunk async for chunk in usage_export_service.stream_export_csv(_principal(), _no_filter_params())
    ]

    # Batched, not one-chunk-per-row: the per-row implementation produced
    # row_total + 1 chunks (>= 500 here), which fails this bound; the batched
    # implementation produces ~row_total / _ROWS_PER_CHUNK chunks.
    assert len(chunks) < row_total / 10
    # ...but still genuinely streamed (header chunk + at least one data chunk),
    # never one fully-buffered blob — keeps AC9 honest.
    assert len(chunks) > 1
    # Batching changes only chunk boundaries, never the bytes: the full CSV is
    # intact (header + every data row, exactly once).
    body = b"".join(chunks).decode("utf-8")
    lines = body.strip("\r\n").split("\r\n")
    assert lines[0] == "customer_id,metric,window_start,total_quantity"
    assert len(lines) == row_total + 1


async def test_stream_export_csv_completion_is_false_on_mid_stream_failure(monkeypatch, capsys):
    """A mid-stream DB error still triggers exactly one completion log (via
    `finally`), but `completed=False` — records what was sent even though the
    body is truncated (plan's accepted risk R3, distinct from AC22's
    pre-flight case)."""
    monkeypatch.setattr(usage_export_service, "scoped_transaction", _fake_scoped_transaction_factory())

    record = UsageRollupExportRecord(
        customer_id="cust_1",
        metric="api_calls",
        window_start=datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc),
        total_quantity=Decimal("1.000000"),
    )

    async def _failing_stream(session, **kwargs):
        yield record
        raise RuntimeError("connection dropped mid-stream")

    monkeypatch.setattr(usage_export_service, "stream_usage_rollups", _failing_stream)

    with pytest.raises(RuntimeError, match="connection dropped mid-stream"):
        async for _chunk in usage_export_service.stream_export_csv(_principal(), _no_filter_params()):
            pass

    captured = capsys.readouterr()
    assert '"completed": false' in captured.out
    assert '"rowCount": 1' in captured.out
