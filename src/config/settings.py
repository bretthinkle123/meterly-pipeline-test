"""Configuration facade — the single place application code reads bootstrap config.

Only *non-secret* bootstrap values live here (region, resource names/ARNs, feature
flags). Secret values are never read directly from the environment in business
logic — see `src/config/secrets.py`, which fetches them at runtime through AWS
Secrets Manager / SSM behind its own facade.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide bootstrap configuration, populated from environment variables.

    Every field here is safe to log or inspect — it locates secrets, it never
    holds one. Actual secret *values* (DB password, API-key pepper, etc.) come
    from `src.config.secrets.get_secret()`.
    """

    model_config = SettingsConfigDict(env_prefix="METERLY_", extra="ignore")

    environment: str = "local"
    service_name: str = "meterly"
    aws_region: str = "us-east-1"

    # Bootstrap pointers to secrets — names/ARNs only, never the secret value.
    database_secret_name: str = "meterly/database-url"
    database_secret_env_fallback: str = "DATABASE_URL"

    # Redis connection is treated as config, not secret (no credential embedded
    # when using the in-VPC ElastiCache endpoint with IAM/network-level auth).
    redis_url: str = "redis://localhost:6379/0"

    # Rate limiting defaults (per-key override lives in api_keys.rate_limit_per_sec).
    tier1_rate_limit_per_second: int = 200
    tier1_rate_limit_burst: int = 400

    # Edge behavior.
    max_body_size_bytes: int = 8 * 1024  # 8 KiB — see api-edge-conventions body guard
    cors_allowed_origins: tuple[str, ...] = ()  # server-to-server API: empty by default

    # Auth verification cache TTL (seconds) — see src/auth/__init__.py.
    api_key_cache_ttl_seconds: int = 300

    # Observability.
    sentry_dsn: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    release_sha: str = "local-dev"

    # Docs/OpenAPI exposure (disabled in prod per ASVS 13.4.5).
    enable_docs: bool = True

    # Dashboard (feature 3) — bootstrap pointer to the server-held BFF reader
    # credential (name/ARN only, never the secret value; see
    # src/auth/dashboard_reader.py), plus the config-driven allowlists shared
    # by CMP-3's dropdowns and the usage-series validation contract so the
    # two can never drift apart.
    dashboard_reader_secret_name: str = "meterly/dashboard-reader-key"
    dashboard_reader_secret_env_fallback: str = "DASHBOARD_READER_API_KEY"
    dashboard_customers: tuple[str, ...] = ("acme-corp", "globex", "initech")
    dashboard_metrics: tuple[str, ...] = ("api_calls", "storage_gb", "active_seats")
    # `month` is deliberately excluded — the hourly rollup cannot serve it
    # correctly within the 90-day lookback bound (plan §Q1); the segmented
    # control still renders a disabled month segment for visual fidelity.
    dashboard_granularities: tuple[str, ...] = ("hour", "day")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` singleton (cached — env is read once)."""
    return Settings()
