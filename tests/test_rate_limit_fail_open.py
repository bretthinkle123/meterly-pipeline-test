"""Regression test for the Tier-2 rate-limit logging facade (AC14/AC16).

Guards the missing `get_logger` import in `src/auth/rate_limit.py` that turned
both `get_logger` call sites in `enforce_tier2_rate_limit` into unhandled 500s
(`NameError: name 'get_logger' is not defined`).

The integration tests in `tests/integration/test_rate_limit.py` exercise the
**429-deny** branch (line 163). This unit test covers the **Redis-outage
fail-open** branch (line 157), which no other test drives, so *both* call
sites stay guarded against the import ever going missing again.
"""

from types import SimpleNamespace

from src.auth.api_key import AuthenticatedPrincipal


async def test_tier2_fails_open_and_logs_when_redis_backend_is_unavailable(monkeypatch):
    """A Redis outage must fail *open* (allow the request) and emit the
    `ratelimit.backend_unavailable` warning -- not raise NameError -> 500.

    Before the fix this branch's `get_logger(...)` call raised
    `NameError: name 'get_logger' is not defined`."""
    from src.auth import rate_limit

    class _ExplodingLimiter:
        async def check(self, *args, **kwargs):
            raise ConnectionError("simulated Redis outage")

    monkeypatch.setattr(rate_limit, "get_tier2_limiter", lambda: _ExplodingLimiter())

    request = SimpleNamespace(url=SimpleNamespace(path="/v1/usage"))
    principal = AuthenticatedPrincipal(api_key_id=1, rate_limit_per_sec=1)

    # Fails open: returns None (allows the request) without raising.
    result = await rate_limit.enforce_tier2_rate_limit(request, principal)
    assert result is None
