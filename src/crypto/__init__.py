"""DataProtection crypto facade — the only module allowed to touch a KDF or a
constant-time comparison.

Every field that needs a credential-class at-rest control (`api_keys.secret_hash`)
routes through here rather than calling `argon2-cffi` inline. Centralizing this
keeps the mechanism greppable and auditable in one place (`data-protection-conventions`).
"""

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Argon2id is deliberately slow (tens of ms) — this is the load-bearing property
# that makes brute-forcing a stolen hash expensive. See src/auth/__init__.py for
# how the request path avoids paying this cost on every request via a verification
# cache, while the at-rest store stays Argon2id-only (never a faster hash).
_password_hasher = PasswordHasher()


def hash_secret(secret: str) -> str:
    """Hash an API-key secret with Argon2id for storage (`api_keys.secret_hash`).

    Returns the encoded Argon2id hash string (includes algorithm parameters and
    salt) — never the plaintext secret.
    """
    return _password_hasher.hash(secret)


def verify_secret(secret: str, stored_hash: str) -> bool:
    """Verify a presented secret against its stored Argon2id hash.

    Returns True on match, False on any mismatch/malformed-hash error — never
    raises to the caller so a bad stored value can't become a 500 that leaks
    detail.
    """
    try:
        return _password_hasher.verify(stored_hash, secret)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 - a malformed/legacy hash must fail closed, not 500
        return False


def sha256_digest(value: str) -> str:
    """Return the hex SHA-256 digest of `value`.

    Used to key the in-process API-key verification cache: the cache stores a
    digest of a high-entropy 128-bit secret, never the secret itself, and a
    digest can't be reversed to recover the key (`src/auth/__init__.py`).
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings in constant time to avoid a timing side-channel.

    Used for the verification-cache digest comparison (ASVS 11.2.4 — in-scope
    L3 for this project because usage data is billing-grade).
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
