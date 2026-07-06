"""AC9 (data_protection log-sink half): a raw `customer_id` (or any other
sensitive field) must never reach the rendered log line, regardless of which
call site logs it."""

import json

from src.logging import _redact_sensitive_fields, _strip_control_characters, get_logger


def test_redact_sensitive_fields_masks_customer_id():
    """`customer_id` is redacted before rendering, wherever a call site logs it."""
    event_dict = {"event": "usage.read", "customer_id": "cust_super_secret_123", "userId": 7}
    redacted = _redact_sensitive_fields(None, "info", event_dict)
    assert redacted["customer_id"] == "***redacted***"
    assert redacted["userId"] == 7


def test_redact_sensitive_fields_masks_authorization_and_secret_keys():
    """Every sensitive key name is redacted, not only customer_id."""
    event_dict = {
        "authorization": "Bearer mtr_live_abc_def",
        "api_key_secret": "shh",
        "secret_hash": "$argon2id$...",
        "password": "hunter2",
        "token": "abc123",
    }
    redacted = _redact_sensitive_fields(None, "info", event_dict)
    assert all(value == "***redacted***" for value in redacted.values())


def test_strip_control_characters_neutralizes_crlf_log_forging():
    """A newline/CRLF in a logged string value is neutralized, not rendered raw."""
    event_dict = {"endpoint": "/v1/events\r\nfake: injected-header"}
    stripped = _strip_control_characters(None, "info", event_dict)
    assert "\r" not in stripped["endpoint"]
    assert "\n" not in stripped["endpoint"]
    assert "\\r\\n" in stripped["endpoint"]


def test_get_logger_end_to_end_never_prints_raw_customer_id(capsys):
    """A real log call through the configured structlog pipeline never emits
    the raw customer_id in the rendered (stdout JSON) output."""
    logger = get_logger(service="meterly")
    logger.info("usage.read", userId=7, action="read", customer_id="cust_should_never_appear")

    captured = capsys.readouterr()
    assert "cust_should_never_appear" not in captured.out

    rendered = json.loads(captured.out.strip().splitlines()[-1])
    assert rendered["customer_id"] == "***redacted***"
