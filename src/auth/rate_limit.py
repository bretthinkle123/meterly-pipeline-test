"""Redis-backed token-bucket rate limiting — Tier 1 (pre-auth, IP+route) and
Tier 2 (post-auth, per-`api_key_id`).

State lives in Redis, never in-process: an in-process counter is per-task and
fails open behind the ALB once there is more than one Fargate task
(`api-edge-conventions`). The bucket check-and-decrement is a single atomic
Lua script so concurrent requests against the same bucket can't race past the
limit (the same TOCTOU concern the events idempotency mechanism addresses,
applied to a counter instead of a unique constraint).
"""

from dataclasses import dataclass

import redis.asyncio as redis

from src.config.settings import get_settings
from src.logging import get_logger

# Atomic token-bucket refill + consume: KEYS[1] = bucket key.
# ARGV: capacity, refill_rate_per_second, now_ms, cost.
# Returns {allowed (0/1), tokens_remaining, retry_after_ms}.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(bucket[1])
local updated_at = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  updated_at = now_ms
end

local elapsed_seconds = math.max(0, (now_ms - updated_at) / 1000)
tokens = math.min(capacity, tokens + elapsed_seconds * refill_rate)

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local deficit = cost - tokens
  retry_after_ms = math.ceil((deficit / refill_rate) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'updated_at', now_ms)
redis.call('PEXPIRE', key, 60000)

return {allowed, tostring(tokens), retry_after_ms}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of a rate-limit check: whether the request is allowed and,
    if not, how long the caller should wait before retrying."""

    allowed: bool
    retry_after_seconds: float


class RedisTokenBucketLimiter:
    """A shared, atomic Redis token-bucket limiter used by both throttle tiers.

    Each tier instantiates this with its own key prefix and capacity/refill
    policy; the Lua script guarantees the check-then-decrement is race-free
    across concurrently arriving requests and across Fargate tasks.
    """

    def __init__(self, redis_client: redis.Redis, *, key_prefix: str) -> None:
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._script = self._redis.register_script(_TOKEN_BUCKET_LUA)

    async def check(
        self, bucket_identity: str, *, capacity: int, refill_rate_per_second: float, cost: float = 1.0
    ) -> RateLimitDecision:
        """Consume `cost` tokens from `bucket_identity`'s bucket; returns the decision."""
        import time

        now_ms = int(time.time() * 1000)
        allowed, _tokens_remaining, retry_after_ms = await self._script(
            keys=[f"{self._key_prefix}:{bucket_identity}"],
            args=[capacity, refill_rate_per_second, now_ms, cost],
        )
        return RateLimitDecision(
            allowed=bool(int(allowed)), retry_after_seconds=round(int(retry_after_ms) / 1000, 3)
        )


_redis_client: redis.Redis | None = None
_tier1_limiter: RedisTokenBucketLimiter | None = None
_tier2_limiter: RedisTokenBucketLimiter | None = None


def get_redis_client() -> redis.Redis:
    """Return the process-wide async Redis client, creating it on first use
    (deferred, like the DB engine, so import stays dependency-free)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis_client


def get_tier1_limiter() -> RedisTokenBucketLimiter:
    """Return the Tier-1 (pre-auth, IP+route-keyed) limiter singleton."""
    global _tier1_limiter
    if _tier1_limiter is None:
        _tier1_limiter = RedisTokenBucketLimiter(get_redis_client(), key_prefix="ratelimit:tier1")
    return _tier1_limiter


def get_tier2_limiter() -> RedisTokenBucketLimiter:
    """Return the Tier-2 (post-auth, `api_key_id`-keyed) limiter singleton."""
    global _tier2_limiter
    if _tier2_limiter is None:
        _tier2_limiter = RedisTokenBucketLimiter(get_redis_client(), key_prefix="ratelimit:tier2")
    return _tier2_limiter


async def dispose_redis_client() -> None:
    """Close the Redis client (called from lifespan shutdown)."""
    global _redis_client, _tier1_limiter, _tier2_limiter
    if _redis_client is not None:
        await _redis_client.aclose()
    _redis_client = None
    _tier1_limiter = None
    _tier2_limiter = None


async def enforce_tier2_rate_limit(request: "Request", principal: "AuthenticatedPrincipal") -> None:
    """Tier-2 dependency: enforce the caller's per-`api_key_id` token bucket.

    Registered on a dependency that runs strictly **after** `require_api_key`
    (both endpoint routes depend on this ordering), so the bucket key is
    always the authenticated principal, never the client IP — this is the
    property the two-principals-one-IP test verifies (AC14/AC16).

    Fails **open** (logs a warning, allows the request) on a Redis connection
    error — a cache outage degrading every authenticated call to "temporarily
    unlimited" is preferable to a full ingest outage; the durable UNIQUE-
    constraint/transaction guarantees are unaffected either way.
    """
    from fastapi import HTTPException, status

    try:
        decision = await get_tier2_limiter().check(
            str(principal.api_key_id),
            capacity=principal.rate_limit_per_sec,
            refill_rate_per_second=principal.rate_limit_per_sec,
        )
    except Exception:  # noqa: BLE001 - a Redis outage must not take down the whole API
        get_logger(service="meterly").warning(
            "ratelimit.backend_unavailable", endpoint=request.url.path, tier="tier2"
        )
        return

    if not decision.allowed:
        get_logger(service="meterly").warning(
            "ratelimit.exceeded",
            userId=principal.api_key_id,
            action="deny",
            endpoint=request.url.path,
            reason="tier2_bucket_exhausted",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": str(max(1, int(decision.retry_after_seconds) or 1))},
        )
