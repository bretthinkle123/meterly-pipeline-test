"""Validation-contract tests for `UsageExportQueryParams` (AC2, AC3, AC4)."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.api.schemas.usage_export import UsageExportQueryParams


def test_all_filters_omitted_is_valid():
    """AC2: every filter is optional — an empty query parses successfully."""
    query = UsageExportQueryParams()
    assert query.customer_id is None
    assert query.metric is None
    assert query.from_ is None
    assert query.to is None


def test_accepts_well_formed_filters():
    """AC2: customer_id/metric/from/to all parse when supplied together."""
    now = datetime.now(timezone.utc)
    query = UsageExportQueryParams(
        customer_id="cust_123",
        metric="api_calls",
        **{"from": (now - timedelta(days=1)).isoformat()},
        to=now.isoformat(),
    )
    assert query.customer_id == "cust_123"
    assert query.metric == "api_calls"
    assert query.from_ is not None
    assert query.to is not None


def test_from_alias_binds_to_from__field():
    """AC3: the query param name `from` (a Python keyword) binds to `from_`."""
    now = datetime.now(timezone.utc)
    query = UsageExportQueryParams(**{"from": now.isoformat()})
    assert query.from_ == now


def test_rejects_naive_from_datetime():
    """AC3: a naive (no-offset) `from` is rejected rather than silently assumed UTC."""
    with pytest.raises(ValidationError):
        UsageExportQueryParams(**{"from": "2026-01-01T00:00:00"})


def test_rejects_naive_to_datetime():
    """AC3: a naive (no-offset) `to` is rejected."""
    with pytest.raises(ValidationError):
        UsageExportQueryParams(to="2026-01-01T00:00:00")


def test_rejects_from_after_to():
    """AC3: from > to is an invalid (inverted) range."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        UsageExportQueryParams(**{"from": now.isoformat(), "to": (now - timedelta(days=1)).isoformat()})


def test_accepts_from_equal_to_to():
    """AC3: from == to is a valid (zero-width) range."""
    now = datetime.now(timezone.utc)
    query = UsageExportQueryParams(**{"from": now.isoformat(), "to": now.isoformat()})
    assert query.from_ == query.to


def test_rejects_from_too_far_in_the_past():
    """AC3: from older than 90 days is rejected (unbounded query-cost guard)."""
    too_old = datetime.now(timezone.utc) - timedelta(days=91)
    with pytest.raises(ValidationError):
        UsageExportQueryParams(**{"from": too_old.isoformat()})


def test_rejects_to_too_far_in_the_future():
    """AC3: to more than 1 hour ahead is rejected."""
    too_future = datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises(ValidationError):
        UsageExportQueryParams(to=too_future.isoformat())


@pytest.mark.parametrize("customer_id", ["' OR 1=1--", "../../etc/passwd", "line1\r\nline2"])
def test_rejects_injection_in_customer_id(customer_id):
    """AC4: injection payloads in customer_id are rejected at the boundary."""
    with pytest.raises(ValidationError):
        UsageExportQueryParams(customer_id=customer_id)


def test_rejects_unknown_fields():
    """AC4: `extra='forbid'` rejects an unexpected query parameter (e.g. a
    client-supplied `api_key_id` probe)."""
    with pytest.raises(ValidationError):
        UsageExportQueryParams(api_key_id=999)
