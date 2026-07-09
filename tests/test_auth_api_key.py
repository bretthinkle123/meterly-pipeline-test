"""Tests for split-token parsing and the in-process verification cache
(AC18 unauthenticated-denial cases; the Argon2id-vs-p95 cache mechanism)."""

from src.auth import ApiKeyVerificationCache
from src.auth.api_key import AuthenticatedPrincipal, generate_split_token, parse_split_token
from src.crypto import sha256_digest


def test_parse_split_token_accepts_well_formed_token():
    """A well-formed `mtr_live_<key_id>_<secret>` token parses into its two halves."""
    _key_id, _secret, presented_key = generate_split_token()
    parsed = parse_split_token(presented_key)
    assert parsed is not None
    assert presented_key == f"mtr_live_{parsed.key_id}_{parsed.secret}"


def test_parse_split_token_rejects_malformed_tokens():
    """Malformed tokens are rejected before any database work (401 fast-path)."""
    for malformed in ["", "not-a-token", "mtr_live_", "mtr_live_abc", "bearer mtr_live_abc_def", "mtr_test_abc_def"]:
        assert parse_split_token(malformed) is None


def test_generate_split_token_round_trips_through_parse():
    """A freshly generated token parses back to the same key_id/secret it was built from."""
    key_id, secret, presented_key = generate_split_token()
    parsed = parse_split_token(presented_key)
    assert parsed.key_id == key_id
    assert parsed.secret == secret


def test_verification_cache_hit_requires_matching_digest():
    """A cache hit only occurs when the presented key's digest matches the cached one."""
    cache = ApiKeyVerificationCache(ttl_seconds=300)
    principal = AuthenticatedPrincipal(api_key_id=42, rate_limit_per_sec=100)
    digest = sha256_digest("mtr_live_somekey_somesecret")

    cache.put("somekey", digest, principal)

    assert cache.get("somekey", digest) == principal
    assert cache.get("somekey", sha256_digest("a-different-presented-key")) is None
    assert cache.get("unknown-key-id", digest) is None


def test_verification_cache_expires_after_ttl(monkeypatch):
    """An entry older than the TTL is treated as a miss (forces re-verification)."""
    import time

    cache = ApiKeyVerificationCache(ttl_seconds=1)
    principal = AuthenticatedPrincipal(api_key_id=7, rate_limit_per_sec=100)
    digest = sha256_digest("mtr_live_somekey_somesecret")
    cache.put("somekey", digest, principal)

    monkeypatch.setattr(time, "monotonic", lambda: 9999999.0)
    assert cache.get("somekey", digest) is None


def test_verification_cache_invalidate_forces_a_miss():
    """Explicit invalidation (used by tests / a future revocation channel) clears the entry."""
    cache = ApiKeyVerificationCache(ttl_seconds=300)
    principal = AuthenticatedPrincipal(api_key_id=1, rate_limit_per_sec=100)
    digest = sha256_digest("mtr_live_somekey_somesecret")
    cache.put("somekey", digest, principal)
    cache.invalidate("somekey")
    assert cache.get("somekey", digest) is None
