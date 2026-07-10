"""Validation-contract tests for `QuotaPutRequest` (AC5, AC18) and
`QuotaDeleteParams` (AC9)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from src.api.schemas.quotas import QuotaDeleteParams, QuotaPutRequest


def _valid_payload(**overrides) -> dict:
    payload = {
        "customer_id": "cust_123",
        "metric": "api_calls",
        "limit_per_window": 1000,
    }
    payload.update(overrides)
    return payload


def test_accepts_a_well_formed_payload():
    """A valid payload parses with the exact fields supplied."""
    quota = QuotaPutRequest(**_valid_payload())
    assert quota.customer_id == "cust_123"
    assert quota.metric == "api_calls"
    assert quota.limit_per_window == 1000


def test_rejects_unknown_fields_mass_assignment():
    """`extra='forbid'` rejects a client-supplied `api_key_id`/`scope`
    (ASVS 15.3.3) — the server alone sets the tenant and never accepts a
    caller-supplied scope elevation."""
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(api_key_id=999))
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(scope="admin"))


@pytest.mark.parametrize("limit_per_window", [0, -1, -1000])
def test_rejects_non_positive_limit(limit_per_window):
    """A cap must be >= 1 — zero/negative is rejected (no kill-switch this slice)."""
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(limit_per_window=limit_per_window))


@pytest.mark.parametrize("limit_per_window", ["not-a-number", 1.5, None])
def test_rejects_non_integer_limit(limit_per_window):
    """A non-integer limit is rejected."""
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(limit_per_window=limit_per_window))


def test_rejects_absurdly_large_limit():
    """A limit beyond the BIGINT-safe upper bound is rejected (overflow guard)."""
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(limit_per_window=10**16))


def test_accepts_the_upper_bound_limit():
    """The declared upper bound itself (1e15) is accepted."""
    quota = QuotaPutRequest(**_valid_payload(limit_per_window=10**15))
    assert quota.limit_per_window == 10**15


@pytest.mark.parametrize(
    "customer_id",
    ["' OR 1=1--", "../../etc/passwd", "line1\r\nline2", "a" * 129, ""],
)
def test_rejects_injection_and_oversized_customer_id(customer_id):
    """SQLi/path-traversal/CRLF/oversized customer_id is rejected at the boundary (AC18)."""
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(customer_id=customer_id))


@pytest.mark.parametrize("metric", ["' OR 1=1--", "a" * 65, ""])
def test_rejects_invalid_metric(metric):
    """Injection/oversized metric is rejected at the boundary (AC18)."""
    with pytest.raises(ValidationError):
        QuotaPutRequest(**_valid_payload(metric=metric))


@given(customer_id=st.text(min_size=1, max_size=300))
def test_property_customer_id_only_ever_accepts_the_allowlist(customer_id):
    """Hypothesis: any customer_id that parses successfully matches the anchored allowlist."""
    import re

    try:
        quota = QuotaPutRequest(**_valid_payload(customer_id=customer_id))
    except ValidationError:
        return
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", quota.customer_id)


def _valid_delete_params(**overrides) -> dict:
    params = {"customer_id": "cust_123", "metric": "api_calls"}
    params.update(overrides)
    return params


def test_delete_params_accepts_well_formed_identifiers():
    """AC9: a valid (customer_id, metric) pair parses with the exact values supplied."""
    params = QuotaDeleteParams(**_valid_delete_params())
    assert params.customer_id == "cust_123"
    assert params.metric == "api_calls"


def test_delete_params_rejects_missing_fields():
    """AC9: both customer_id and metric are required."""
    with pytest.raises(ValidationError):
        QuotaDeleteParams(customer_id="cust_123")
    with pytest.raises(ValidationError):
        QuotaDeleteParams(metric="api_calls")


def test_delete_params_rejects_unknown_fields_mass_assignment():
    """AC9: `extra='forbid'` rejects a client-supplied `api_key_id` — the
    server alone resolves the tenant from the authenticated principal
    (ASVS 15.3.3)."""
    with pytest.raises(ValidationError):
        QuotaDeleteParams(**_valid_delete_params(api_key_id=999))


@pytest.mark.parametrize(
    "customer_id",
    ["' OR 1=1--", "../../etc/passwd", "line1\r\nline2", "a" * 129, ""],
)
def test_delete_params_rejects_injection_and_oversized_customer_id(customer_id):
    """AC9: SQLi/path-traversal/CRLF/oversized customer_id is rejected at the
    schema boundary and never reaches the DELETE SQL sink."""
    with pytest.raises(ValidationError):
        QuotaDeleteParams(**_valid_delete_params(customer_id=customer_id))


@pytest.mark.parametrize("metric", ["' OR 1=1--", "a" * 65, ""])
def test_delete_params_rejects_invalid_metric(metric):
    """AC9: injection/oversized metric is rejected at the schema boundary."""
    with pytest.raises(ValidationError):
        QuotaDeleteParams(**_valid_delete_params(metric=metric))


@given(customer_id=st.text(min_size=1, max_size=300))
def test_property_delete_customer_id_only_ever_accepts_the_allowlist(customer_id):
    """Hypothesis: any customer_id accepted by QuotaDeleteParams matches the anchored allowlist."""
    import re

    try:
        params = QuotaDeleteParams(**_valid_delete_params(customer_id=customer_id))
    except ValidationError:
        return
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", params.customer_id)
