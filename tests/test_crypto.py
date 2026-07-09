"""Tests for the DataProtection crypto facade (AC8/AC-DATA-PROTECTION)."""

from src.crypto import constant_time_equals, hash_secret, sha256_digest, verify_secret


def test_hash_secret_produces_an_argon2id_hash_not_plaintext():
    """The persisted form is an Argon2id-encoded hash, never the plaintext secret."""
    secret = "a-high-entropy-api-key-secret-value"
    stored_hash = hash_secret(secret)
    assert stored_hash != secret
    assert stored_hash.startswith("$argon2id$")


def test_verify_secret_round_trips():
    """A secret verifies successfully against its own stored hash."""
    secret = "another-high-entropy-secret"
    stored_hash = hash_secret(secret)
    assert verify_secret(secret, stored_hash) is True


def test_verify_secret_rejects_wrong_secret():
    """A different secret fails verification against the stored hash."""
    stored_hash = hash_secret("correct-secret")
    assert verify_secret("wrong-secret", stored_hash) is False


def test_verify_secret_fails_closed_on_malformed_hash():
    """A malformed/legacy stored value fails closed (False), never raises."""
    assert verify_secret("anything", "not-a-real-argon2-hash") is False


def test_sha256_digest_is_deterministic_and_irreversible_looking():
    """The same input always digests identically; digest never equals the input."""
    value = "mtr_live_abc123_def456"
    assert sha256_digest(value) == sha256_digest(value)
    assert sha256_digest(value) != value
    assert len(sha256_digest(value)) == 64


def test_constant_time_equals_matches_and_mismatches():
    """Equality behaves like a normal string comparison (just constant-time)."""
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
