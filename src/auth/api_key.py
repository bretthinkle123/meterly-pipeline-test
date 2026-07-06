"""Split-token API-key parsing and Argon2id verification against `api_keys`.

Key format: `mtr_live_<key_id>_<secret>` — `key_id` is a public, indexed lookup
handle; `secret` is a >=128-bit CSPRNG value whose Argon2id hash is the only
thing stored. Parsing is a strict anchored regex so a malformed header is
rejected before any database work (`src/api/schemas` sibling contract for the
`Authorization` header, per the plan's validation-contract table).
"""

import re
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from src.crypto import hash_secret, verify_secret
from src.repositories.api_keys_repo import find_active_key_by_key_id

_TOKEN_PATTERN = re.compile(r"^mtr_live_(?P<key_id>[A-Za-z0-9]{1,32})_(?P<secret>[A-Za-z0-9]{1,64})$")
_KEY_ID_BYTES = 16
_SECRET_BYTES = 32


@dataclass(frozen=True)
class ParsedApiKey:
    """The two halves of a presented split-token API key."""

    key_id: str
    secret: str


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """The authenticated caller: the internal `api_key_id` and its rate-limit budget."""

    api_key_id: int
    rate_limit_per_sec: int


def parse_split_token(presented_key: str) -> ParsedApiKey | None:
    """Parse `mtr_live_<key_id>_<secret>`; returns None for anything malformed.

    A strict, anchored regex means a malformed token is rejected before any
    database work — no scanning, no partial parse (401 before any DB work).
    """
    match = _TOKEN_PATTERN.match(presented_key)
    if match is None:
        return None
    return ParsedApiKey(key_id=match.group("key_id"), secret=match.group("secret"))


async def verify_api_key(
    connection: AsyncConnection, presented_key: str
) -> AuthenticatedPrincipal | None:
    """Verify a presented `Authorization` bearer value end to end.

    Parses the split token, looks the key up by its public `key_id`, and runs
    an Argon2id verify against the stored hash. Returns None on any failure —
    malformed token, unknown/revoked key_id, or a wrong secret are all
    indistinguishable to the caller (no oracle for key enumeration).
    """
    parsed = parse_split_token(presented_key)
    if parsed is None:
        return None

    record = await find_active_key_by_key_id(connection, parsed.key_id)
    if record is None:
        return None

    if not verify_secret(parsed.secret, record.secret_hash):
        return None

    return AuthenticatedPrincipal(
        api_key_id=record.id, rate_limit_per_sec=record.rate_limit_per_sec
    )


def generate_split_token() -> tuple[str, str, str]:
    """Generate a new `(key_id, secret, presented_key)` triple for key
    provisioning (`scripts/seed_api_key.py`).

    `secret_hash = hash_secret(secret)` is what the caller stores; `secret`
    and the full `presented_key` are shown to the operator exactly once and
    never persisted in plaintext.
    """
    key_id = secrets.token_hex(_KEY_ID_BYTES)
    secret = secrets.token_hex(_SECRET_BYTES)
    presented_key = f"mtr_live_{key_id}_{secret}"
    return key_id, secret, presented_key


def hash_new_secret(secret: str) -> str:
    """Hash a freshly generated secret for storage — thin re-export of the
    crypto facade so callers of this module don't need a second import."""
    return hash_secret(secret)
