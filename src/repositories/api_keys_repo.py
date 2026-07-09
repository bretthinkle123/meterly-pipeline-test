"""Repository for `api_keys` — the split-token lookup by `key_id`.

This is the one repository that legitimately queries by something other than
`api_key_id` (it is *how* `api_key_id` gets resolved from a presented token),
so it is exempt from the row-level-security scoping rule the other
repositories enforce — there is no tenant scope yet at this point in the
request.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class ApiKeyRecord:
    """A row from `api_keys`, the fields the auth guard needs to verify a request."""

    id: int
    key_id: str
    secret_hash: str
    rate_limit_per_sec: int
    revoked_at: object | None
    scope: str


async def find_active_key_by_key_id(
    connection: AsyncConnection, key_id: str
) -> ApiKeyRecord | None:
    """Look up a non-revoked API key by its public `key_id` handle.

    Returns None if no matching, non-revoked key exists — the caller (the
    auth guard) treats that identically to a failed secret verification so
    key enumeration can't be distinguished from a wrong secret.
    """
    result = await connection.execute(
        text(
            """
            SELECT id, key_id, secret_hash, rate_limit_per_sec, revoked_at, scope
            FROM api_keys
            WHERE key_id = :key_id AND revoked_at IS NULL
            """
        ),
        {"key_id": key_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return ApiKeyRecord(
        id=row["id"],
        key_id=row["key_id"],
        secret_hash=row["secret_hash"],
        rate_limit_per_sec=row["rate_limit_per_sec"],
        revoked_at=row["revoked_at"],
        scope=row["scope"],
    )


async def create_api_key(
    connection: AsyncConnection,
    *,
    key_id: str,
    secret_hash: str,
    label: str,
    rate_limit_per_sec: int = 100,
    scope: str = "ingest",
) -> int:
    """Insert a new API key row (secret already hashed by the caller) and
    return its internal surrogate id. Used only by `scripts/seed_api_key.py`
    — there is no key-creation HTTP endpoint in this build's scope.

    `scope` defaults to `'ingest'` so every existing caller of this function
    is unaffected; pass `scope='admin'` to provision a key that may also call
    `PUT /v1/quotas` (admin is a superset scope, not a separate key family).
    """
    result = await connection.execute(
        text(
            """
            INSERT INTO api_keys (key_id, secret_hash, label, rate_limit_per_sec, scope)
            VALUES (:key_id, :secret_hash, :label, :rate_limit_per_sec, :scope)
            RETURNING id
            """
        ),
        {
            "key_id": key_id,
            "secret_hash": secret_hash,
            "label": label,
            "rate_limit_per_sec": rate_limit_per_sec,
            "scope": scope,
        },
    )
    return result.scalar_one()
