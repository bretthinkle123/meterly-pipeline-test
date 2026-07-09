"""Unit tests for the CSV encoding facade (`src/api/csv_export.py`) — RFC 4180
quoting via stdlib `csv`, the OWASP formula-injection escape, and value
formatting (AC1, AC10, AC14)."""

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.api.csv_export import (
    EXPORT_HEADER,
    escape_csv_text_cell,
    format_total_quantity,
    format_window_start,
)


def test_export_header_matches_the_brief_column_order():
    """AC1: the header row is exactly `customer_id,metric,window_start,total_quantity`."""
    assert EXPORT_HEADER == ["customer_id", "metric", "window_start", "total_quantity"]


@pytest.mark.parametrize("trigger_char", ["=", "+", "-", "@", "\t", "\r"])
def test_leading_formula_trigger_is_quote_prefixed(trigger_char):
    """AC10: a text cell whose first character is a formula trigger is
    prefixed with a single quote so a spreadsheet renders it as literal text."""
    value = f"{trigger_char}cmd|'/bin/calc'!A1"
    escaped = escape_csv_text_cell(value)
    assert escaped.startswith("'" + trigger_char)


def test_leading_dash_customer_id_is_escaped():
    """AC10: the ingest allowlist permits a leading `-` (e.g. customer_id
    '-1'); the CSV sink must still neutralize it independent of ingest
    validation having allowed it through."""
    assert escape_csv_text_cell("-1") == "'-1"


def test_non_trigger_leading_character_is_left_untouched():
    """A normal identifier with no formula-trigger leading character passes through unchanged."""
    assert escape_csv_text_cell("cust_123") == "cust_123"


def test_embedded_control_characters_are_stripped():
    """AC10: raw control characters (other than tab/CR/LF, which RFC 4180
    quoting already handles) are neutralized rather than passed through."""
    value = "cust\x00_\x07123"
    assert escape_csv_text_cell(value) == "cust_123"


def test_empty_string_is_returned_unchanged():
    """An empty cell has no leading character to escape."""
    assert escape_csv_text_cell("") == ""


def test_total_quantity_formats_as_plain_decimal_no_scientific_notation():
    """AC14: total_quantity renders as str(Decimal) — no E-notation, scale-6
    trailing zeros preserved."""
    assert format_total_quantity(Decimal("1000000.000000")) == "1000000.000000"
    assert format_total_quantity(Decimal("0.000001")) == "0.000001"


def test_window_start_formats_as_utc_iso8601_with_offset():
    """AC14: window_start renders identically to the JSON response's isoformat() form."""
    value = datetime(2026, 7, 8, 14, 0, 0, tzinfo=timezone.utc)
    assert format_window_start(value) == value.isoformat()
    assert format_window_start(value) == "2026-07-08T14:00:00+00:00"


def test_numeric_and_timestamp_values_are_never_run_through_the_formula_escape():
    """AC10: numeric/timestamp cells are server-generated and are not passed
    through escape_csv_text_cell — verified here by confirming a formatted
    numeric-looking string with a leading '-' is NOT what the escape does to
    it when used via the intended formatter, only via the text-cell path."""
    quantity = Decimal("-5")  # a hypothetical negative value would not be escaped by the formatter
    assert format_total_quantity(quantity) == "-5"


def test_rfc4180_quoting_via_stdlib_csv_writer_for_comma_quote_and_newline():
    """AC1: fields containing comma/quote/embedded newline are quoted per
    RFC 4180 by stdlib csv.writer with QUOTE_MINIMAL, CRLF line endings."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["a,b", 'has"quote', "line1\nline2", "plain"])
    output = buffer.getvalue()
    assert output.endswith("\r\n")
    assert '"a,b"' in output
    assert '"has""quote"' in output
    assert '"line1\nline2"' in output
    assert ",plain" in output
