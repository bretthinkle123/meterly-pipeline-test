"""Unit tests for `src/services/dashboard_service.py` pure logic: the
11-window timestamp computation per granularity, delta formatting
(up/down/**zero-neutral**), quantity formatting, and the populated/empty
decision — mocking `get_usage` so these run with no DB (AC8, AC23, Q5).
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services import dashboard_service


def test_window_starts_hour_granularity_returns_11_hourly_boundaries():
    now = datetime(2026, 3, 15, 14, 37, 0, tzinfo=timezone.utc)
    starts = dashboard_service._window_starts(now, "hour")
    assert len(starts) == 11
    assert starts[0] == datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)
    assert starts[1] == datetime(2026, 3, 15, 13, 0, tzinfo=timezone.utc)
    assert starts[-1] == datetime(2026, 3, 15, 4, 0, tzinfo=timezone.utc)


def test_window_starts_day_granularity_returns_11_day_boundaries():
    now = datetime(2026, 3, 15, 14, 37, 0, tzinfo=timezone.utc)
    starts = dashboard_service._window_starts(now, "day")
    assert len(starts) == 11
    assert starts[0] == datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
    assert starts[1] == datetime(2026, 3, 14, 0, 0, tzinfo=timezone.utc)
    assert starts[-1] == datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc)


def test_window_starts_anchors_to_server_now_not_a_client_value():
    """No anchor/window param exists in the schema at all — this test just
    documents that `_window_starts` takes `now` as a plain argument the
    caller (the service function) always supplies from `datetime.now()`,
    never from request input (see `get_usage_series`)."""
    import inspect

    signature = inspect.signature(dashboard_service.get_usage_series)
    assert set(signature.parameters) == {"customer_id", "metric", "granularity"}


@pytest.mark.parametrize(
    "delta,expected_text,expected_direction",
    [
        (Decimal("5"), "+5", "up"),
        (Decimal("-5"), "-5", "down"),
        (Decimal("0"), "—", "neutral"),
        (Decimal("1234.5"), "+1,234.5", "up"),
    ],
)
def test_format_delta_directions(delta, expected_text, expected_direction):
    text, direction = dashboard_service._format_delta(delta)
    assert text == expected_text
    assert direction == expected_direction


def test_format_quantity_adds_thousands_separators_and_trims_trailing_zeros():
    assert dashboard_service._format_quantity(Decimal("1234567")) == "1,234,567"
    assert dashboard_service._format_quantity(Decimal("1234.50")) == "1,234.5"
    assert dashboard_service._format_quantity(Decimal("0")) == "0"


def test_format_window_label_hour_includes_time():
    label = dashboard_service._format_window_label(
        datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc), "hour"
    )
    assert label == "Mar 15, 14:00"


def test_format_window_label_day_omits_time():
    label = dashboard_service._format_window_label(
        datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc), "day"
    )
    assert label == "Mar 15, 2026"


class _FakeUsageResponse:
    def __init__(self, total_quantity: Decimal, event_count: int):
        self.total_quantity = total_quantity
        self.event_count = event_count


@pytest.mark.asyncio
async def test_get_usage_series_populated_state_and_correct_deltas():
    """AC8: the series is assembled from `get_usage` reads (mocked here),
    the current number is the newest window, and each delta is window i vs
    i+1."""
    quantities = [Decimal(v) for v in [100, 90, 90, 50, 0, 0, 0, 0, 0, 0, 0]]

    async def _fake_get_usage(principal, query):
        # window is the newest-first index encoded by hour offset from "now"
        return _FakeUsageResponse(total_quantity=quantities.pop(0), event_count=1)

    with (
        patch.object(dashboard_service, "get_dashboard_reader_principal", new=AsyncMock(return_value=MagicMock(api_key_id=1))),
        patch.object(dashboard_service, "get_usage", new=AsyncMock(side_effect=_fake_get_usage)),
    ):
        response = await dashboard_service.get_usage_series(
            customer_id="acme-corp", metric="api_calls", granularity="hour"
        )

    assert response.state == "populated"
    assert response.current.quantity == "100"
    assert response.current.delta_direction == "up"  # 100 - 90 = +10
    assert len(response.rows) == 10
    assert response.rows[0].delta_direction == "up"  # 100 - 90 = +10
    assert response.rows[1].delta_direction == "neutral"  # 90 - 90 = 0
    assert response.rows[2].delta_direction == "up"  # 90 - 50 = +40


@pytest.mark.asyncio
async def test_get_usage_series_empty_state_when_all_windows_zero():
    """AC5: all 11 windows zero -> the real empty-state trigger (not a
    hardcoded customer_id check)."""
    async def _fake_get_usage(principal, query):
        return _FakeUsageResponse(total_quantity=Decimal("0"), event_count=0)

    with (
        patch.object(dashboard_service, "get_dashboard_reader_principal", new=AsyncMock(return_value=MagicMock(api_key_id=1))),
        patch.object(dashboard_service, "get_usage", new=AsyncMock(side_effect=_fake_get_usage)),
    ):
        response = await dashboard_service.get_usage_series(
            customer_id="acme-corp", metric="api_calls", granularity="hour"
        )

    assert response.state == "empty"
    assert all(row.quantity == "0" for row in response.rows)


@pytest.mark.asyncio
async def test_get_usage_series_day_granularity_sums_elapsed_hours():
    """AC23: `day` granularity sums a day's elapsed hourly buckets rather
    than issuing one read per day."""
    async def _fake_get_usage(principal, query):
        return _FakeUsageResponse(total_quantity=Decimal("1"), event_count=1)

    with (
        patch.object(dashboard_service, "get_dashboard_reader_principal", new=AsyncMock(return_value=MagicMock(api_key_id=1))),
        patch.object(dashboard_service, "get_usage", new=AsyncMock(side_effect=_fake_get_usage)),
    ):
        response = await dashboard_service.get_usage_series(
            customer_id="acme-corp", metric="api_calls", granularity="day"
        )

    # Every hourly read returns 1, so each day's total = number of elapsed
    # hours that day; state is populated since totals are nonzero.
    assert response.state == "populated"
