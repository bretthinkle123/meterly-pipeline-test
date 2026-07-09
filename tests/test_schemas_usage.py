"""Validation-contract tests for `UsageQueryParams` (AC15)."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.api.schemas.usage import UsageQueryParams


def _valid_query(**overrides) -> dict:
    payload = {
        "customer_id": "cust_123",
        "metric": "api_calls",
        "window": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_accepts_a_well_formed_query():
    """A valid, timezone-aware window within range parses successfully."""
    query = UsageQueryParams(**_valid_query())
    assert query.customer_id == "cust_123"


def test_rejects_naive_datetime():
    """A naive (no-offset) datetime is rejected rather than silently assumed UTC."""
    with pytest.raises(ValidationError):
        UsageQueryParams(**_valid_query(window="2026-01-01T00:00:00"))


def test_rejects_window_too_far_in_the_past():
    """A window older than 90 days is rejected (unbounded query-cost guard)."""
    too_old = datetime.now(timezone.utc) - timedelta(days=91)
    with pytest.raises(ValidationError):
        UsageQueryParams(**_valid_query(window=too_old.isoformat()))


def test_rejects_window_too_far_in_the_future():
    """A window more than 1 hour ahead is rejected."""
    too_future = datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises(ValidationError):
        UsageQueryParams(**_valid_query(window=too_future.isoformat()))


@pytest.mark.parametrize("customer_id", ["' OR 1=1--", "../../etc/passwd", "line1\r\nline2"])
def test_rejects_injection_in_customer_id(customer_id):
    """Injection payloads in customer_id are rejected at the boundary."""
    with pytest.raises(ValidationError):
        UsageQueryParams(**_valid_query(customer_id=customer_id))


def test_rejects_unknown_fields():
    """`extra='forbid'` rejects an unexpected query parameter."""
    with pytest.raises(ValidationError):
        UsageQueryParams(**_valid_query(api_key_id=999))
