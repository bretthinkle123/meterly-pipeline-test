"""Unit tests for `src/auth/dashboard_reader.py` — the server-held reader
principal resolution + memoization (AC9: no client key; the credential is
resolved server-side and cached with a short TTL)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.auth import dashboard_reader


@pytest.fixture(autouse=True)
def _reset_cache():
    dashboard_reader.invalidate_cached_reader_principal()
    yield
    dashboard_reader.invalidate_cached_reader_principal()


class _FakeConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeEngine:
    def connect(self):
        return _FakeConnection()


async def test_resolves_and_caches_the_principal():
    fake_principal = MagicMock()
    with (
        patch.object(dashboard_reader, "get_secret", return_value="mtr_live_fake_secret"),
        patch.object(dashboard_reader, "get_engine", return_value=_FakeEngine()),
        patch.object(dashboard_reader, "verify_api_key", new=AsyncMock(return_value=fake_principal)),
    ):
        principal = await dashboard_reader.get_dashboard_reader_principal()
        assert principal is fake_principal


async def test_second_call_within_ttl_reuses_the_cached_principal_no_reverify():
    fake_principal = MagicMock()
    verify_mock = AsyncMock(return_value=fake_principal)
    with (
        patch.object(dashboard_reader, "get_secret", return_value="mtr_live_fake_secret"),
        patch.object(dashboard_reader, "get_engine", return_value=_FakeEngine()),
        patch.object(dashboard_reader, "verify_api_key", new=verify_mock),
    ):
        await dashboard_reader.get_dashboard_reader_principal()
        await dashboard_reader.get_dashboard_reader_principal()

    assert verify_mock.call_count == 1, "the memoized principal must not re-verify within the TTL"


async def test_ttl_expiry_triggers_a_fresh_resolve():
    fake_principal = MagicMock()
    verify_mock = AsyncMock(return_value=fake_principal)
    with (
        patch.object(dashboard_reader, "get_secret", return_value="mtr_live_fake_secret"),
        patch.object(dashboard_reader, "get_engine", return_value=_FakeEngine()),
        patch.object(dashboard_reader, "verify_api_key", new=verify_mock),
        patch.object(dashboard_reader, "time") as time_mock,
    ):
        time_mock.monotonic.side_effect = [0.0, 1000.0, 1000.0]
        await dashboard_reader.get_dashboard_reader_principal()
        await dashboard_reader.get_dashboard_reader_principal()

    assert verify_mock.call_count == 2, "past the TTL the principal must be re-resolved and re-verified"


async def test_failed_verification_raises_runtime_error_never_returns_none():
    """A reader key that fails verification must raise (propagating to the
    generic 500 envelope, AC24) rather than silently proceeding with no
    principal."""
    with (
        patch.object(dashboard_reader, "get_secret", return_value="mtr_live_bad_secret"),
        patch.object(dashboard_reader, "get_engine", return_value=_FakeEngine()),
        patch.object(dashboard_reader, "verify_api_key", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(RuntimeError):
            await dashboard_reader.get_dashboard_reader_principal()


async def test_unresolvable_secret_propagates_as_runtime_error():
    """If the secrets facade cannot resolve the reader key at all (no
    Secrets Manager, no env fallback), the failure surfaces as a RuntimeError
    rather than crashing with an unhandled exception type."""
    with (
        patch.object(dashboard_reader, "get_secret", side_effect=RuntimeError("secret not found")),
    ):
        with pytest.raises(RuntimeError):
            await dashboard_reader.get_dashboard_reader_principal()
