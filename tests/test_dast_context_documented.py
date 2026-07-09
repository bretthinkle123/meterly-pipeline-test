"""AC24 (DAST-3): the scanner auth context — header name + token shape — must
be documented somewhere a DAST job (ZAP/Schemathesis config) can read it."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bearer_split_token_auth_context_is_documented():
    """The plan documents the `Authorization: Bearer mtr_live_<key_id>_<secret>`
    scanner auth context (DAST-3) — presence check across the plan/docs."""
    candidates = [
        _REPO_ROOT / ".pipeline" / "plan.md",
        _REPO_ROOT / "docs" / "system_architecture.md",
    ]
    combined_text = ""
    for path in candidates:
        if path.exists():
            combined_text += path.read_text(encoding="utf-8", errors="ignore")

    assert "mtr_live" in combined_text, "the split-token key format must be documented"
    assert "Bearer" in combined_text, "the Authorization scheme (Bearer) must be documented"
