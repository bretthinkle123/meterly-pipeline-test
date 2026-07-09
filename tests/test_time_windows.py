"""Tests for the UTC hour-flooring helper shared by both services."""

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from src.services.time_windows import floor_to_hour_utc


def test_floors_minutes_seconds_microseconds():
    """A timestamp mid-hour floors to that hour's start, in UTC."""
    timestamp = datetime(2026, 3, 15, 14, 37, 52, 123456, tzinfo=timezone.utc)
    assert floor_to_hour_utc(timestamp) == datetime(2026, 3, 15, 14, 0, 0, 0, tzinfo=timezone.utc)


def test_converts_a_non_utc_timezone_before_flooring():
    """A timestamp in another timezone is converted to UTC before flooring —
    the timezone trap `date_trunc` would otherwise fall into."""
    minus_five = timezone(timedelta(hours=-5))
    timestamp = datetime(2026, 3, 15, 9, 37, 0, tzinfo=minus_five)  # 14:37 UTC
    assert floor_to_hour_utc(timestamp) == datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)


@given(
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 1, 1),
    )
)
def test_property_floored_result_is_always_exactly_on_the_hour(naive_timestamp):
    """Hypothesis: the floored result always has zero minutes/seconds/microseconds."""
    timestamp = naive_timestamp.replace(tzinfo=timezone.utc)
    floored = floor_to_hour_utc(timestamp)
    assert floored.minute == 0
    assert floored.second == 0
    assert floored.microsecond == 0
    assert floored <= timestamp
