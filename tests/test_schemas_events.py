"""Validation-contract tests for `EventCreateRequest` (AC13)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from src.api.schemas.events import EventCreateRequest


def _valid_payload(**overrides) -> dict:
    payload = {
        "customer_id": "cust_123",
        "metric": "api_calls",
        "quantity": "10.5",
        "idempotency_key": "idem-key-abc123",
    }
    payload.update(overrides)
    return payload


def test_accepts_a_well_formed_payload():
    """A valid payload parses and the quantity is an exact Decimal."""
    event = EventCreateRequest(**_valid_payload())
    assert event.quantity == Decimal("10.5")
    assert event.customer_id == "cust_123"


def test_rejects_unknown_fields_mass_assignment():
    """`extra='forbid'` rejects a client-supplied api_key_id (ASVS 15.3.3)."""
    with pytest.raises(ValidationError):
        EventCreateRequest(**_valid_payload(api_key_id=999))


def test_rejects_unknown_fields_event_time():
    """A client cannot backdate an event by supplying event_time (threat T2)."""
    with pytest.raises(ValidationError):
        EventCreateRequest(**_valid_payload(event_time="2000-01-01T00:00:00Z"))


@pytest.mark.parametrize(
    "customer_id",
    ["' OR 1=1--", "../../etc/passwd", "line1\r\nline2", "a" * 129, ""],
)
def test_rejects_injection_and_oversized_customer_id(customer_id):
    """SQLi/path-traversal/CRLF/oversized customer_id is rejected at the boundary."""
    with pytest.raises(ValidationError):
        EventCreateRequest(**_valid_payload(customer_id=customer_id))


@pytest.mark.parametrize("metric", ["' OR 1=1--", "a" * 65, ""])
def test_rejects_invalid_metric(metric):
    """Injection/oversized metric is rejected at the boundary."""
    with pytest.raises(ValidationError):
        EventCreateRequest(**_valid_payload(metric=metric))


@pytest.mark.parametrize("idempotency_key", ["has spaces", "a" * 201, ""])
def test_rejects_invalid_idempotency_key(idempotency_key):
    """Idempotency key outside the allowlist/length bound is rejected."""
    with pytest.raises(ValidationError):
        EventCreateRequest(**_valid_payload(idempotency_key=idempotency_key))


@pytest.mark.parametrize("quantity", ["0", "-5", "1000000000001", "not-a-number"])
def test_rejects_invalid_quantity(quantity):
    """Zero, negative, over-range, or non-numeric quantity is rejected."""
    with pytest.raises(ValidationError):
        EventCreateRequest(**_valid_payload(quantity=quantity))


@given(customer_id=st.text(min_size=1, max_size=300))
def test_property_customer_id_only_matches_allowlist_ever_accepts(customer_id):
    """Hypothesis: any customer_id that parses successfully matches the anchored allowlist."""
    import re

    try:
        event = EventCreateRequest(**_valid_payload(customer_id=customer_id))
    except ValidationError:
        return
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event.customer_id)
