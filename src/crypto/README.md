# src/crypto/

## Purpose

Cryptographic utilities: Argon2id credential hashing (for API key secrets), constant-time comparison, and credential generation.

## Modules

| File / Module | Responsibility |
|---|---|
| `__init__.py` | `hash_secret(secret: str) -> str` — Argon2id hash; `verify_secret(secret: str, hash: str) -> bool` — constant-time comparison; `generate_secret(length: int) -> str` — random secret generation. |

## Relationships

**Public surface:**
- `hash_secret()` is called by `scripts/seed_api_key.py` when provisioning a new API key.
- `verify_secret()` is called by `src/auth.api_key` during credential verification (cache miss or always-verify path).
- `generate_secret()` is called by the key provisioning script.

**Dependencies:**
- Imports `argon2` library (from `argon2-cffi` package, a native binding to libargon2).
- Imports `secrets` module (Python standard library) for random generation.

**Usage context:**
- At API key creation, the plaintext secret is hashed with Argon2id and stored as `api_keys.secret_hash`.
- The plaintext secret is returned to the caller (displayed once, never stored).
- At authentication (every request), the provided secret is verified against the stored hash using constant-time comparison (prevents timing attacks).

## Notes

**Argon2id parameters:**
- Memory: 65 MiB (reasonable for container workloads; balances security and latency).
- Time cost: 3 iterations (calibrated to ~100ms on typical hardware).
- Parallelism: 4 threads.
- These parameters are frozen in code (not configurable); they match industry defaults and the OWASP recommendation.

**Constant-time comparison (TIMING-ATTACK-RESISTANT):**
- The `verify_secret()` function uses `argon2.PasswordHasher.verify()`, which internally uses `hmac.compare_digest()`.
- This ensures the comparison time is independent of the input (no early-exit on first mismatch).
- Protects against timing side-channel attacks (attacker timing the response to infer secret bytes).

**Credential generation:**
- `generate_secret()` uses `secrets.token_urlsafe(length)` to generate a URL-safe random string.
- Default length: 32 bytes, encoded as 43 characters (base64url).
- Output is suitable for use as the secret half of a split-token API key.

**Test coverage:**
- `tests/test_crypto.py` verifies:
  - Hash format is Argon2id (starts with `$argon2id$...`).
  - Plaintext secret is never stored or logged.
  - Correct secret verifies (returns True).
  - Incorrect secret is rejected (returns False).
  - Hash is salted and unique (two hashes of the same secret are different).
