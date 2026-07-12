"""Validation-contract tests for `GET /v1/usage/daily` (AC3, AC4, AC5, AC6,
AC10-unit): `parse_daily_date`'s format/calendar/range checks, the UTC
day-bounds computation (incl. month rollover and leap year), and
`DailyUsageQueryParams`'s `extra='forbid'` posture.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.schemas.usage_daily import (
    DailyMetricCount,
    DailyUsageQueryParams,
    DailyUsageResponse,
    day_window_for,
    parse_daily_date,
)


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def test_parses_a_well_formed_todays_date():
    """A well-formed date within range parses to a half-open UTC day window."""
    today = _today_utc_str()
    window = parse_daily_date(today)

    assert window.date_str == today
    assert window.day_end - window.day_start == timedelta(days=1)
    assert window.day_start.tzinfo == timezone.utc


def test_missing_date_raises_400():
    """AC3: `None` (the query param omitted) raises HTTPException(400)."""
    with pytest.raises(HTTPException) as exc_info:
        parse_daily_date(None)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "malformed",
    [
        "2026-7-1",
        "2026/07/11",
        "2026-13-40",
        "2026-02-30",
        "not-a-date",
        "",
        "2026-07-11 ",
        " 2026-07-11",
        "2026-07-11T00:00:00",
        "2026-07-11extra",
    ],
)
def test_malformed_date_raises_400(malformed):
    """AC4: every malformed form raises HTTPException(400), never 422/200."""
    with pytest.raises(HTTPException) as exc_info:
        parse_daily_date(malformed)
    assert exc_info.value.status_code == 400


def test_out_of_range_date_raises_400():
    """AC5: a date older than today-90d or later than today+1d raises 400."""
    today = datetime.now(timezone.utc).date()
    too_old = (today - timedelta(days=200)).isoformat()
    too_future = (today + timedelta(days=5)).isoformat()

    with pytest.raises(HTTPException) as exc_info_old:
        parse_daily_date(too_old)
    assert exc_info_old.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info_future:
        parse_daily_date(too_future)
    assert exc_info_future.value.status_code == 400


def test_exactly_ninety_days_ago_is_in_range():
    """The lower bound `today_utc-90d` is inclusive."""
    today = datetime.now(timezone.utc).date()
    boundary = (today - timedelta(days=90)).isoformat()
    window = parse_daily_date(boundary)
    assert window.date_str == boundary


def test_exactly_one_day_ahead_is_in_range():
    """The upper bound `today_utc+1d` is inclusive."""
    today = datetime.now(timezone.utc).date()
    boundary = (today + timedelta(days=1)).isoformat()
    window = parse_daily_date(boundary)
    assert window.date_str == boundary


def test_day_bounds_utc():
    """The parsed window is `[day_start, day_start+1d)` in UTC, verified
    against a fixed, in-range reference date rather than 'today' so the test
    is deterministic regardless of when it runs."""
    reference = datetime.now(timezone.utc).date() - timedelta(days=10)
    window = parse_daily_date(reference.isoformat())

    expected_start = datetime(reference.year, reference.month, reference.day, tzinfo=timezone.utc)
    assert window.day_start == expected_start
    assert window.day_end == expected_start + timedelta(days=1)


def test_day_window_for_handles_month_rollover():
    """A date at the end of a month floors/ceils correctly across the
    boundary. Exercises the pure `day_window_for` helper directly so the
    assertion is independent of the sliding `[today-90d, today+1d]` range
    `parse_daily_date` enforces (a fixed historical/future date would
    otherwise be flaky against that moving window)."""
    last_day_of_january = date(2026, 1, 31)
    day_start, day_end = day_window_for(last_day_of_january)

    assert day_start == datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert day_end == datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_day_window_for_handles_leap_year_feb_29():
    """Feb 29 on a leap year is a valid calendar date whose window spans to
    Mar 1 (also exercised via the pure helper for the same reason as above)."""
    day_start, day_end = day_window_for(date(2024, 2, 29))

    assert day_start == datetime(2024, 2, 29, tzinfo=timezone.utc)
    assert day_end == datetime(2024, 3, 1, tzinfo=timezone.utc)


def test_feb_29_on_a_non_leap_year_is_malformed():
    """AC4: Feb 29 on a non-leap year is a calendar-invalid date -> 400."""
    with pytest.raises(HTTPException) as exc_info:
        parse_daily_date("2025-02-29")
    assert exc_info.value.status_code == 400


def test_query_params_accepts_a_well_formed_date():
    """`DailyUsageQueryParams.date` accepts an arbitrary well-formed string
    (the imperative parser enforces the value contract, not the field type)."""
    query = DailyUsageQueryParams(date="2026-07-11")
    assert query.date == "2026-07-11"


def test_query_params_accepts_omitted_date():
    """`date` is optional at the model level -> a missing value is not a
    422/ValidationError; `parse_daily_date(None)` raises the 400 instead."""
    query = DailyUsageQueryParams()
    assert query.date is None


def test_query_params_rejects_unknown_fields():
    """AC6: `extra='forbid'` rejects an unexpected query parameter."""
    with pytest.raises(ValidationError):
        DailyUsageQueryParams(date="2026-07-11", customer_id="cust_1")


def test_response_model_rejects_unknown_fields():
    """ASVS 15.3.1: the response model itself is `extra='forbid'` (no
    api_key_id or other field can ever be echoed back)."""
    with pytest.raises(ValidationError):
        DailyUsageResponse(date="2026-07-11", metrics=[], api_key_id=1)


def test_response_model_shape():
    """`DailyUsageResponse` carries `date` + an ordered `metrics` list of
    `{metric, event_count}`."""
    response = DailyUsageResponse(
        date="2026-07-11",
        metrics=[DailyMetricCount(metric="api_calls", event_count=5)],
    )
    assert response.date == "2026-07-11"
    assert response.metrics[0].metric == "api_calls"
    assert response.metrics[0].event_count == 5


def test_metric_count_rejects_unknown_fields():
    """ASVS 15.3.1: the per-metric entry model is also `extra='forbid'`."""
    with pytest.raises(ValidationError):
        DailyMetricCount(metric="api_calls", event_count=5, customer_id="cust_1")
