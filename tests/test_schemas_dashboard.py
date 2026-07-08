"""Validation-contract tests for `UsageSeriesQueryParams` / `ConfigResponse`
(AC11, AC21) — the dashboard BFF's one untrusted input surface.

`customer_id`/`metric` must be anchored-pattern *and* allowlist members;
`granularity` is a closed `{hour, day}` enum with `month` deliberately
excluded (Q1); `extra='forbid'` rejects any unknown query param.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from src.api.schemas.dashboard import ConfigResponse, UsageSeriesQueryParams
from src.config.settings import get_settings


def _valid_query(**overrides) -> dict:
    settings = get_settings()
    payload = {
        "customer_id": settings.dashboard_customers[0],
        "metric": settings.dashboard_metrics[0],
        "granularity": "hour",
    }
    payload.update(overrides)
    return payload


def test_accepts_a_well_formed_query():
    """A valid, allowlisted customer/metric + hour granularity parses."""
    query = UsageSeriesQueryParams(**_valid_query())
    assert query.granularity == "hour"


def test_accepts_day_granularity():
    """`day` is a supported granularity (Q1: hour+day live)."""
    query = UsageSeriesQueryParams(**_valid_query(granularity="day"))
    assert query.granularity == "day"


def test_rejects_month_granularity():
    """AC23/Q1: `month` is excluded — the hourly rollup cannot serve it
    within the 90-day lookback bound, so it must 422 at the schema, never
    silently fall through to `day`."""
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(**_valid_query(granularity="month"))


def test_rejects_customer_id_not_in_allowlist():
    """A syntactically valid but non-allowlisted customer_id is rejected —
    the boundary allowlist that stops enumeration (I-D3)."""
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(**_valid_query(customer_id="not-a-real-customer"))


def test_rejects_metric_not_in_allowlist():
    """A syntactically valid but non-allowlisted metric is rejected."""
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(**_valid_query(metric="not-a-real-metric"))


@pytest.mark.parametrize("customer_id", ["' OR 1=1--", "<script>alert(1)</script>", "../../etc/passwd", "line1\r\nline2"])
def test_rejects_injection_payloads_in_customer_id(customer_id):
    """Injection payloads are rejected at the boundary (pattern AND
    allowlist), never reaching the `get_usage` SQL sink."""
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(**_valid_query(customer_id=customer_id))


@pytest.mark.parametrize("metric", ["' OR 1=1--", "<script>alert(1)</script>", "../../etc/passwd"])
def test_rejects_injection_payloads_in_metric(metric):
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(**_valid_query(metric=metric))


def test_rejects_unknown_query_param():
    """`extra='forbid'` rejects an unexpected param (e.g. a client-supplied
    window/anchor, which would defeat the server-`now()` anchoring)."""
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(**_valid_query(window="2026-01-01T00:00:00Z"))


def test_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        UsageSeriesQueryParams(granularity="hour")


@given(st.text(min_size=1, max_size=200))
def test_property_arbitrary_text_customer_id_never_raises_uncontrolled_exception(candidate):
    """Hypothesis: ANY string customer_id either validates successfully (only
    possible for exact allowlist members) or raises the well-typed
    `ValidationError` — never an unhandled exception (fails closed)."""
    try:
        UsageSeriesQueryParams(**_valid_query(customer_id=candidate))
    except ValidationError:
        pass


def test_config_response_shape_rejects_unknown_fields():
    """`ConfigResponse` is also `extra='forbid'` for symmetry with the
    request-side contract."""
    with pytest.raises(ValidationError):
        ConfigResponse(
            customers=["a"], metrics=["b"], granularities=["hour"], environment="prod", extra_field="x"
        )


def test_config_response_accepts_the_configured_shape():
    response = ConfigResponse(
        customers=["acme-corp"], metrics=["api_calls"], granularities=["hour", "day"], environment="staging"
    )
    assert response.environment == "staging"
