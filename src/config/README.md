# src/config/

## Purpose

Application configuration and secrets management: environment variables, dependency injection, and AWS Secrets Manager integration for database credentials.

## Modules

| File / Module | Responsibility |
|---|---|
| `settings.py` | `Settings` — Pydantic BaseSettings config class; loads all environment variables at app startup. Singleton instance available as `settings`. |
| `secrets.py` | `fetch_db_credentials()` — fetches the RDS database connection string from AWS Secrets Manager (if `DATABASE_URL` is not already set). Async function, called during lifespan startup. |

## Relationships

**Public surface:**
- `settings` (module-level singleton) is imported by many modules: `src/auth`, `src/logging`, `src/observability`, `src/db`, `src/main.py`.
- `fetch_db_credentials()` is called once at app startup in `src/main.py` (lifespan context manager).

**Dependencies:**
- `settings.py` uses Pydantic v2 for validation and type-checking.
- `secrets.py` imports boto3 (AWS SDK) to call `SecretsManager.get_secret_value()`.

**Configuration sources:**
- Environment variables are the primary source (12-factor app pattern).
- If `DATABASE_URL` is not set, `fetch_db_credentials()` fetches it from Secrets Manager.
- Required env vars: `DATABASE_URL` (or AWS credentials + secret name), `REDIS_URL`, `RELEASE_SHA`.
- Optional: `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `LOG_LEVEL`, `ENVIRONMENT`.

## Notes

**Settings attributes:**
- `database_url` — async PostgreSQL connection string (e.g., `postgresql+asyncpg://user:pass@host/dbname`).
- `redis_url` — Redis connection string (e.g., `redis://host:6379/0`).
- `release_sha` — commit hash (for Sentry and X-Ray tagging).
- `sentry_dsn` — Sentry project DSN (optional; if unset, Sentry is disabled).
- `otel_exporter_otlp_endpoint` — OTel exporter endpoint (optional; if unset, OTel is disabled).
- `log_level` — structlog verbosity (default: INFO).
- `environment` — deployment context (local/staging/prod).

**Secrets Manager integration:**
- At startup, if `DATABASE_URL` is not set, `fetch_db_credentials()` is called.
- It retrieves the secret named in the `DB_SECRET_NAME` env var (default: `meterly/rds/credentials`).
- The secret is expected to be a JSON object with keys: `username`, `password`, `host`, `port`, `dbname`.
- The function constructs the async connection string: `postgresql+asyncpg://user:pass@host:port/dbname`.

**Validation:**
- Pydantic validates all env vars at load time.
- Missing required fields raise a `ValidationError` at startup (fail-fast).
- Type mismatches (e.g., non-integer PORT) are caught immediately.

**Testing:**
- Local dev uses the `smoke.env` file (checked into the repo) with defaults (no secrets).
- CI uses environment variables set by the test runner or GitHub Actions secrets.
- Prod uses AWS Secrets Manager for the database credential (IAM-authenticated fetch).
