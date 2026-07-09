"""CSV encoding facade for `GET /v1/usage/export`.

The single boundary that owns the RFC 4180 column contract and the OWASP
CSV/formula-injection escape (`code-standards` facade rule) — centralizing it
here, rather than inlining it in the streaming generator, keeps the control
greppable, independently unit-testable, and the one place the security agent
verifies it lives.
"""

from decimal import Decimal
from datetime import datetime

EXPORT_HEADER = ["customer_id", "metric", "window_start", "total_quantity"]

# A spreadsheet application (Excel/Sheets) treats a cell beginning with any of
# these characters as the start of a formula/command — the OWASP CSV/formula-
# injection trigger set.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")

# Control characters other than tab/CR/LF, which RFC 4180 quoting (handled by
# `csv.writer`'s QUOTE_MINIMAL at the generator that calls this facade) already
# renders safely — everything else is a raw control byte with no legitimate
# reason to appear in a customer/metric identifier and is stripped outright.
_SAFE_WHITESPACE = ("\t", "\r", "\n")

# Precomputed `str.translate` deletion table for exactly the control bytes the
# strip step removes: every C0 control (0x00–0x1F) except tab/CR/LF, plus DEL
# (0x7F). Encoding runs this per text cell for every exported row (up to 200,000
# cell escapes at the 100,000-row cap), so the strip is done by the C-level
# `str.translate` in a single pass rather than a per-character Python generator +
# function call + `ord()` (the previous hot path). Semantics are identical to the
# prior `_is_safe_character` filter — same characters kept, same characters
# removed — verified by the unchanged AC10 escape unit tests.
_CONTROL_STRIP_TABLE = {
    codepoint: None for codepoint in range(0x20) if chr(codepoint) not in _SAFE_WHITESPACE
}
_CONTROL_STRIP_TABLE[0x7F] = None


def escape_csv_text_cell(value: str) -> str:
    """Neutralize a text cell against spreadsheet formula-injection.

    Applied at the CSV encoding *sink*, independent of upstream ingest
    validation: today's ingest allowlist (`^[A-Za-z0-9_.:-]{1,128}$`) permits
    a leading `-`, which is a live formula trigger in Excel/Sheets, so this is
    not hypothetical defense-in-depth — it is a real, reachable case. Strips
    embedded raw control characters, then prefixes a single quote `'` when the
    (post-strip) first character is a formula trigger; a spreadsheet then
    renders the cell as literal text rather than evaluating it. RFC 4180
    quoting itself (comma/quote/CR/LF) is the caller's `csv.writer`'s job, not
    this function's.
    """
    if not value:
        return value
    cleaned = value.translate(_CONTROL_STRIP_TABLE)
    if cleaned and cleaned[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + cleaned
    return cleaned


def format_window_start(window_start: datetime) -> str:
    """Render `window_start` identically to the JSON `GET /v1/usage`
    response's string form: UTC ISO-8601 with an explicit offset."""
    return window_start.isoformat()


def format_total_quantity(total_quantity: Decimal) -> str:
    """Render `total_quantity` as a plain decimal string.

    `Decimal.__str__` never emits scientific notation and preserves
    `Numeric(38,6)` scale-6 trailing zeros, which keeps the export
    deterministic and diff-clean (the brief's "plain decimal" requirement).
    Numeric cells are server-generated and never attacker-controlled, so they
    are deliberately not passed through `escape_csv_text_cell` — doing so
    would corrupt the value.
    """
    return str(total_quantity)
